"""对象存储读取 API。

为文档抽取出的图片等派生资源提供受控访问：必须通过登录态，并且只能读取
归属于当前用户可见知识库的文档对象。
"""

from __future__ import annotations

from pathlib import PurePosixPath

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.api.errors import forbidden, not_found
from app.core.database import get_db
from app.models.knowledge_document import KnowledgeDocument
from app.models.user import User
from app.repositories.knowledge_base_repository import KnowledgeBaseRepository

router = APIRouter(prefix="/objects", tags=["objects"])

_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".ico": "image/vnd.microsoft.icon",
    ".emf": "image/emf",
    ".wmf": "image/wmf",
    ".pdf": "application/pdf",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
}


def _guess_media_type(key: str) -> str:
    return _MEDIA_TYPES.get(PurePosixPath(key).suffix.lower(), "application/octet-stream")


def resolve_owner_document(db: Session, key: str) -> KnowledgeDocument | None:
    """根据对象 key 反查所属文档。

    key 形如 ``{document_id}/images/img-1.png`` 或
    ``knowledge-bases/{kb_id}/{document_id}/images/img-1.png``。
    """
    parts = [part for part in key.split("/") if part and part not in {".", ".."}]
    if not parts:
        return None
    if parts[0] == "knowledge-bases":
        if len(parts) < 3:
            return None
        document_id = parts[2]
    else:
        document_id = parts[0]
    return db.scalar(
        select(KnowledgeDocument).where(KnowledgeDocument.id == document_id)
    )


@router.get("/{key:path}")
def read_object(
    key: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not key or key.startswith("/") or ".." in key.split("/"):
        raise not_found("对象不存在")

    document = resolve_owner_document(db, key)
    if document is None:
        raise not_found("对象不存在")

    repository = KnowledgeBaseRepository(db)
    if not repository.can_read(
        document.knowledge_base_id,
        current_user.id,
        is_admin=current_user.role == "admin",
    ):
        raise forbidden("无权访问该对象")

    from app.bootstrap.container import get_provider_container

    object_store = get_provider_container().object_store
    try:
        if not object_store.exists(key):
            raise not_found("对象不存在")
        with object_store.materialize(key) as local_path:
            data = local_path.read_bytes()
    except Exception as exc:
        from app.api.errors import APIError

        if isinstance(exc, APIError):
            raise
        raise not_found("对象不存在") from exc

    return Response(
        content=data,
        media_type=_guess_media_type(key),
        headers={
            "Cache-Control": "private, max-age=3600",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'; style-src 'unsafe-inline'",
        },
    )
