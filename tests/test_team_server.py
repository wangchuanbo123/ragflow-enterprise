"""Server contracts that must hold before increasing process concurrency."""

from collections import OrderedDict
from datetime import datetime, timedelta
import os
import uuid

import pytest


def test_server_configuration_preserves_models_and_auth_without_overwriting(tmp_path):
    from scripts.configure_server import configure_server, read_env
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy/server.env.example").write_text("APP_PROFILE=server\nLLM_MODEL=default\n", encoding="utf-8")
    source = 'LLM_MODEL="configured"\nLLM_API_KEY="test-secret#literal"\nEMBEDDING_MODEL=bge-m3\nJWT_SECRET_KEY="existing-signing-secret"\nINITIAL_ADMIN_PASSWORD="existing-admin-password"\n'
    (tmp_path / ".env").write_text(source, encoding="utf-8")
    path = configure_server(tmp_path)
    first = path.read_bytes()
    settings = read_env(path)
    assert settings["LLM_MODEL"] == "configured"
    assert settings["LLM_API_KEY"] == "test-secret#literal"
    assert settings["EMBEDDING_MODEL"] == "bge-m3"
    assert settings["JWT_SECRET_KEY"] == "existing-signing-secret"
    assert len(settings["POSTGRES_PASSWORD"]) == 64
    assert settings["POSTGRES_PASSWORD"] != settings["REDIS_PASSWORD"]
    assert configure_server(tmp_path).read_bytes() == first
    assert (tmp_path / ".env").read_text(encoding="utf-8") == source


def test_server_rejects_process_local_storage():
    from app.bootstrap.container import validate_provider_configuration
    values = dict(profile="server", metadata_store="postgresql", vector_store="pgvector",
                  object_store="local", task_queue="redis", cache="redis", graph_store="sqlalchemy")
    validate_provider_configuration(**values)
    for field, value in (("metadata_store", "sqlite"), ("vector_store", "chroma_embedded"),
                         ("task_queue", "inline"), ("cache", "memory")):
        with pytest.raises(ValueError, match="Server profile requires"):
            validate_provider_configuration(**{**values, field: value})


def test_internal_agent_callback_requires_explicit_trust(monkeypatch):
    from app.agents.settings import AgentSettings
    monkeypatch.delenv("AGENT_TRUSTED_HTTP_HOSTS", raising=False)
    with pytest.raises(ValueError):
        AgentSettings(callback_url="http://api:8080/api/v1/agent-tools")
    monkeypatch.setenv("AGENT_TRUSTED_HTTP_HOSTS", "api")
    assert AgentSettings(callback_url="http://api:8080/api/v1/agent-tools").callback_url.startswith("http://api:")
    with pytest.raises(ValueError):
        AgentSettings(base_url="http://api:4096", password="secret")
    with pytest.raises(ValueError):
        AgentSettings(callback_url="http://api.attacker.test:8080/api/v1/agent-tools")


def test_loopback_model_addresses_are_mapped_to_the_container_host(tmp_path):
    from scripts.configure_server import configure_server, read_env
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy/server.env.example").write_text("APP_PROFILE=server\n", encoding="utf-8")
    (tmp_path / ".env").write_text("LLM_BASE_URL=http://127.0.0.1:1244/v2\nOLLAMA_BASE_URL=http://localhost:11434\nEMBEDDING_BASE_URL=https://provider.example/v1\n", encoding="utf-8")
    settings = read_env(configure_server(tmp_path))
    assert settings["LLM_BASE_URL"] == "http://host.docker.internal:1244/v2"
    assert settings["OLLAMA_BASE_URL"] == "http://host.docker.internal:11434"
    assert settings["EMBEDDING_BASE_URL"] == "https://provider.example/v1"


def test_legacy_short_seed_password_does_not_break_server_boot(tmp_path):
    from scripts.configure_server import configure_server, read_env
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy/server.env.example").write_text("APP_PROFILE=server\n", encoding="utf-8")
    original = "INITIAL_ADMIN_PASSWORD=legacy\n"
    (tmp_path / ".env").write_text(original, encoding="utf-8")
    settings = read_env(configure_server(tmp_path))
    assert len(settings["INITIAL_ADMIN_PASSWORD"]) >= 12
    assert (tmp_path / ".env").read_text(encoding="utf-8") == original


def test_deployment_identity_is_persisted_and_idempotent(db_engine):
    from app.core.database import SessionLocal
    from app.services.deployment_identity import read_deployment_identity, ensure_deployment_identity
    with SessionLocal() as db:
        assert read_deployment_identity(db) is None
        identifier = ensure_deployment_identity(db)
        assert uuid.UUID(identifier)
        assert ensure_deployment_identity(db) == identifier
    with SessionLocal() as db:
        assert read_deployment_identity(db) == identifier


