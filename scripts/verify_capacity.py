"""Explicit-target HTTP capacity acceptance; never imports application settings.

Prepare a JSONL case file against a dedicated test knowledge base. Each line:
{"id":"fact", "query":"...", "document_ids":["doc"],
 "expected_document_ids":["doc"], "expected_terms":["known fact"]}
Include unanswerable cases with expect_refusal=true. Authentication is an existing
session cookie supplied only through --token-env ENV_NAME (repeat for real users).
No login secrets, questions, answers, or token values are written to the report.

Example: python -m scripts.verify_capacity --target https://test.example \
  --knowledge-base TEST_KB --token-env CAPACITY_SESSION --cases cases.jsonl \
  --report capacity.json

--background-create-isolated-kb additionally creates a temporary knowledge base,
uploads bounded synthetic documents there, measures overlapping indexing work,
and deletes the uploaded documents before archiving that new knowledge base. Without this flag no files
are uploaded. The specified retrieval knowledge base is never uploaded into.
This performs real model calls only when explicitly run against a server.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlsplit
import uuid


class CapacityError(RuntimeError):
    """Fixed error codes only; do not expose HTTP responses or credentials."""


def require(condition, code):
    if not condition:
        raise CapacityError(code)


def target_url(value):
    parsed = urlsplit(value)
    require(parsed.scheme in {'http', 'https'} and bool(parsed.hostname)
            and not any((parsed.username, parsed.password, parsed.query, parsed.fragment))
            and parsed.path in {'', '/'}, 'target_must_be_explicit_origin')
    return value.rstrip('/')


def identifier(value):
    require(isinstance(value, str) and bool(re.fullmatch(r'[A-Za-z0-9_-]{1,160}', value)), 'invalid_identifier')
    return value


def load_cases(path):
    raw = Path(path).read_bytes()
    require(len(raw) <= 4 * 1024 * 1024, 'cases_too_large')
    cases = []
    try:
        for line in raw.decode('utf-8-sig').splitlines():
            if not line.strip():
                continue
            case = json.loads(line)
            require(isinstance(case, dict), 'invalid_case')
            identifier(case.get('id'))
            require(isinstance(case.get('query'), str) and 0 < len(case['query'].strip()) <= 4000, 'invalid_case_query')
            for key in ('document_ids', 'expected_document_ids', 'expected_chunk_ids', 'expected_terms', 'forbidden_terms'):
                values = case.get(key, [])
                require(isinstance(values, list) and len(values) <= 500
                        and all(isinstance(value, str) and value.strip() and len(value) <= 4000 for value in values), 'invalid_case_assertions')
            require(type(case.get('expect_refusal', False)) is bool, 'invalid_refusal_expectation')
            require(bool(case.get('document_ids')), 'explicit_document_scope_required')
            require(set(case.get('expected_document_ids', [])) <= set(case['document_ids']), 'expected_evidence_outside_scope')
            if not case.get('expect_refusal', False):
                require(bool(case.get('expected_document_ids') or case.get('expected_chunk_ids'))
                        and bool(case.get('expected_terms')), 'answerable_case_requires_evidence_and_terms')
            cases.append(case)
    except (UnicodeError, json.JSONDecodeError, TypeError):
        raise CapacityError('invalid_cases_jsonl') from None
    require(0 < len(cases) <= 500 and len({case['id'] for case in cases}) == len(cases), 'cases_missing_or_duplicate')
    return cases, hashlib.sha256(raw).hexdigest()


def iter_sse(lines):
    event, data, size = 'message', [], 0
    for line in lines:
        if not line:
            if data:
                try:
                    payload = json.loads('\n'.join(data))
                except (ValueError, TypeError):
                    raise CapacityError('invalid_sse_json') from None
                require(isinstance(payload, dict), 'invalid_sse_payload')
                yield event, payload
            event, data, size = 'message', [], 0
        elif line.startswith('event:'):
            event = line[6:].strip()
        elif line.startswith('data:'):
            size += len(line)
            require(size <= 2 * 1024 * 1024, 'sse_frame_too_large')
            data.append(line[5:].lstrip())
    if data:
        raise CapacityError('truncated_sse_frame')


REFUSAL = re.compile(r'无法(?:确认|确定|回答|核实)|(?:资料|证据|信息)不足|未(?:提供|记载|提及|说明|找到)|没有(?:相关|足够|提供)|不知道|not enough (?:information|evidence)|cannot (?:answer|determine)|insufficient', re.I)


def answer_checks(case, answer, sources, route, knowledge_base):
    source_docs = {source.get('document_id') for source in sources}
    source_chunks = {source.get('chunk_id') for source in sources}
    refused = bool(REFUSAL.search(answer)) or route.get('evidence_status') in {'insufficient', 'unavailable'}
    expected_refusal = case.get('expect_refusal', False)
    checks = {
        'scope': all(source.get('document_id') in case['document_ids']
                     and source.get('knowledge_base_id', knowledge_base) == knowledge_base for source in sources),
        'expected_documents': set(case.get('expected_document_ids', [])) <= source_docs,
        'expected_chunks': set(case.get('expected_chunk_ids', [])) <= source_chunks,
        'terms': all(term.casefold() in answer.casefold() for term in case.get('expected_terms', [])),
        'forbidden_terms_absent': all(term.casefold() not in answer.casefold() for term in case.get('forbidden_terms', [])),
        'refusal': refused == expected_refusal,
        'citation_ids': route.get('citation_validation', {}).get('status') != 'invalid',
    }
    # Independently check citation numbering; this is not factual entailment.
    labels = {str(source.get('citation_id') or f'来源{index+1}').strip('[] ') for index, source in enumerate(sources)}
    checks['citation_ids'] = checks['citation_ids'] and set(re.findall(r'\[(来源\d+)\]', answer)) <= labels
    return {'checks': checks, 'passed': all(checks.values()), 'refused': refused,
            'expected_refusal': expected_refusal, 'evidence_passed': all(checks[key] for key in ('scope', 'expected_documents', 'expected_chunks', 'citation_ids')),
            'degraded': route.get('evidence_status') == 'unavailable' or any(item.get('status') in {'degraded', 'error'} for item in route.get('retrieval_diagnostics', [])),
            'source_count': len(sources)}


def checked_json(client, method, path, **kwargs):
    response = client.request(method, path, **kwargs)
    require(200 <= response.status_code < 300, 'http_' + str(response.status_code))
    if response.status_code == 204:
        return None
    try:
        return response.json()
    except ValueError:
        raise CapacityError('invalid_http_json') from None


def make_client(target, token, timeout, *, transport=None):
    import httpx
    return httpx.Client(base_url=target, cookies={'rag_auth_token': token}, timeout=timeout,
                        trust_env=False, follow_redirects=False, transport=transport)


def measure_case(target, token, kb, case, *, timeout=180, strategy='standard', client_factory=make_client):
    started = time.monotonic()
    result = {'case_id': case['id'], 'started': started, 'first_token_ms': None,
              'total_ms': None, 'error': None, 'passed': False, 'evidence_passed': False,
              'expected_refusal': case.get('expect_refusal', False), 'refused': False, 'degraded': False}
    conversation = None
    with client_factory(target, token, timeout) as client:
        try:
            conversation = checked_json(client, 'POST', '/api/v1/conversations', json={
                'title': '容量验收 ' + case['id'], 'knowledge_base_id': kb})
            identifier(conversation.get('id'))
            result['setup_ms'] = (time.monotonic() - started) * 1000
            stream_started, answer, sources, route, complete = time.monotonic(), '', [], {}, False
            with client.stream('POST', f"/api/v1/conversations/{conversation['id']}/messages/stream", json={
                'content': case['query'], 'retrieval': {'strategy': strategy,
                'scope': {'selection_kind': 'document', 'knowledge_base_ids': [kb], 'document_ids': case['document_ids']}}}) as response:
                require(response.status_code == 200, 'stream_http_' + str(response.status_code))
                for event, data in iter_sse(response.iter_lines()):
                    require(time.monotonic() - started <= timeout, 'request_deadline_exceeded')
                    if event == 'delta' and isinstance(data.get('content'), str) and data['content']:
                        if result['first_token_ms'] is None:
                            result['first_token_ms'] = (time.monotonic() - stream_started) * 1000
                        answer += data['content']
                    elif event == 'answer' and isinstance(data.get('content'), str):
                        answer = data['content']
                    elif event == 'sources':
                        sources = data.get('sources', [])
                        require(isinstance(sources, list) and all(isinstance(item, dict) for item in sources), 'invalid_sources')
                    elif event == 'route':
                        route = data.get('route') or {}
                        require(isinstance(route, dict), 'invalid_route')
                    elif event == 'error':
                        raise CapacityError('stream_error')
                    elif event == 'done':
                        complete = data.get('status') == 'completed'
                    require(len(answer) <= 2 * 1024 * 1024, 'answer_too_large')
            require(complete, 'stream_incomplete')
            require(result['first_token_ms'] is not None, 'no_answer_delta')
            result.update(answer_checks(case, answer, sources, route, kb))
        except Exception as exc:
            result['error'] = str(exc) if isinstance(exc, CapacityError) else 'transport_or_protocol_error'
        finally:
            result['finished'] = time.monotonic()
            result['total_ms'] = (result['finished'] - started) * 1000
            result['cleanup_ok'] = True
            if isinstance(conversation, dict) and re.fullmatch(r'[A-Za-z0-9_-]{1,160}', str(conversation.get('id', ''))):
                try:
                    checked_json(client, 'DELETE', '/api/v1/conversations/' + conversation['id'])
                except Exception:
                    result['cleanup_ok'] = False
    return result


def percentile(values, fraction):
    values = sorted(value for value in values if value is not None and math.isfinite(value))
    if not values:
        return None
    position = (len(values) - 1) * fraction
    lower = math.floor(position)
    return values[lower] + (values[math.ceil(position)] - values[lower]) * (position - lower)


@dataclass(frozen=True)
class Thresholds:
    min_samples: int = 20
    min_rounds: int = 3
    max_p95_first_token_ms: float = 10000
    max_p95_total_ms: float = 60000
    max_error_rate: float = 0.01
    min_evidence_pass_rate: float = 1.0
    max_unexpected_refusal_rate: float = 0.01
    min_refusal_pass_rate: float = 1.0
    max_degraded_rate: float = 0.0


def summarize(samples, concurrency, cases, thresholds, *, background=False):
    count = len(samples)
    completed = [sample for sample in samples if not sample['error']]
    answerable = [sample for sample in samples if not sample['expected_refusal']]
    refusals = [sample for sample in samples if sample['expected_refusal']]
    rate = lambda values, predicate: sum(bool(predicate(value)) for value in values) / len(values) if values else None
    measured = {
        'first_token_ms': {label: percentile([sample['first_token_ms'] for sample in completed], fraction) for label, fraction in [('p50', .5), ('p95', .95)]},
        'total_ms': {label: percentile([sample['total_ms'] for sample in samples], fraction) for label, fraction in [('p50', .5), ('p95', .95)]},
        'error_rate': rate(samples, lambda sample: bool(sample['error'])),
        'evidence_pass_rate': rate(answerable, lambda sample: not sample['error'] and sample['evidence_passed']),
        'unexpected_refusal_rate': rate(answerable, lambda sample: sample['refused']),
        'refusal_pass_rate': rate(refusals, lambda sample: not sample['error'] and sample['passed']),
        'degraded_rate': rate(samples, lambda sample: sample['degraded']),
        'assertion_pass_rate': rate(samples, lambda sample: sample['passed']),
    }
    missing = sorted({case['id'] for case in cases} - {sample['case_id'] for sample in samples})
    insufficient = []
    if count < max(thresholds.min_samples, concurrency * thresholds.min_rounds):
        insufficient.append('insufficient_samples_or_rounds')
    if len(completed) < thresholds.min_samples:
        insufficient.append('insufficient_completed_latency_samples')
    if missing:
        insufficient.append('not_all_fixed_cases_measured')
    if not answerable or not refusals:
        insufficient.append('answerable_and_refusal_cases_required')
    if any(sum(sample['case_id'] == case['id'] for sample in samples) < 3 for case in cases):
        insufficient.append('each_fixed_case_requires_three_repetitions')
    overlap_count = sum(sample.get('background_overlap', False) for sample in samples)
    if background and overlap_count < max(thresholds.min_samples, concurrency * thresholds.min_rounds):
        insufficient.append('insufficient_indexing_overlap')
    failures = []
    for name, value, limit, direction in (
        ('p95_first_token_ms', measured['first_token_ms']['p95'], thresholds.max_p95_first_token_ms, 'max'),
        ('p95_total_ms', measured['total_ms']['p95'], thresholds.max_p95_total_ms, 'max'),
        ('error_rate', measured['error_rate'], thresholds.max_error_rate, 'max'),
        ('evidence_pass_rate', measured['evidence_pass_rate'], thresholds.min_evidence_pass_rate, 'min'),
        ('unexpected_refusal_rate', measured['unexpected_refusal_rate'], thresholds.max_unexpected_refusal_rate, 'max'),
        ('refusal_pass_rate', measured['refusal_pass_rate'], thresholds.min_refusal_pass_rate, 'min'),
        ('degraded_rate', measured['degraded_rate'], thresholds.max_degraded_rate, 'max')):
        if value is not None and (value > limit if direction == 'max' else value < limit):
            failures.append(name)
    if any(not sample['passed'] for sample in samples):
        failures.append('case_assertions')
    if any(not sample.get('cleanup_ok', True) for sample in samples):
        failures.append('conversation_cleanup')
    return {'concurrency': concurrency, 'samples': count, 'completed': len(completed), 'background_overlap_samples': overlap_count,
            'metrics': measured, 'missing_cases': missing, 'unverified_reasons': insufficient, 'failed_gates': failures,
            'status': 'failed' if failures else 'unverified' if insufficient else 'passed'}


class BackgroundIndexing:
    """Generate load exclusively inside a newly created, disposable knowledge base."""
    def __init__(self, target, token, timeout, *, files=4, kib=128, max_batches=20, forbidden_kb=None, client_factory=make_client):
        self.target, self.token, self.timeout = target, token, timeout
        self.files, self.kib, self.max_batches, self.client_factory = files, kib, max_batches, client_factory
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.intervals = []
        self.error = None
        self.cleanup_ok = False
        self.batches = 0
        self.kb = None
        self.job = None
        self.forbidden_kb = forbidden_kb
        self.documents = set()
        self.documents_deleted = 0

    def _wait_job(self, client, job_id, *, cancel=False):
        if cancel:
            checked_json(client, 'POST', f'/api/v1/knowledge-bases/{self.kb}/index-jobs/{job_id}/cancel')
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            job = checked_json(client, 'GET', f'/api/v1/knowledge-bases/{self.kb}/index-jobs/{job_id}')
            if job.get('status') in {'completed', 'partial', 'failed', 'cancelled'}:
                return job['status']
            if self.stop.wait(.25) and not cancel:
                checked_json(client, 'POST', f'/api/v1/knowledge-bases/{self.kb}/index-jobs/{job_id}/cancel')
                cancel = True
            elif cancel:
                time.sleep(.25)
        raise CapacityError('background_job_timeout')

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True, name='capacity-isolated-indexing')
        self.thread.start()
        require(self.ready.wait(self.timeout), 'background_start_timeout')
        require(not self.error, self.error or 'background_failed')

    def _run(self):
        nonce = uuid.uuid4().hex
        with self.client_factory(self.target, self.token, self.timeout) as client:
            try:
                created = checked_json(client, 'POST', '/api/v1/knowledge-bases', json={
                    'name': '容量验收临时库 ' + nonce, 'slug': 'capacity-' + nonce})
                require(created.get('slug') == 'capacity-' + nonce and created.get('id') != self.forbidden_kb, 'background_ownership_not_confirmed')
                self.kb = identifier(created.get('id'))
                checked_json(client, 'PUT', f'/api/v1/knowledge-bases/{self.kb}/knowledge-graph/automation', json={
                    'auto_build_enabled': False, 'auto_schema_generation_enabled': False})
                for batch in range(self.max_batches):
                    if self.stop.is_set():
                        break
                    started = time.monotonic()
                    self.intervals.append([started, None])
                    content = (('Synthetic capacity indexing fixture ' + nonce + str(batch) + '\n').encode() * (self.kib * 1024 // 40 + 1))[:self.kib * 1024]
                    files = [('files', (f'capacity-{nonce}-{batch}-{index}.txt', content + str(index).encode(), 'text/plain')) for index in range(self.files)]
                    result = checked_json(client, 'POST', f'/api/v1/knowledge-bases/{self.kb}/documents/batch-upload', files=files)
                    self.documents.update(identifier(value) for value in result.get('document_ids', []))
                    require(result.get('accepted') == self.files and not result.get('rejected') and result.get('job_id'), 'background_upload_rejected')
                    self.job = identifier(result['job_id'])
                    self.batches += 1
                    self.ready.set()
                    state = self._wait_job(client, self.job)
                    require(state == 'completed' or self.stop.is_set() and state == 'cancelled', 'background_indexing_failed')
                    self.intervals[-1][1] = time.monotonic()
                    self.job = None
            except Exception as exc:
                self.error = str(exc) if isinstance(exc, CapacityError) else 'background_transport_error'
            finally:
                if self.intervals and self.intervals[-1][1] is None:
                    self.intervals[-1][1] = time.monotonic()
                self.ready.set()
                if self.kb:
                    try:
                        if self.job:
                            self._wait_job(client, self.job, cancel=True)
                        identifiers = sorted(self.documents)
                        for start in range(0, len(identifiers), 500):
                            batch = identifiers[start:start + 500]
                            deletion = checked_json(client, 'POST', f'/api/v1/knowledge-bases/{self.kb}/documents/batch',
                                                    json={'document_ids': batch, 'action': 'delete'})
                            if deletion.get('job_id'):
                                # Cleanup jobs must finish, even though the load stop event is set.
                                job_id = identifier(deletion['job_id'])
                                deadline = time.monotonic() + self.timeout
                                while time.monotonic() < deadline:
                                    state = checked_json(client, 'GET', f'/api/v1/knowledge-bases/{self.kb}/index-jobs/{job_id}').get('status')
                                    if state in {'completed', 'partial', 'failed', 'cancelled'}:
                                        require(state == 'completed', 'background_document_cleanup_failed')
                                        break
                                    time.sleep(.25)
                                else:
                                    raise CapacityError('background_document_cleanup_timeout')
                            self.documents_deleted += len(batch)
                        checked_json(client, 'DELETE', f'/api/v1/knowledge-bases/{self.kb}')
                        self.cleanup_ok = True
                    except Exception:
                        self.error = self.error or 'background_cleanup_failed'

    def finish(self):
        self.stop.set()
        self.thread.join(2 * self.timeout + 10)
        require(not self.thread.is_alive(), 'background_shutdown_timeout')

    def overlaps(self, sample):
        return any(sample['started'] < (end or time.monotonic()) and sample['finished'] > start for start, end in self.intervals)


def run_level(args, tokens, cases, concurrency, thresholds, background=None, measure=measure_case):
    def request(index):
        sample = measure(args.target, tokens[index % len(tokens)], args.knowledge_base, cases[index % len(cases)], timeout=args.timeout, strategy=args.strategy)
        if background:
            sample['background_overlap'] = background.overlaps(sample)
        return sample
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        samples = list(pool.map(request, range(args.samples)))
    result = summarize(samples, concurrency, cases, thresholds, background=background is not None)
    result['elapsed_seconds'] = time.monotonic() - started
    result['requests_per_second'] = len(samples) / max(result['elapsed_seconds'], 0.000001)
    result['requests'] = [{key: value for key, value in sample.items() if key not in {'started', 'finished'}} for sample in samples]
    return result


def parser():
    result = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    result.add_argument('--target', required=True, help='Explicit test API origin, without /api/v1')
    result.add_argument('--knowledge-base', required=True, help='Explicit prepared test knowledge base')
    result.add_argument('--token-env', required=True, action='append', help='Environment variable containing an existing session cookie; repeat for multiple users')
    result.add_argument('--cases', required=True, type=Path)
    result.add_argument('--report', required=True, type=Path)
    result.add_argument('--concurrency', default='1,4,8')
    result.add_argument('--samples', type=int, default=40, help='Measured requests per concurrency level and scenario')
    result.add_argument('--timeout', type=float, default=180)
    result.add_argument('--strategy', choices=['fast', 'standard', 'deep'], default='standard')
    result.add_argument('--min-samples', type=int, default=20)
    result.add_argument('--max-p95-first-token-ms', type=float, default=10000)
    result.add_argument('--max-p95-total-ms', type=float, default=60000)
    result.add_argument('--max-error-rate', type=float, default=.01)
    result.add_argument('--min-evidence-pass-rate', type=float, default=1)
    result.add_argument('--max-unexpected-refusal-rate', type=float, default=.01)
    result.add_argument('--min-refusal-pass-rate', type=float, default=1)
    result.add_argument('--max-degraded-rate', type=float, default=0)
    result.add_argument('--background-create-isolated-kb', action='store_true')
    result.add_argument('--background-files', type=int, default=4)
    result.add_argument('--background-kib', type=int, default=128)
    result.add_argument('--background-max-batches', type=int, default=20)
    return result


def run(args):
    args.target = target_url(args.target)
    identifier(args.knowledge_base)
    require(1 <= args.samples <= 10000 and 20 <= args.min_samples <= 10000 and 1 <= args.timeout <= 3600, 'invalid_sample_or_timeout_limits')
    require(1 <= args.background_files <= 20 and 1 <= args.background_kib <= 1024 and 1 <= args.background_max_batches <= 100, 'invalid_background_limits')
    require(args.report.resolve() != args.cases.resolve(), 'report_must_not_overwrite_cases')
    try:
        levels = [int(value) for value in args.concurrency.split(',')]
    except ValueError:
        raise CapacityError('invalid_concurrency') from None
    require(bool(levels) and len(levels) == len(set(levels)) and all(1 <= level <= 64 for level in levels), 'invalid_concurrency')
    tokens = [os.environ.get(name, '') for name in args.token_env]
    require(all(tokens) and all('\r' not in token and '\n' not in token and ';' not in token for token in tokens), 'session_token_environment_missing_or_invalid')
    cases, digest = load_cases(args.cases)
    fields = {field: getattr(args, field) for field in Thresholds.__dataclass_fields__ if hasattr(args, field)}
    require(all(math.isfinite(value) and value >= 0 for value in fields.values()), 'invalid_threshold')
    require(all(value <= 1 for key, value in fields.items() if key.endswith('_rate')), 'invalid_rate_threshold')
    thresholds = Thresholds(**fields)
    report = {'schema_version': 1, 'target': args.target, 'knowledge_base': args.knowledge_base, 'cases_sha256': digest,
              'case_count': len(cases), 'authenticated_sessions': len(tokens), 'strategy': args.strategy,
              'thresholds': vars(thresholds), 'baseline': [], 'with_background_indexing': [],
              'limitations': ['Finite test workload only; not a maximum document-capacity guarantee.',
                             'Expected text terms and citation IDs do not establish factual entailment.',
                             'First-token latency starts at the streaming HTTP request; total latency includes conversation setup.',
                             'One session can create concurrent requests; real-user capacity needs representative sessions.',
                             'Warm/cold cache, provider quota and corpus size must be recorded separately by the operator.']}
    for level in levels:
        report['baseline'].append(run_level(args, tokens, cases, level, thresholds))
    background = None
    try:
        if args.background_create_isolated_kb:
            background = BackgroundIndexing(args.target, tokens[0], args.timeout, files=args.background_files,
                                            kib=args.background_kib, max_batches=args.background_max_batches, forbidden_kb=args.knowledge_base)
            background.start()
            for level in levels:
                report['with_background_indexing'].append(run_level(args, tokens, cases, level, thresholds, background))
    except CapacityError as exc:
        report['background_failure'] = str(exc)
    finally:
        if background:
            try:
                background.finish()
            except CapacityError as exc:
                report['background_failure'] = str(exc)
            report['background'] = {'isolated_knowledge_base': background.kb, 'batches': background.batches,
                                    'cleanup_ok': background.cleanup_ok, 'documents_deleted': background.documents_deleted,
                                    'cleanup_mode': 'delete_uploaded_documents_then_archive_temporary_knowledge_base', 'error': background.error}
    for baseline, interfered in zip(report['baseline'], report['with_background_indexing']):
        interfered['impact'] = {key: interfered['metrics'][key]['p95'] - baseline['metrics'][key]['p95']
                               if interfered['metrics'][key]['p95'] is not None and baseline['metrics'][key]['p95'] is not None else None
                               for key in ('first_token_ms', 'total_ms')}
    results = report['baseline'] + report['with_background_indexing']
    report['status'] = 'failed' if any(result['status'] == 'failed' for result in results) or report.get('background_failure') or background and background.error else (
        'unverified' if any(result['status'] == 'unverified' for result in results) else 'passed')
    return report


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        report = run(args)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'status': report['status'], 'report': str(args.report)}, ensure_ascii=False))
        return {'passed': 0, 'failed': 1, 'unverified': 3}[report['status']]
    except (CapacityError, OSError) as exc:
        code = str(exc) if isinstance(exc, CapacityError) else 'local_file_error'
        print(json.dumps({'status': 'failed', 'error': code}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
