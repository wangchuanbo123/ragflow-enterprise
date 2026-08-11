from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.documents import Document
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.adapters.local import (
    InlineTaskQueue,
    LocalObjectStore,
    SQLAlchemyMetadataSessionProvider,
)
from rag.providers.interfaces import VectorIndexRef
from rag.providers.vector_store import ChromaVectorStoreProvider


def test_local_object_store_uses_logical_keys_and_materializes(tmp_path):
    store = LocalObjectStore(tmp_path)

    ref = store.put_bytes("kb-1/document.txt", b"hello")

    assert ref.key == "kb-1/document.txt"
    assert ref.size == 5
    assert store.exists(ref.key)
    with store.materialize(ref.key) as path:
        assert path == (tmp_path / "kb-1" / "document.txt").resolve()
        assert path.read_bytes() == b"hello"
    assert store.health().status == "ok"


@pytest.mark.parametrize("key", ["../escape.txt", "/absolute.txt", "a\\b.txt", ""])
def test_local_object_store_rejects_unsafe_keys(tmp_path, key):
    store = LocalObjectStore(tmp_path)

    with pytest.raises(ValueError):
        store.put_bytes(key, b"unsafe")


def test_inline_task_queue_is_a_database_job_wakeup_signal():
    queue = InlineTaskQueue()

    assert queue.wait(0) is False
    queue.enqueue("job-1")
    assert queue.wait(0) is True
    assert queue.wait(0) is False
    assert queue.health().status == "ok"


def test_sqlalchemy_metadata_adapter_opens_sessions_and_checks_health():
    engine = create_engine("sqlite://", future=True)
    session_factory = sessionmaker(bind=engine, future=True)
    adapter = SQLAlchemyMetadataSessionProvider(session_factory, "sqlite")

    with adapter.session() as db:
        assert db.execute(__import__("sqlalchemy").text("SELECT 1")).scalar_one() == 1

    assert adapter.health().status == "ok"
    assert adapter.identity().provider == "sqlite"


class _NativeCollection:
    def __init__(self, name):
        self.name = name
        self.records = {
            "a": Document(page_content="a", metadata={"document_id": "doc-1"}),
            "b": Document(page_content="b", metadata={"document_id": "doc-2"}),
        }

    def count(self):
        return len(self.records)

    def get(self, include=None, where=None):
        del include
        ids = list(self.records)
        if where:
            ids = [
                item_id
                for item_id, item in self.records.items()
                if all(item.metadata.get(key) == value for key, value in where.items())
            ]
        return {"ids": ids}

    def delete(self, ids=None, where=None):
        if ids:
            for item_id in ids:
                self.records.pop(item_id, None)
        if where:
            for item_id, item in list(self.records.items()):
                if all(item.metadata.get(key) == value for key, value in where.items()):
                    self.records.pop(item_id, None)


class _NativeClient:
    def __init__(self):
        self.collections = {"documents": _NativeCollection("documents")}

    def get_collection(self, name):
        if name not in self.collections:
            raise ValueError(name)
        return self.collections[name]

    def list_collections(self):
        return [SimpleNamespace(name=name) for name in self.collections]

    def delete_collection(self, name):
        del self.collections[name]


def test_chroma_management_is_hidden_behind_vector_provider_contract(tmp_path):
    client = _NativeClient()
    provider = ChromaVectorStoreProvider(tmp_path, client=client)
    ref = VectorIndexRef(name="documents")

    assert provider.count(ref) == 2
    assert provider.list_ids(ref, where={"document_id": "doc-1"}) == {"a"}
    assert provider.list_indexes()[0].count == 2

    provider.delete_vectors(ref, ids=["a"])
    assert provider.list_ids(ref) == {"b"}
    provider.delete_index(ref)
    assert provider.list_indexes() == []