def test_storage_preflight_catches_missing_and_absolute_legacy_references(client, tmp_path):
    from app.core.database import SessionLocal
    from app.models.knowledge_document import KnowledgeDocument
    from app.adapters.local import LocalObjectStore
    from scripts.verify_server_storage import verify_objects
    store = LocalObjectStore(tmp_path / "objects")
    with SessionLocal() as db:
        db.add(KnowledgeDocument(source_path="doc.txt", original_filename="doc.txt", file_hash="hash", file_size=3, status="ready"))
        db.commit()
        with pytest.raises(ValueError, match="missing=1"):
            verify_objects(db, store)
        store.put_bytes("doc.txt", b"abc")
        assert verify_objects(db, store)["object_references"] == 1
        db.add(KnowledgeDocument(source_path="C:\\private\\old.txt", original_filename="old.txt", file_hash="hash2", file_size=3, status="ready"))
        db.commit()
        with pytest.raises(ValueError, match="unsafe_or_absolute=1"):
            verify_objects(db, store)


def test_object_import_is_verified_and_never_overwrites_a_different_object(tmp_path):
    from scripts.import_legacy_objects import import_objects
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    (source / "doc.txt").write_text("original", encoding="utf-8")
    assert import_objects(source, target)["copied"] == 1
    assert import_objects(source, target)["verified_existing"] == 1
    (target / "doc.txt").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="refusing to overwrite"):
        import_objects(source, target)
    assert (target / "doc.txt").read_text() == "changed"
    assert (source / "doc.txt").read_text() == "original"


def test_bm25_cache_eviction_releases_loaded_documents(monkeypatch):
    from rag.retrievers import bm25_retriever as module
    monkeypatch.setattr(module, "_bm25_by_knowledge_base", OrderedDict())
    monkeypatch.setattr(module, "BM25_MAX_CACHE_KNOWLEDGE_BASES", 1)
    first = module.get_bm25_retriever("one")
    first._built = True
    first._docs = [object()]
    module.get_bm25_retriever("two")
    assert first._docs == [] and not first._built
    assert list(module._bm25_by_knowledge_base) == ["two"]


def test_scoped_bm25_preserves_corpus_scores_and_top_k(monkeypatch):
    from langchain_core.documents import Document
    from rank_bm25 import BM25L
    from rag.retrievers import bm25_retriever as module
    monkeypatch.setattr(module, "_tokenize", str.split)
    retriever = module.ChunkBM25Retriever("test")
    retriever._docs = [Document(page_content=text, metadata={"document_id": scope})
                       for text, scope in [("alpha alpha", "hidden"), ("alpha beta", "visible"), ("beta", "visible")]]
    retriever._bm25 = BM25L([item.page_content.split() for item in retriever._docs])
    retriever._built = True
    result = retriever.get_relevant_documents("alpha", top_k=1, allowed_document_ids={"visible"})
    assert len(result) == 1 and result[0].metadata["document_id"] == "visible"
    assert result[0].metadata["bm25_score"] == retriever._bm25.get_scores(["alpha"])[1]
    assert retriever.get_relevant_documents("alpha", top_k=0) == []


def test_scheduler_recovers_only_abandoned_streams(db_engine, monkeypatch):
    url = os.getenv("TEST_REDIS_URL")
    if not url:
        pytest.skip("TEST_REDIS_URL required")
    from app.core import config, database
    from app.models.user import User
    from app.models.conversation import Conversation
    from app.models.message import Message
    from app.repositories.knowledge_base_repository import KnowledgeBaseRepository
    from app.services.stream_lifecycle import stream_lease, recover_abandoned_streams
    from app.adapters.redis import get_redis_client
    monkeypatch.setattr(config, "REDIS_URL", url)
    monkeypatch.setattr(config, "REDIS_KEY_PREFIX", "stream-test:" + uuid.uuid4().hex)
    monkeypatch.setattr(config, "CACHE_PROVIDER", "redis")
    lease = None
    try:
        with database.SessionLocal() as db:
            user = User(username="owner", password_hash="hash", role="admin")
            db.add(user)
            db.commit()
            kb = KnowledgeBaseRepository(db).ensure_default(user.id)
            conversation = Conversation(user_id=user.id, knowledge_base_id=kb.id, title="streams")
            db.add(conversation)
            db.commit()
            old = datetime.utcnow() - timedelta(minutes=10)
            rows = [Message(conversation_id=conversation.id, knowledge_base_id=kb.id,
                            role="assistant", content="checkpoint", status="streaming", created_at=created)
                    for created in (old, old, datetime.utcnow())]
            db.add_all(rows)
            db.commit()
            lease = stream_lease(rows[0].id)
            assert recover_abandoned_streams(db) == 1
            db.expire_all()
            assert [row.status for row in rows] == ["streaming", "interrupted", "streaming"]
    finally:
        if lease is not None:
            lease.close()
        client = get_redis_client()
        keys = list(client.scan_iter(match=config.REDIS_KEY_PREFIX + ":*"))
        if keys:
            client.delete(*keys)
