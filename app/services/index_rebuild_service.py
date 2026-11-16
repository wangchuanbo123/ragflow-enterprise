"""Rebuild only registered originals, never an arbitrary object-store scan."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import shutil
from tempfile import TemporaryDirectory

from app.core.knowledge_bases import object_prefix_for_knowledge_base
from app.repositories.knowledge_repository import KnowledgeRepository
from rag.errors import DocumentValidationError
from rag.loaders.document_loader import SUPPORTED_EXTENSIONS


def registered_rebuild_documents(db, knowledge_base_id):
    return [
        document for document in KnowledgeRepository(db, knowledge_base_id).list_documents()
        if document.status not in {"deleted", "pending_delete"}
    ]


def _checked_key(document, knowledge_base_id):
    key = document.source_path
    if not isinstance(key, str) or not key or "\\" in key or "\x00" in key or ":" in key:
        raise DocumentValidationError("已登记原件路径无效")
    logical = PurePosixPath(key)
    if logical.is_absolute() or any(part in {"", ".", "..", ".versions"} for part in key.split("/")):
        raise DocumentValidationError("已登记原件路径无效")
    prefix = object_prefix_for_knowledge_base(knowledge_base_id)
    belongs = key.startswith(f"{prefix}/") if prefix else not key.startswith("knowledge-bases/")
    if document.knowledge_base_id != knowledge_base_id or not belongs:
        raise DocumentValidationError("已登记原件不属于所选知识库")
    if logical.suffix.lower() not in SUPPORTED_EXTENSIONS or logical.name.startswith("~$"):
        raise DocumentValidationError("已登记原件格式不受支持")
    return key


def inspect_rebuild_originals(db, knowledge_base_id, object_store):
    """Read-only inventory; no embeddings, vectors, index state writes or parsing."""
    documents = registered_rebuild_documents(db, knowledge_base_id)
    errors = []
    for document in documents:
        try:
            key = _checked_key(document, knowledge_base_id)
            if not object_store.exists(key):
                raise DocumentValidationError("原件不存在")
        except Exception as exc:
            # Operators can identify the document without exposing storage paths
            # or provider exception text (which can contain credentials).
            reason = str(exc) if isinstance(exc, DocumentValidationError) else "无法读取原件"
            errors.append({"document_id": document.id, "reason": reason})
    if not documents:
        errors.append({"reason": "知识库没有可重建的已登记文档"})
    return {
        "knowledge_base_id": knowledge_base_id,
        "source_kind": "registered_objects",
        "documents_to_rebuild": len(documents),
        "registered_original_bytes": sum(max(document.file_size or 0, 0) for document in documents),
        "ready": not errors,
        "error_count": len(errors),
        "errors": errors[:20],
        "preserves_previous_collection": True,
    }


def rebuild_registered_originals(manager, object_store, *, job_id, cancelled=None):
    """Stage an exact snapshot of one KB's originals under the worker's KB lock."""
    from app.agents.contracts import AgentCancelledError

    def check_cancelled():
        if cancelled is not None and cancelled():
            raise AgentCancelledError("索引重建已取消，活动索引保持不变")

    documents = registered_rebuild_documents(manager.db, manager.knowledge_base_id)
    if not documents:
        raise DocumentValidationError("知识库没有可重建的已登记文档")
    with TemporaryDirectory(prefix="rag-rebuild-") as directory:
        root = Path(directory).resolve()
        for document in documents:
            check_cancelled()
            key = _checked_key(document, manager.knowledge_base_id)
            target = root.joinpath(*PurePosixPath(key).parts).resolve()
            if not target.is_relative_to(root):
                raise DocumentValidationError("已登记原件路径无效")
            try:
                with object_store.materialize(key) as original:
                    if not Path(original).is_file():
                        raise FileNotFoundError(key)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(original, target)
            except Exception as exc:
                raise DocumentValidationError(
                    f"无法读取文档 {document.id} 的原件，活动索引保持不变"
                ) from exc
        check_cancelled()
        return manager.rebuild(root, job_id=job_id, cancelled=cancelled)
