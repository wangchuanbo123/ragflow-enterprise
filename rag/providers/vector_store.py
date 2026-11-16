from __future__ import annotations

from pathlib import Path


from rag.providers.interfaces import (
    ProviderHealth,
    ProviderIdentity,
    VectorIndexInfo,
    VectorIndexRef,
)


class ChromaVectorStoreProvider:
    """Embedded Chroma adapter used by the local profile."""

    def __init__(self, persist_dir: str | Path, client=None):
        self.persist_dir = str(persist_dir)
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import chromadb

            self._client = chromadb.PersistentClient(path=self.persist_dir)
        return self._client

    @staticmethod
    def _require_name(index_ref: VectorIndexRef) -> str:
        if not index_ref.name:
            raise ValueError("Vector index name is required")
        return index_ref.name

    def _collection(self, index_ref: VectorIndexRef):
        return self.client.get_collection(self._require_name(index_ref))

    def identity(self) -> ProviderIdentity:
        return ProviderIdentity(
            kind="vector_store",
            provider="chroma_embedded",
            capabilities=("metadata_filter", "versioned_collection"),
        )

    def open(self, embedding, index_ref: VectorIndexRef):
        from langchain_community.vectorstores import Chroma
        kwargs = {
            "persist_directory": self.persist_dir,
            "embedding_function": embedding,
        }
        if index_ref.name:
            kwargs["collection_name"] = index_ref.name
        store = Chroma(**kwargs)
        collection = store._collection
        metadata = dict(collection.metadata or {})
        changes = {}
        for key in ("index_version", "embedding_profile_id"):
            requested = getattr(index_ref, key)
            if not requested:
                continue
            if metadata.get(key) and metadata[key] != requested:
                raise ValueError(f"Vector collection {key} does not match")
            if not metadata.get(key):
                if collection.count():
                    raise ValueError("Legacy vector identity is unverified; rebuild into a new shadow collection before use")
                changes[key] = requested
        if changes:
            collection.modify(metadata={**metadata, **changes})
        return store

    def probe(self):
        try:
            self.client.heartbeat()
            return ProviderHealth.ok(provider="chroma_embedded")
        except Exception as exc:
            return ProviderHealth.fail(f"Chroma unavailable ({type(exc).__name__})")

    def health(
        self,
        index_ref: VectorIndexRef,
        expected_ids: set[str] | None = None,
    ) -> ProviderHealth:
        if not index_ref.name:
            return ProviderHealth.fail("No active vector index configured")

        try:
            try:
                collection = self._collection(index_ref)
            except Exception:
                return ProviderHealth.fail(
                    f"Active collection '{index_ref.name}' not found",
                    collection=index_ref.name,
                )

            vector_count = collection.count()
            metadata = {
                "collection": index_ref.name,
                "vector_count": vector_count,
                "provider": self.identity().provider,
            }
            if expected_ids is None:
                return ProviderHealth.ok(**metadata)

            chunk_count = len(expected_ids)
            metadata["chunk_count"] = chunk_count
            if vector_count != chunk_count:
                return ProviderHealth.fail(
                    f"Vector count ({vector_count}) != active chunk count ({chunk_count})",
                    **metadata,
                )

            vector_ids = set(collection.get(include=[])["ids"])
            if vector_ids != expected_ids:
                missing = sorted(expected_ids - vector_ids)[:5]
                unexpected = sorted(vector_ids - expected_ids)[:5]
                return ProviderHealth.fail(
                    "Vector IDs do not match active chunk IDs; "
                    f"missing={missing}, unexpected={unexpected}",
                    **metadata,
                )
            return ProviderHealth.ok(**metadata)
        except Exception as exc:
            return ProviderHealth.fail(str(exc)[:200])

    def list_indexes(self) -> list[VectorIndexInfo]:
        indexes: list[VectorIndexInfo] = []
        for item in self.client.list_collections():
            name = item if isinstance(item, str) else item.name
            collection = self.client.get_collection(name)
            indexes.append(
                VectorIndexInfo(
                    ref=VectorIndexRef(name=name),
                    count=collection.count(),
                )
            )
        return indexes

    def delete_index(self, index_ref: VectorIndexRef) -> None:
        self.client.delete_collection(self._require_name(index_ref))

    def count(self, index_ref: VectorIndexRef) -> int:
        return self._collection(index_ref).count()

    def list_ids(
        self,
        index_ref: VectorIndexRef,
        where=None,
    ) -> set[str]:
        kwargs = {"include": []}
        if where:
            kwargs["where"] = dict(where)
        return set(self._collection(index_ref).get(**kwargs)["ids"])

    def delete_vectors(
        self,
        index_ref: VectorIndexRef,
        *,
        ids=None,
        where=None,
    ) -> None:
        if ids is None and where is None:
            raise ValueError("Vector deletion requires IDs or a metadata filter")
        if ids is not None and not ids:
            return

        kwargs = {}
        if ids is not None:
            kwargs["ids"] = list(ids)
        if where is not None:
            kwargs["where"] = dict(where)
        self._collection(index_ref).delete(**kwargs)
