"""Refuse completion when migrated object references cannot work on the server."""

import json
from pathlib import PurePosixPath, PureWindowsPath

from sqlalchemy import select


def verify_objects(db, object_store) -> dict:
    from app.models.knowledge_document import KnowledgeDocument
    from app.models.document_management import DocumentVersion
    from app.models.backup import KnowledgeBackup
    from app.models.code_graph import CodeFile
    from app.models.knowledge_chunk import KnowledgeChunk
    from rag.indexing.image_metadata import decode_images
    keys = set()
    invalid = 0

    def remember(key):
        nonlocal invalid
        if not key:
            return
        if not isinstance(key, str) or PurePosixPath(key).is_absolute() or PureWindowsPath(key).is_absolute() or "\\" in key:
            invalid += 1
            return
        if any(part in {".", ".."} for part in PurePosixPath(key).parts):
            invalid += 1
            return
        keys.add(key)

    for column in (KnowledgeDocument.source_path, DocumentVersion.object_key, KnowledgeBackup.object_key, CodeFile.object_key):
        for key in db.scalars(select(column).execution_options(yield_per=1000)):
            remember(key)
    for metadata in db.scalars(select(KnowledgeChunk.metadata_json).execution_options(yield_per=1000)):
        for image in decode_images((metadata or {}).get("images")):
            remember(image.get("object_key"))
    missing = sum(not object_store.exists(key) for key in keys)
    if invalid or missing:
        raise ValueError(f"Migrated object references need repair: unsafe_or_absolute={invalid}, missing={missing}; original sources preserved")
    return {"status": "verified", "object_references": len(keys)}


def main() -> int:
    from app.bootstrap.container import get_provider_container
    container = get_provider_container()
    with container.metadata.session() as db:
        report = verify_objects(db, container.object_store)
        from scripts.migrate_vectors_to_pgvector import collection_owners
        from rag.providers.interfaces import VectorIndexRef
        owners = collection_owners(db.get_bind())
        for name, kb in owners.items():
            result = container.vector_store.health(VectorIndexRef(name=name, knowledge_base_id=kb))
            if result.status != "ok":
                raise ValueError("Migrated active vector collection is unavailable; source or vector import needs repair")
    from app.services.readiness_service import _check_vector_store
    if _check_vector_store(container.vector_store).get("status") != "ok":
        raise ValueError("Migrated document vector IDs do not match active chunks")
    report["active_vector_collections"] = len(owners)
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
