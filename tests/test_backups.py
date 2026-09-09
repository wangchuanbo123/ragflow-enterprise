from app.adapters.local import LocalObjectStore
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.knowledge_document import KnowledgeDocument
from app.models.knowledge_graph import KgEntity, KgEntityMention
from app.services.backup_service import BackupService


def _seed(db):
    kb = KnowledgeBase(id="kb-backup", name="Backup KB", slug="backup-kb", is_active=True)
    document = KnowledgeDocument(
        id="doc-backup", knowledge_base_id=kb.id, source_path="documents/doc.txt",
        original_filename="doc.txt", file_hash="a" * 64, file_size=5, status="indexed",
    )
    chunk = KnowledgeChunk(
        id="chunk-backup", knowledge_base_id=kb.id, document_id=document.id,
        file_hash=document.file_hash, chunk_index=0, content="hello", content_hash="b" * 64,
    )
    entity = KgEntity(
        id="entity-backup", knowledge_base_id=kb.id, entity_type="system",
        canonical_name="订单系统", normalized_name="订单系统",
    )
    mention = KgEntityMention(
        id="mention-backup", knowledge_base_id=kb.id, entity_id=entity.id,
        document_id=document.id, chunk_id=chunk.id, mention="订单系统", confidence=1.0,
    )
    db.add_all([kb, document, chunk, entity, mention])
    db.commit()
    return kb, document, entity


def test_knowledge_base_backup_is_versioned_and_restorable(db_engine, tmp_path, monkeypatch):
    from sqlalchemy.orm import Session

    store = LocalObjectStore(tmp_path / "objects")
    store.put_bytes("documents/doc.txt", b"hello")
    with Session(db_engine) as db:
        kb, document, _ = _seed(db)
        service = BackupService(db, kb.id, store)
        monkeypatch.setattr(service, "_queue_derived_rebuilds", lambda *args, **kwargs: None)
        document_id = document.id
        first = service.create("knowledge_base", label="stable")
        assert first.version_no == 1
        assert store.exists(first.object_key)

        document.original_filename = "changed.txt"
        db.commit()
        restored = service.restore(first.id, "user-id")
        assert restored.restored_at is not None
        assert db.get(KnowledgeDocument, document_id).original_filename == "doc.txt"
        assert len(service.list()) == 2  # selected version + automatic pre-restore protection


def test_document_graph_backup_rolls_back_mentions(db_engine, tmp_path, monkeypatch):
    from sqlalchemy.orm import Session

    with Session(db_engine) as db:
        kb, document, entity = _seed(db)
        service = BackupService(db, kb.id, LocalObjectStore(tmp_path / "objects"))
        monkeypatch.setattr(service, "_queue_derived_rebuilds", lambda *args, **kwargs: None)
        backup = service.create("document_graph", document_id=document.id)
        db.query(KgEntityMention).filter_by(document_id=document.id).delete()
        db.commit()
        service.restore(backup.id)
        restored = db.query(KgEntityMention).filter_by(document_id=document.id).one()
        assert restored.entity_id == entity.id
