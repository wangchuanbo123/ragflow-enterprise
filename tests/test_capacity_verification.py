"""No application boot, network access or model calls: HTTP uses MockTransport."""
import json

import httpx
import pytest

from scripts import verify_capacity as capacity


FACT = {'id': 'fact', 'query': '负责人是谁？', 'document_ids': ['doc'],
        'expected_document_ids': ['doc'], 'expected_terms': ['林青']}
REFUSAL = {'id': 'missing', 'query': '总部地址？', 'document_ids': ['doc'], 'expect_refusal': True}


def sample(index, **overrides):
    refusal = bool(index % 2)
    result = {'case_id': 'missing' if refusal else 'fact', 'first_token_ms': 20,
              'total_ms': 60, 'error': None, 'passed': True, 'evidence_passed': True,
              'expected_refusal': refusal, 'refused': refusal, 'degraded': False, 'cleanup_ok': True,
              'started': index, 'finished': index + 1}
    return result | overrides


def test_url_and_inputs_have_no_ambient_target_defaults(tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit):
        capacity.parser().parse_args([])
    for target in ['https://secret@example.test', 'https://example.test?token=secret', 'file:///tmp/a', 'https://example.test/api/v1']:
        with pytest.raises(capacity.CapacityError):
            capacity.target_url(target)
    cases = tmp_path / 'cases.jsonl'
    cases.write_text(json.dumps(FACT), encoding='utf-8')
    report = tmp_path / 'report.json'
    monkeypatch.delenv('CAPACITY_MISSING_SESSION', raising=False)
    assert capacity.main(['--target', 'https://example.test', '--knowledge-base', 'test-kb', '--token-env',
                          'CAPACITY_MISSING_SESSION', '--cases', str(cases), '--report', str(report)]) == 2
    assert not report.exists()
    assert 'session_token_environment_missing' in capsys.readouterr().out


@pytest.mark.parametrize('change', [
    {'document_ids': []}, {'expected_document_ids': ['outside']}, {'expected_terms': []},
    {'expect_refusal': 'false'}, {'query': ''}, {'id': '../escape'}, {'document_ids': ['']},
])
def test_fixed_case_manifest_rejects_ambiguous_or_unbounded_input(tmp_path, change):
    manifest = tmp_path / 'cases.jsonl'
    manifest.write_text(json.dumps(FACT | change), encoding='utf-8')
    with pytest.raises(capacity.CapacityError):
        capacity.load_cases(manifest)


def test_case_manifest_is_fixed_and_hashed(tmp_path):
    manifest = tmp_path / 'cases.jsonl'
    manifest.write_text('\n'.join(map(json.dumps, [FACT, REFUSAL])), encoding='utf-8')
    cases, digest = capacity.load_cases(manifest)
    assert cases == [FACT, REFUSAL] and len(digest) == 64


def test_percentiles_and_insufficient_samples_do_not_claim_capacity():
    assert capacity.percentile([0, 100], .95) == 95
    assert capacity.percentile([], .95) is None
    summary = capacity.summarize([sample(i) for i in range(8)], 8, [FACT, REFUSAL], capacity.Thresholds())
    assert summary['status'] == 'unverified'
    assert 'insufficient_samples_or_rounds' in summary['unverified_reasons']
    assert 'insufficient_completed_latency_samples' in summary['unverified_reasons']
    assert capacity.summarize([sample(i) for i in range(40)], 8, [FACT, REFUSAL], capacity.Thresholds())['status'] == 'passed'


@pytest.mark.parametrize('override,gate', [
    ({'first_token_ms': 10001}, 'p95_first_token_ms'),
    ({'total_ms': 60001}, 'p95_total_ms'),
    ({'error': 'stream_error'}, 'error_rate'),
    ({'evidence_passed': False}, 'evidence_pass_rate'),
    ({'degraded': True}, 'degraded_rate'),
    ({'cleanup_ok': False}, 'conversation_cleanup'),
    ({'passed': False}, 'case_assertions'),
])
def test_threshold_failures_are_explicit(override, gate):
    result = capacity.summarize([sample(i, **override) for i in range(40)], 4, [FACT, REFUSAL], capacity.Thresholds())
    assert result['status'] == 'failed'
    assert gate in result['failed_gates']


