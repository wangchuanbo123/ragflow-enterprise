"""Repeatable 50-member/10-request team acceptance using isolated real services.

The ASGI application, PostgreSQL, pgvector, Redis, authorization and job worker
are real. Only generation/embedding/reranking providers are controlled locally.
This measures application behavior, not cloud-model or HTTP-server throughput.
Run this module in a fresh process; it never uses DATABASE_URL as a default.
"""

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import threading
import time
import uuid


def synthetic_upload(index):
    """Distinct contents exercise independent jobs rather than upload deduplication."""
    return (f"parallel-{index}.txt", (f"TEAM_ALPHA_ONLY 同时上传资料。合成上传编号 {index}。\n" * 30).encode("utf-8"), "text/plain")


def verify(postgres_url, redis_url, *, members=50, concurrency=10):
    if "app.core.config" in sys.modules:
        raise RuntimeError("Run the verification in a fresh subprocess")
    if members < concurrency or not 1 <= concurrency <= 50 or not 1 <= members <= 200:
        raise ValueError("Invalid synthetic team size")
    from sqlalchemy import create_engine, select, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.schema import CreateSchema, DropSchema
    schema, prefix = "verify_team_" + uuid.uuid4().hex, "verify-team:" + uuid.uuid4().hex
    source_url = make_url(postgres_url)
    if source_url.get_backend_name() != "postgresql":
        raise ValueError("An explicit test PostgreSQL URL is required")
    source_url = source_url.set(drivername="postgresql+psycopg")
    admin_engine = create_engine(source_url, hide_parameters=True)
    with admin_engine.begin() as connection:
        connection.execute(CreateSchema(schema))
    target = source_url.update_query_dict({"options": f"-csearch_path={schema},public"})
    report = {"status": "failed", "members": members, "concurrent_clients": concurrency, "schema": schema,
        "redis_namespace": prefix, "model": "controlled_local_fake", "cloud_model_calls": 0,
        "limits": "In-process ASGI traffic; does not prove real LLM, multi-process HTTP or production throughput."}
    clients, metrics, metrics_lock = [], [], threading.Lock()
    http_active, http_peak = 0, 0
    database, redis_client = None, None
    with tempfile.TemporaryDirectory(prefix="rag-team-verification-") as workspace:
        os.environ.update({"APP_PROFILE": "server", "APP_ROLE": "api", "DATA_DIR": workspace,
            "DATABASE_URL": target.render_as_string(hide_password=False), "REDIS_URL": redis_url, "REDIS_KEY_PREFIX": prefix,
            "VECTOR_STORE_PROVIDER": "pgvector", "METADATA_STORE_PROVIDER": "postgresql", "CACHE_PROVIDER": "redis",
            "TASK_QUEUE_PROVIDER": "redis", "GRAPH_STORE_PROVIDER": "sqlalchemy", "RERANKER_PROVIDER": "none",
            "INITIAL_ADMIN_USERNAME": "verify-admin", "INITIAL_ADMIN_PASSWORD": "Synthetic-Admin-123!",
            "JWT_SECRET_KEY": "synthetic-verification-" + uuid.uuid4().hex,
            "LOCAL_OBJECT_STORE_DIR": str(Path(workspace) / "docs"), "CODECODE_AUTO_START": "false",
            "INDEX_WORKER_ENABLED": "false", "CONNECTOR_SCHEDULER_ENABLED": "false", "BACKUP_MONTHLY_ENABLED": "false",
            "GRAPH_COMMUNITY_MAINTENANCE_ENABLED": "false", "CONTEXT_JSON_EXPORT_ENABLED": "false",
            "AGENT_WORKSPACE_DIR": str(Path(workspace) / "agents"), "RAG_MAX_CONCURRENCY": "8", "RAG_QUEUE_TIMEOUT_SECONDS": "30"})
        try:
            from app.core import database, config
            from app.core.init_db import run_migrations, ensure_initial_admin
            from app.core.security import hash_password
            from app.models.user import User
            from app.models.index_job import KnowledgeIndexState, IndexJob
            from app.models.knowledge_document import KnowledgeDocument
            from app.models.knowledge_chunk import KnowledgeChunk
            from app.models.knowledge_graph import KgEntity
            from app.repositories.knowledge_base_repository import KnowledgeBaseRepository
            from app.repositories.index_job_repository import IndexJobRepository
            from app.adapters.redis import get_redis_client
            from rag.providers import factory
            from rag.providers.interfaces import ProviderIdentity, ProviderHealth, VectorIndexRef
            from rag.providers.reranker import PassthroughReranker

            class Embedding:
                def identity(self):
                    return ProviderIdentity(kind="embedding", provider="controlled", model="team-1024")
                def health(self):
                    return ProviderHealth.ok()
                def get_model(self):
                    return self
                def embed_query(self, value):
                    digest = hashlib.sha256(value.encode()).digest()
                    return [1.0 if index == digest[0] else 0.01 for index in range(1024)]
                def embed_documents(self, values):
                    return [self.embed_query(value) for value in values]

            class Model:
                active = peak = calls = 0
                lock = threading.Lock()
                def identity(self):
                    return ProviderIdentity(kind="llm", provider="controlled", model="fake-no-network")
                def health(self):
                    return ProviderHealth.ok()
                def get_model(self):
                    return self
                def generate(self, prompt):
                    with self.lock:
                        self.active += 1
                        self.peak = max(self.peak, self.active)
                        self.calls += 1
                    try:
                        time.sleep(.02)
                        alpha, bravo = "TEAM_ALPHA_ONLY" in prompt, "TEAM_BRAVO_ONLY" in prompt
                        if alpha and bravo:
                            raise AssertionError("Cross-knowledge-base context leaked")
                        return ("TEAM_ALPHA_ONLY" if alpha else "TEAM_BRAVO_ONLY" if bravo else "无可用资料") + "[来源1]"
                    finally:
                        with self.lock:
                            self.active -= 1
                def stream(self, prompt):
                    yield self.generate(prompt)

            embedding, model = Embedding(), Model()
            factory.get_embedding_provider = lambda: embedding
            factory.get_llm_provider = lambda purpose="answer": model
            factory.get_reranker = lambda: PassthroughReranker()
            from app.agents import factory as agent_factory
            def no_external_agent(*args, **kwargs):
                raise AssertionError("Unexpected external agent call in synthetic team verification")
            agent_factory.get_agent_backend = no_external_agent
            run_migrations(target_engine=database.engine)
            with database.SessionLocal() as db:
                ensure_initial_admin(db)
                owner = db.scalar(select(User).where(User.username == config.INITIAL_ADMIN_USERNAME))
                KnowledgeBaseRepository(db).ensure_default(owner.id)
                password_hash = hash_password("Synthetic-Member-123!")
                db.add_all(User(id=str(uuid.uuid4()), username=f"team-member-{index}", display_name=f"Member {index}",
                    password_hash=password_hash, role="user", is_active=True) for index in range(members))
                db.commit()
            from app.main import app
            from fastapi.testclient import TestClient
            from app.bootstrap.container import get_provider_container
            admin = TestClient(app, base_url="https://team.test", raise_server_exceptions=False)
            clients.append(admin)
            assert admin.post("/api/v1/auth/login", json={"username": config.INITIAL_ADMIN_USERNAME,
                "password": config.INITIAL_ADMIN_PASSWORD}).status_code == 200
            bases = []
            for label in ("alpha", "bravo"):
                response = admin.post("/api/v1/knowledge-bases", json={"name": f"Synthetic {label}", "slug": f"team-{label}"})
                assert response.status_code == 201, f"Knowledge-base creation HTTP {response.status_code}"
                bases.append(response.json()["id"])
            container = get_provider_container()
            redis_client = get_redis_client()
            assert redis_client.ping() and container.vector_store.identity().provider == "pgvector"
            from langchain_core.documents import Document
            with database.SessionLocal() as db:
                for index, kb in enumerate(bases):
                    marker = "TEAM_ALPHA_ONLY" if index == 0 else "TEAM_BRAVO_ONLY"
                    document_id = str(uuid.uuid4())
                    db.add(KnowledgeDocument(id=document_id, knowledge_base_id=kb, source_path=f"{kb}/seed.txt",
                        original_filename="seed.txt", file_hash="synthetic", file_size=10000, status="ready", chunk_count=500))
                    db.flush()
                    documents, identifiers = [], []
                    for number in range(500):
                        identifier = str(uuid.uuid4())
                        content = f"{marker} 团队资料内容，记录 {number}，原文为受控合成测试。"
                        db.add(KnowledgeChunk(id=identifier, knowledge_base_id=kb, document_id=document_id, file_hash="synthetic",
                            chunk_index=number, content=content, content_hash=hashlib.sha256(content.encode()).hexdigest()))
                        identifiers.append(identifier)
                        documents.append(Document(page_content=content, metadata={"chunk_id": identifier, "document_id": document_id,
                            "knowledge_base_id": kb, "source": "seed.txt", "chunk_index": number}))
                    collection = f"team_{index}"
                    from rag.indexing.profiles import embedding_fingerprint, index_ref
                    db.add(KnowledgeIndexState(knowledge_base_id=kb, active_collection_name=collection,
                                               embedding_fingerprint=embedding_fingerprint(embedding)))
                    db.add(KgEntity(knowledge_base_id=kb, entity_type="SYSTEM", canonical_name=marker, normalized_name=marker.lower()))
                    db.commit()
                    container.vector_store.open(embedding, index_ref(collection, kb, embedding)).add_documents(documents, ids=identifiers)
            users = []
            for index in range(members):
                kb = bases[index % 2]
                assert admin.put(f"/api/v1/knowledge-bases/{kb}/members", json={"username": f"team-member-{index}",
                    "role": "editor" if index < 2 else "viewer"}).status_code == 200
                member = TestClient(app, base_url="https://team.test", raise_server_exceptions=False)
                clients.append(member)
                response = member.post("/api/v1/auth/login", json={"username": f"team-member-{index}", "password": "Synthetic-Member-123!"})
                assert response.status_code == 200, f"Member {index} login HTTP {response.status_code}; check shared-IP limit"
                assert {item["id"] for item in member.get("/api/v1/knowledge-bases").json()} == {kb}
                users.append((member, kb, bases[(index + 1) % 2]))

            def request(client, kind, method, path, expected=200, **kwargs):
                nonlocal http_active, http_peak
                with metrics_lock:
                    http_active += 1
                    http_peak = max(http_peak, http_active)
                start = time.perf_counter()
                try:
                    response = getattr(client, method)(path, **kwargs)
                    accepted = (expected,) if isinstance(expected, int) else expected
                    with metrics_lock:
                        metrics.append({"kind": kind, "status": response.status_code, "expected": expected,
                            "ok": response.status_code in accepted, "elapsed_ms": (time.perf_counter() - start) * 1000})
                    assert response.status_code in accepted, f"{kind} HTTP {response.status_code}, expected {expected}"
                    return response
                finally:
                    with metrics_lock:
                        http_active -= 1

            # Warm both real retrieval pipelines before measuring steady traffic.
            for member, kb, foreign in users[:2]:
                assert member.post(f"/api/v1/knowledge-bases/{kb}/retrieval/debug", json={"query": "团队资料内容", "retrieval": {"channels": ["text"]}}).status_code == 200
            barrier = threading.Barrier(concurrency)
            def use_member(index):
                member, kb, foreign = users[index]
                if index < concurrency:
                    barrier.wait(timeout=20)
                base = f"/api/v1/knowledge-bases/{kb}"
                payload = {"query": "团队资料内容", "retrieval": {"channels": ["text"]}}
                answer = request(member, "ask", "post", base + "/ask", json=payload).json()
                assert answer["sources"] and all(source["knowledge_base_id"] == kb for source in answer["sources"])
                expected_marker = "TEAM_ALPHA_ONLY" if kb == bases[0] else "TEAM_BRAVO_ONLY"
                assert expected_marker in answer["answer"]
                retrieval = request(member, "retrieval", "post", base + "/retrieval/debug", json=payload).json()
                assert retrieval["sources"] and all(source["knowledge_base_id"] == kb for source in retrieval["sources"])
                graph = request(member, "graph", "get", base + "/knowledge-graph/stats").json()
                assert graph["entities"] == 1
                request(member, "cross_kb_denied", "post", f"/api/v1/knowledge-bases/{foreign}/ask", expected=(403, 404), json=payload)
                request(member, "cross_kb_denied", "get", f"/api/v1/knowledge-bases/{foreign}/knowledge-graph/stats", expected=(403, 404))

            upload_jobs, worker_results = [], []
            def upload_and_execute():
                member, kb, foreign = users[0]
                # A separate authenticated client avoids sharing cookie jars
                # between the upload thread and this member's query thread.
                uploader = TestClient(app, base_url="https://team.test", raise_server_exceptions=False)
                uploader.cookies.update(member.cookies)
                clients.append(uploader)
                for index in range(5):
                    response = request(uploader, "upload", "post", f"/api/v1/knowledge-bases/{kb}/documents", expected=202,
                        files={"file": synthetic_upload(index)})
                    upload_jobs.append(response.json()["job_id"])
                from app.services.index_worker import _process_job
                with ThreadPoolExecutor(max_workers=concurrency) as contenders:
                    worker_results.extend(contenders.map(lambda index: _process_job(upload_jobs[0], worker_id=f"verify-worker-{index}"), range(concurrency)))

            start = time.perf_counter()
            with ThreadPoolExecutor(max_workers=concurrency + 1) as pool:
                upload = pool.submit(upload_and_execute)
                for result in pool.map(use_member, range(members)):
                    pass
                upload.result()
            with database.SessionLocal() as db:
                rows = list(db.scalars(select(IndexJob).where(IndexJob.id.in_(upload_jobs))))
                assert len(upload_jobs) == len(set(upload_jobs)) == len(rows) == 5, (
                    f"Five distinct synthetic uploads must create five persisted jobs; received={len(upload_jobs)}, "
                    f"unique={len(set(upload_jobs))}, persisted={len(rows)}")
                processed = next(row for row in rows if row.id == upload_jobs[0])
                assert processed.status == "completed" and processed.attempt_count == 1, (
                    f"Contended job must complete exactly once; status={processed.status}, attempts={processed.attempt_count}")
                assert sum(bool(value) for value in worker_results) == 1, "Exactly one competing worker must own and execute the job"
            grouped = defaultdict(list)
            for item in metrics:
                grouped[item["kind"]].append(item)
            def summarize(items):
                elapsed = sorted(item["elapsed_ms"] for item in items)
                return {"requests": len(items), "p50_ms": round(statistics.median(elapsed), 3),
                    "p95_ms": round(elapsed[min(len(elapsed) - 1, int(len(elapsed) * .95))], 3),
                    "unexpected_errors": sum(not item["ok"] for item in items)}
            report.update(status="completed", wall_seconds=round(time.perf_counter() - start, 3),
                logged_in_members=len(users), vector_rows=1000, vector_dimensions=1024,
                peak_inflight_http=http_peak, configured_rag_slots=config.RAG_MAX_CONCURRENCY, peak_fake_generation=model.peak,
                endpoints={kind: summarize(items) for kind, items in grouped.items()},
                unexpected_error_rate=sum(not item["ok"] for item in metrics) / len(metrics),
                uploaded_jobs=len(upload_jobs), duplicate_executions=processed.attempt_count - 1,
                competing_workers=concurrency, successful_job_executions=sum(bool(value) for value in worker_results),
                shared_ip_login_limit=config.AUTH_LOGIN_IP_MAX_ATTEMPTS)
        finally:
            for client in clients:
                client.close()
            if redis_client is not None:
                keys = list(redis_client.scan_iter(match=prefix + ":*"))
                if keys:
                    redis_client.delete(*keys)
                report["redis_namespace_removed"] = not list(redis_client.scan_iter(match=prefix + ":*"))
                redis_client.close()
            if database is not None:
                database.engine.dispose()
            with admin_engine.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True))
            admin_engine.dispose()
            report["schema_removed"] = True
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres-url", default=os.getenv("TEST_POSTGRES_URL"))
    parser.add_argument("--redis-url", default=os.getenv("TEST_REDIS_URL"))
    parser.add_argument("--members", type=int, default=50)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if not args.postgres_url or not args.redis_url:
        parser.error("Provide explicit TEST_POSTGRES_URL and TEST_REDIS_URL (or matching flags)")
    try:
        report = verify(args.postgres_url, args.redis_url, members=args.members, concurrency=args.concurrency)
    except Exception as exc:
        report = {"status": "failed", "error_type": type(exc).__name__, "error": str(exc) if isinstance(exc, (AssertionError, ValueError))
            else "Controlled team verification failed; inspect isolated service/test logs"}
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