def test_background_overlap_must_be_observed():
    samples = [sample(i, background_overlap=i < 3) for i in range(40)]
    result = capacity.summarize(samples, 8, [FACT, REFUSAL], capacity.Thresholds(), background=True)
    assert result['status'] == 'unverified'
    assert result['background_overlap_samples'] == 3
    assert 'insufficient_indexing_overlap' in result['unverified_reasons']


def test_evidence_scope_citation_and_refusal_checks_are_independent():
    source = [{'document_id': 'doc', 'chunk_id': 'chunk', 'citation_id': '[来源1]'}]
    assert capacity.answer_checks(FACT, '林青[来源1]', source, {}, 'kb')['passed']
    assert not capacity.answer_checks(FACT, '林青[来源99]', source, {}, 'kb')['passed']
    assert not capacity.answer_checks(FACT, '林青[来源1]', [source[0] | {'knowledge_base_id': 'other'}], {}, 'kb')['passed']
    assert not capacity.answer_checks(FACT, '资料不足', [], {}, 'kb')['passed']
    assert capacity.answer_checks(REFUSAL, '资料不足，无法确认地址', [], {}, 'kb')['passed']
    assert capacity.answer_checks(REFUSAL, '资料不足', [], {'evidence_status': 'unavailable'}, 'kb')['degraded']


def test_stream_uses_explicit_scope_and_final_replacement_and_deletes_only_its_conversation():
    requests = []
    token = 'SENSITIVE-SESSION-TEST-VALUE'
    def handler(request):
        requests.append(request)
        if request.url.path == '/api/v1/conversations' and request.method == 'POST':
            assert json.loads(request.content)['knowledge_base_id'] == 'test-kb'
            assert token in request.headers['cookie']
            return httpx.Response(201, json={'id': 'created-conversation'})
        if request.url.path.endswith('/messages/stream'):
            payload = json.loads(request.content)
            assert payload['retrieval']['scope'] == {'selection_kind': 'document', 'knowledge_base_ids': ['test-kb'], 'document_ids': ['doc']}
            return httpx.Response(200, text='event: delta\ndata: {"content":"错误草稿[来源99]"}\n\n'
                'event: answer\ndata: {"content":"林青[来源1]"}\n\n'
                'event: sources\ndata: {"sources":[{"document_id":"doc","citation_id":"[来源1]"}]}\n\n'
                'event: done\ndata: {"status":"completed"}\n\n')
        assert request.method == 'DELETE' and request.url.path == '/api/v1/conversations/created-conversation'
        return httpx.Response(204)
    factory = lambda target, token, timeout: capacity.make_client(target, token, timeout, transport=httpx.MockTransport(handler))
    result = capacity.measure_case('https://explicit-test.invalid', token, 'test-kb', FACT, client_factory=factory)
    assert result['passed'] and result['cleanup_ok'] and result['first_token_ms'] is not None
    assert len(requests) == 3
    assert token not in json.dumps(result) and FACT['query'] not in json.dumps(result) and '林青' not in json.dumps(result)


@pytest.mark.parametrize('body,expected_error', [
    ('event: delta\ndata: {"content":"林青"}\n\n', 'stream_incomplete'),
    ('event: done\ndata: {"status":"completed"}\n\n', 'no_answer_delta'),
    ('event: error\ndata: {"message":"sensitive-upstream-detail"}\n\n', 'stream_error'),
    ('event: delta\ndata: {"content":"林青"}', 'truncated_sse_frame'),
])
def test_protocol_failures_are_recorded_without_leaking_response_text(body, expected_error):
    def handler(request):
        if request.url.path == '/api/v1/conversations':
            return httpx.Response(201, json={'id': 'created'})
        if request.method == 'DELETE':
            return httpx.Response(204)
        return httpx.Response(200, text=body)
    factory = lambda target, token, timeout: capacity.make_client(target, token, timeout, transport=httpx.MockTransport(handler))
    result = capacity.measure_case('https://test.invalid', 'private-token', 'kb', FACT, client_factory=factory)
    assert result['error'] == expected_error and not result['passed'] and result['cleanup_ok']
    assert 'sensitive-upstream-detail' not in json.dumps(result)


def test_main_returns_nonzero_for_failed_and_unverified_gates(tmp_path, monkeypatch):
    path = tmp_path / 'capacity.json'
    for status, expected in [('passed', 0), ('failed', 1), ('unverified', 3)]:
        monkeypatch.setattr(capacity, 'run', lambda _args, status=status: {'status': status})
        assert capacity.main(['--target','https://test.invalid','--knowledge-base','test-kb','--token-env','SESSION',
                              '--cases','unused.jsonl','--report',str(path)]) == expected
        assert json.loads(path.read_text(encoding='utf-8'))['status'] == status


def test_run_never_starts_background_upload_without_explicit_flag(tmp_path, monkeypatch):
    manifest=tmp_path/'cases.jsonl'
    manifest.write_text('\n'.join(map(json.dumps,[FACT,REFUSAL])),encoding='utf-8')
    monkeypatch.setenv('CAPACITY_SESSION','session-secret')
    args=capacity.parser().parse_args(['--target','https://test.invalid','--knowledge-base','test-kb','--token-env','CAPACITY_SESSION',
        '--cases',str(manifest),'--report',str(tmp_path/'report.json'),'--samples','40'])
    monkeypatch.setattr(capacity,'run_level',lambda _args,_tokens,cases,level,thresholds:capacity.summarize([sample(i) for i in range(40)],level,cases,thresholds))
    monkeypatch.setattr(capacity,'BackgroundIndexing',lambda *_args,**_kwargs:pytest.fail('Background uploads require explicit flag'))
    report=capacity.run(args)
    assert [item['concurrency'] for item in report['baseline']]==[1,4,8]
    assert report['status']=='passed' and report['with_background_indexing']==[]
    assert 'session-secret' not in json.dumps(report)


def test_background_uploads_and_cleanup_are_confined_to_the_new_isolated_knowledge_base():
    paths=[]
    def handler(request):
        paths.append((request.method,request.url.path))
        if request.method=='POST' and request.url.path=='/api/v1/knowledge-bases':
            slug=json.loads(request.content)['slug']
            return httpx.Response(201,json={'id':'created-test-kb','slug':slug})
        assert '/created-test-kb/' in request.url.path or request.url.path=='/api/v1/knowledge-bases/created-test-kb'
        if request.method=='PUT':
            assert json.loads(request.content)=={'auto_build_enabled':False,'auto_schema_generation_enabled':False}
            return httpx.Response(200,json={})
        if request.url.path.endswith('/batch-upload'):
            assert b'Synthetic capacity indexing fixture' in request.content
            return httpx.Response(202,json={'accepted':1,'rejected':[],'document_ids':['owned-doc'],'job_id':'index-job'})
        if request.url.path.endswith('/documents/batch'):
            assert json.loads(request.content)=={'document_ids':['owned-doc'],'action':'delete'}
            return httpx.Response(202,json={'job_id':'delete-job'})
        if request.method=='GET':
            return httpx.Response(200,json={'status':'completed'})
        assert request.method=='DELETE'
        return httpx.Response(204)
    factory=lambda target,token,timeout:capacity.make_client(target,token,timeout,transport=httpx.MockTransport(handler))
    background=capacity.BackgroundIndexing('https://test.invalid','secret',1,files=1,kib=1,max_batches=1,forbidden_kb='retrieval-kb',client_factory=factory)
    background.start();background.finish()
    assert background.cleanup_ok and background.documents_deleted==1 and background.batches==1
    assert not background.error
    assert all('retrieval-kb' not in path for _,path in paths)
    assert paths[-1]==('DELETE','/api/v1/knowledge-bases/created-test-kb')


def test_background_ownership_mismatch_cannot_mutate_the_retrieval_knowledge_base():
    requests=[]
    def handler(request):
        requests.append(request)
        return httpx.Response(201,json={'id':'retrieval-kb','slug':json.loads(request.content)['slug']})
    factory=lambda target,token,timeout:capacity.make_client(target,token,timeout,transport=httpx.MockTransport(handler))
    background=capacity.BackgroundIndexing('https://test.invalid','secret',1,forbidden_kb='retrieval-kb',client_factory=factory)
    with pytest.raises(capacity.CapacityError,match='ownership_not_confirmed'):
        background.start()
    background.finish()
    assert len(requests)==1 and background.kb is None and not background.cleanup_ok
