"""Knowledge-base lifecycle and scoped document endpoints."""

from fastapi import APIRouter, Depends, File, Query, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.api.errors import bad_request, forbidden, not_found
from app.core.config import (
    MAX_BATCH_UPLOAD_FILES,
    MAX_BATCH_UPLOAD_TOTAL_MB,
    MAX_DOCUMENT_SIZE_MB,
)
from app.core.database import get_db
from app.models.user import User
from app.repositories.knowledge_base_repository import KnowledgeBaseRepository
from app.repositories.index_job_repository import IndexJobRepository
from app.schemas.document import DocumentOut, IndexJobEventOut, IndexJobOut
from app.schemas.document_management import (
    BatchDocumentRequest,
    ChunkDetailOut,
    ChunkNeighborOut,
    ChunkOut,
    DocumentTagsUpdate,
    DocumentVersionOut,
    FolderCreate,
    FolderOut,
    SearchHitOut,
    TagCreate,
    TagOut,
)
from app.schemas.knowledge_base import (
    DataSourceCreate,
    DataSourceOut,
    DataSourceSyncOut,
    DataSourceUpdate,
    KnowledgeBaseCreate,
    KnowledgeBaseMemberOut,
    KnowledgeBaseMemberUpsert,
    KnowledgeBaseOut,
    KnowledgeBaseUpdate,
)
from app.schemas.knowledge_graph import (
    DocumentSubgraphOut,
    EntityOut,
    EntitySubgraphOut,
    GraphCorrectionCreate,
    GraphCorrectionOut,
    GraphStatsOut,
    NeighborRelationOut,
)
from app.schemas.rag import AskRequest, AskResponse, RetrievalDebugResponse
from app.services.document_index_service import DocumentIndexService

router = APIRouter(prefix="/knowledge-bases", tags=["knowledge-bases"])
_UPLOAD_READ_CHUNK_BYTES = 1024 * 1024


def _is_admin(user: User) -> bool:
    return user.role == "admin"


def _require_read(db: Session, knowledge_base_id: str, user: User) -> None:
    if not KnowledgeBaseRepository(db).can_read(
        knowledge_base_id,
        user.id,
        is_admin=_is_admin(user),
    ):
        raise not_found("知识库不存在")


def _require_edit(db: Session, knowledge_base_id: str, user: User) -> None:
    if not KnowledgeBaseRepository(db).can_edit(
        knowledge_base_id,
        user.id,
        is_admin=_is_admin(user),
    ):
        raise forbidden("没有该知识库的编辑权限")


def _require_owner(db: Session, knowledge_base_id: str, user: User) -> None:
    repository = KnowledgeBaseRepository(db)
    knowledge_base = repository.get(knowledge_base_id)
    if knowledge_base is None:
        raise not_found("知识库不存在")
    if user.role == "admin":
        return
    if repository.member_role(knowledge_base_id, user.id) != "owner":
        raise forbidden("仅知识库所有者可管理成员")


@router.get("", response_model=list[KnowledgeBaseOut])
def list_knowledge_bases(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return KnowledgeBaseRepository(db).list_for_user(
        current_user.id,
        is_admin=_is_admin(current_user),
    )


@router.post("", response_model=KnowledgeBaseOut, status_code=201)
def create_knowledge_base(
    payload: KnowledgeBaseCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not _is_admin(current_user):
        raise forbidden("仅管理员可创建知识库")
    try:
        knowledge_base = KnowledgeBaseRepository(db).create(
            name=payload.name,
            slug=payload.slug,
            owner_id=current_user.id,
        )
        knowledge_base.description = payload.description
        db.commit()
        db.refresh(knowledge_base)
        return knowledge_base
    except ValueError as exc:
        raise bad_request(str(exc)) from exc


@router.get("/{knowledge_base_id}", response_model=KnowledgeBaseOut)
def get_knowledge_base(
    knowledge_base_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_read(db, knowledge_base_id, current_user)
    knowledge_base = KnowledgeBaseRepository(db).get(knowledge_base_id)
    if knowledge_base is None:
        raise not_found("知识库不存在")
    return knowledge_base


@router.patch("/{knowledge_base_id}", response_model=KnowledgeBaseOut)
def update_knowledge_base(
    knowledge_base_id: str,
    payload: KnowledgeBaseUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_owner(db, knowledge_base_id, current_user)
    repository = KnowledgeBaseRepository(db)
    knowledge_base = repository.get(knowledge_base_id)
    if knowledge_base is None:
        raise not_found("知识库不存在")
    supplied = payload.model_fields_set
    try:
        return repository.update(
            knowledge_base,
            name=payload.name,
            slug=payload.slug,
            description=payload.description,
            description_supplied="description" in supplied,
        )
    except ValueError as exc:
        raise bad_request(str(exc)) from exc


@router.delete("/{knowledge_base_id}", status_code=status.HTTP_204_NO_CONTENT)
def archive_knowledge_base(
    knowledge_base_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_owner(db, knowledge_base_id, current_user)
    repository = KnowledgeBaseRepository(db)
    knowledge_base = repository.get(knowledge_base_id)
    if knowledge_base is None:
        raise not_found("知识库不存在")
    try:
        repository.archive(knowledge_base)
    except ValueError as exc:
        raise bad_request(str(exc)) from exc


@router.get(
    "/{knowledge_base_id}/members",
    response_model=list[KnowledgeBaseMemberOut],
)
def list_knowledge_base_members(
    knowledge_base_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_read(db, knowledge_base_id, current_user)
    return KnowledgeBaseRepository(db).list_members(knowledge_base_id)


@router.put(
    "/{knowledge_base_id}/members",
    response_model=KnowledgeBaseMemberOut,
)
def upsert_knowledge_base_member(
    knowledge_base_id: str,
    payload: KnowledgeBaseMemberUpsert,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_owner(db, knowledge_base_id, current_user)
    try:
        return KnowledgeBaseRepository(db).upsert_member(
            knowledge_base_id,
            payload.username,
            payload.role,
        )
    except ValueError as exc:
        raise bad_request(str(exc)) from exc


@router.delete(
    "/{knowledge_base_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def remove_knowledge_base_member(
    knowledge_base_id: str,
    user_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_owner(db, knowledge_base_id, current_user)
    try:
        removed = KnowledgeBaseRepository(db).remove_member(
            knowledge_base_id,
            user_id,
        )
    except ValueError as exc:
        raise bad_request(str(exc)) from exc
    if not removed:
        raise not_found("知识库成员不存在")
    return None


@router.get(
    "/{knowledge_base_id}/sources",
    response_model=list[DataSourceOut],
)
def list_data_sources(
    knowledge_base_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_read(db, knowledge_base_id, current_user)
    return KnowledgeBaseRepository(db).list_data_sources(knowledge_base_id)


@router.post(
    "/{knowledge_base_id}/sources",
    response_model=DataSourceOut,
    status_code=status.HTTP_201_CREATED,
)
def create_data_source(
    knowledge_base_id: str,
    payload: DataSourceCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_edit(db, knowledge_base_id, current_user)
    try:
        return KnowledgeBaseRepository(db).create_data_source(
            knowledge_base_id,
            source_type=payload.source_type,
            name=payload.name,
            connection_ref=payload.connection_ref,
            config=payload.config,
            sync_interval_minutes=payload.sync_interval_minutes,
            deletion_policy=payload.deletion_policy,
        )
    except ValueError as exc:
        raise bad_request(str(exc)) from exc


@router.patch(
    "/{knowledge_base_id}/sources/{source_id}",
    response_model=DataSourceOut,
)
def update_data_source(
    knowledge_base_id: str,
    source_id: str,
    payload: DataSourceUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_edit(db, knowledge_base_id, current_user)
    source = KnowledgeBaseRepository(db).update_data_source(
        knowledge_base_id,
        source_id,
        **payload.model_dump(exclude_unset=True),
    )
    if source is None:
        raise not_found("数据源不存在")
    return source


@router.delete(
    "/{knowledge_base_id}/sources/{source_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_data_source(
    knowledge_base_id: str,
    source_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_edit(db, knowledge_base_id, current_user)
    if not KnowledgeBaseRepository(db).delete_data_source(
        knowledge_base_id,
        source_id,
    ):
        raise not_found("数据源不存在")
    return None


@router.post(
    "/{knowledge_base_id}/sources/{source_id}/check",
    response_model=DataSourceOut,
)
def check_data_source_configuration(
    knowledge_base_id: str,
    source_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_edit(db, knowledge_base_id, current_user)
    from app.services.data_source_service import DataSourceService

    source = DataSourceService(
        db,
        knowledge_base_id,
    ).check_configuration(source_id)
    if source is None:
        raise not_found("数据源不存在")
    return source


@router.post(
    "/{knowledge_base_id}/sources/{source_id}/sync",
    response_model=DataSourceSyncOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def sync_data_source(
    knowledge_base_id: str,
    source_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_edit(db, knowledge_base_id, current_user)
    from app.services.data_source_service import DataSourceService

    try:
        result = DataSourceService(db, knowledge_base_id).request_sync(
            source_id,
            current_user.id,
        )
    except ValueError as exc:
        raise bad_request(str(exc)) from exc
    if result is None:
        raise not_found("数据源不存在")
    return result


@router.get("/{knowledge_base_id}/documents", response_model=list[DocumentOut])
def list_documents(
    knowledge_base_id: str,
    folder_id: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    tag_ids: list[str] = Query(default=[]),
    filename: str | None = Query(default=None, max_length=255),
    metadata_key: str | None = Query(default=None, max_length=128),
    metadata_value: str | None = Query(default=None, max_length=255),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_read(db, knowledge_base_id, current_user)
    metadata_filters = None
    if metadata_key is not None and metadata_value is not None:
        metadata_filters = {metadata_key: metadata_value}
    return DocumentIndexService(db, knowledge_base_id).list_documents(
        folder_id=folder_id,
        status=status_filter,
        tag_ids=tag_ids,
        filename_contains=filename,
        metadata_filters=metadata_filters,
    )


@router.get("/{knowledge_base_id}/documents/tag-map", response_model=dict[str, list[str]])
def document_tag_map(
    knowledge_base_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_read(db, knowledge_base_id, current_user)
    return DocumentIndexService(
        db, knowledge_base_id
    ).management_repo.document_tag_map()


@router.get(
    "/{knowledge_base_id}/documents/{document_id}",
    response_model=DocumentOut,
)
def get_document(
    knowledge_base_id: str,
    document_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_read(db, knowledge_base_id, current_user)
    document = DocumentIndexService(db, knowledge_base_id).get_document(document_id)
    if document is None:
        raise not_found("文档不存在")
    return document


@router.post(
    "/{knowledge_base_id}/documents",
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_document(
    knowledge_base_id: str,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_edit(db, knowledge_base_id, current_user)
    max_bytes = MAX_DOCUMENT_SIZE_MB * 1024 * 1024
    parts: list[bytes] = []
    total = 0
    while chunk := await file.read(_UPLOAD_READ_CHUNK_BYTES):
        total += len(chunk)
        if total > max_bytes:
            raise bad_request(f"File too large: more than {MAX_DOCUMENT_SIZE_MB}MB")
        parts.append(chunk)

    try:
        job, document_id = DocumentIndexService(
            db,
            knowledge_base_id,
        ).create_upload_job(
            file.filename or "upload.txt",
            b"".join(parts),
            current_user.id,
        )
    except Exception as exc:
        from rag.errors import DocumentTooLargeError, DocumentValidationError

        if isinstance(exc, (DocumentValidationError, DocumentTooLargeError)):
            raise bad_request(str(exc)) from exc
        raise
    return {"job_id": job.id, "document_id": document_id, "status": "accepted"}


@router.post(
    "/{knowledge_base_id}/documents/batch-upload",
    status_code=status.HTTP_202_ACCEPTED,
)
async def batch_upload_documents(
    knowledge_base_id: str,
    files: list[UploadFile] = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_edit(db, knowledge_base_id, current_user)
    if not files:
        raise bad_request("At least one file is required")
    if len(files) > MAX_BATCH_UPLOAD_FILES:
        raise bad_request(
            f"A batch can contain at most {MAX_BATCH_UPLOAD_FILES} files"
        )

    max_file_bytes = MAX_DOCUMENT_SIZE_MB * 1024 * 1024
    max_total_bytes = MAX_BATCH_UPLOAD_TOTAL_MB * 1024 * 1024
    total_bytes = 0
    uploads: list[tuple[str, bytes]] = []
    for file in files:
        parts: list[bytes] = []
        file_bytes = 0
        while chunk := await file.read(_UPLOAD_READ_CHUNK_BYTES):
            file_bytes += len(chunk)
            total_bytes += len(chunk)
            if total_bytes > max_total_bytes:
                raise bad_request(
                    f"Batch is larger than {MAX_BATCH_UPLOAD_TOTAL_MB}MB"
                )
            parts.append(chunk)
            if file_bytes > max_file_bytes:
                break
        uploads.append((file.filename or "upload.txt", b"".join(parts)))

    try:
        job, document_ids, rejected = DocumentIndexService(
            db,
            knowledge_base_id,
        ).create_batch_upload_job(uploads, current_user.id)
    except Exception as exc:
        from rag.errors import DocumentTooLargeError, DocumentValidationError

        if isinstance(exc, (DocumentValidationError, DocumentTooLargeError)):
            raise bad_request(str(exc)) from exc
        raise
    return {
        "job_id": job.id,
        "document_ids": document_ids,
        "accepted": len(document_ids),
        "rejected": rejected,
        "status": "partial" if rejected else "accepted",
    }


@router.delete(
    "/{knowledge_base_id}/documents/{document_id}",
    status_code=status.HTTP_202_ACCEPTED,
)
def delete_document(
    knowledge_base_id: str,
    document_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_edit(db, knowledge_base_id, current_user)
    try:
        job = DocumentIndexService(db, knowledge_base_id).request_delete(
            document_id,
            current_user.id,
        )
    except Exception as exc:
        from rag.errors import DocumentValidationError

        if isinstance(exc, DocumentValidationError):
            raise not_found("文档不存在") from exc
        raise
    return {"job_id": job.id, "status": "accepted"}


@router.post(
    "/{knowledge_base_id}/documents/{document_id}/reindex",
    status_code=status.HTTP_202_ACCEPTED,
)
def reindex_document(
    knowledge_base_id: str,
    document_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_edit(db, knowledge_base_id, current_user)
    try:
        job = DocumentIndexService(db, knowledge_base_id).request_reindex(
            document_id,
            current_user.id,
        )
    except Exception as exc:
        from rag.errors import DocumentValidationError

        if isinstance(exc, DocumentValidationError):
            raise not_found("文档不存在") from exc
        raise
    return {"job_id": job.id, "status": "accepted"}


@router.post(
    "/{knowledge_base_id}/index-jobs/sync",
    status_code=status.HTTP_202_ACCEPTED,
)
def request_sync(
    knowledge_base_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_edit(db, knowledge_base_id, current_user)
    job = DocumentIndexService(db, knowledge_base_id).request_sync_job(current_user.id)
    return {"job_id": job.id, "status": "accepted"}


@router.get(
    "/{knowledge_base_id}/index-jobs",
    response_model=list[IndexJobOut],
)
def list_index_jobs(
    knowledge_base_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_read(db, knowledge_base_id, current_user)
    return IndexJobRepository(db, knowledge_base_id).list_jobs()


@router.get(
    "/{knowledge_base_id}/index-jobs/{job_id}",
    response_model=IndexJobOut,
)
def get_index_job(
    knowledge_base_id: str,
    job_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_read(db, knowledge_base_id, current_user)
    job = IndexJobRepository(db, knowledge_base_id).get(job_id)
    if job is None:
        raise not_found("任务不存在")
    return job


@router.get(
    "/{knowledge_base_id}/index-jobs/{job_id}/events",
    response_model=list[IndexJobEventOut],
)
def list_index_job_events(
    knowledge_base_id: str,
    job_id: str,
    after_id: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_read(db, knowledge_base_id, current_user)
    repository = IndexJobRepository(db, knowledge_base_id)
    if repository.get(job_id) is None:
        raise not_found("Index job not found")
    return repository.list_events(job_id, after_id=after_id, limit=limit)


@router.post(
    "/{knowledge_base_id}/index-jobs/{job_id}/cancel",
    response_model=IndexJobOut,
)
def cancel_index_job(
    knowledge_base_id: str,
    job_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_edit(db, knowledge_base_id, current_user)
    job = IndexJobRepository(db, knowledge_base_id).request_cancel(job_id)
    if job is None:
        raise not_found("Index job not found")
    return job


@router.post(
    "/{knowledge_base_id}/index-jobs/{job_id}/retry",
    response_model=IndexJobOut,
)
def retry_index_job(
    knowledge_base_id: str,
    job_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_edit(db, knowledge_base_id, current_user)
    try:
        return DocumentIndexService(db, knowledge_base_id).retry_job(job_id)
    except ValueError as exc:
        raise bad_request(str(exc)) from exc
    except Exception as exc:
        from rag.errors import DocumentValidationError

        if isinstance(exc, DocumentValidationError):
            raise not_found("Index job not found") from exc
        raise


@router.post("/{knowledge_base_id}/ask", response_model=AskResponse)
def ask_knowledge_base(
    knowledge_base_id: str,
    payload: AskRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_read(db, knowledge_base_id, current_user)
    from app.services.rag_service import ask_question

    return ask_question(
        payload.query,
        knowledge_base_id=knowledge_base_id,
        retrieval_options=payload.retrieval,
    )


@router.post(
    "/{knowledge_base_id}/retrieval/debug",
    response_model=RetrievalDebugResponse,
)
def debug_retrieval(
    knowledge_base_id: str,
    payload: AskRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_read(db, knowledge_base_id, current_user)
    from app.services.rag_service import inspect_retrieval

    return inspect_retrieval(
        payload.query,
        knowledge_base_id=knowledge_base_id,
        retrieval_options=payload.retrieval,
    )


@router.get(
    "/{knowledge_base_id}/knowledge-graph/stats",
    response_model=GraphStatsOut,
)
def graph_stats(
    knowledge_base_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_read(db, knowledge_base_id, current_user)
    from app.bootstrap.container import get_provider_container

    return GraphStatsOut(
        **get_provider_container().graph_store(db, knowledge_base_id).stats()
    )


@router.get(
    "/{knowledge_base_id}/knowledge-graph/entities",
    response_model=list[EntityOut],
)
def search_graph_entities(
    knowledge_base_id: str,
    query: str = Query(min_length=1, max_length=255),
    limit: int = Query(default=20, ge=1, le=100),
    graph_kind: str | None = Query(
        default=None,
        pattern="^(general|concept|functional|configuration|standard)$",
    ),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_read(db, knowledge_base_id, current_user)
    from app.bootstrap.container import get_provider_container

    fetch_limit = 100 if graph_kind else limit
    entities = get_provider_container().graph_store(
        db,
        knowledge_base_id,
    ).search_entities(query, fetch_limit)
    if graph_kind and graph_kind != "general":
        from rag.knowledge_graph.profiles import profile_type_names

        allowed_entities, _ = profile_type_names(graph_kind)
        entities = [
            entity for entity in entities
            if str(entity.get("entity_type") or "").upper() in allowed_entities
        ][:limit]
    return [EntityOut(**entity) for entity in entities]


@router.get(
    "/{knowledge_base_id}/knowledge-graph/entities/{entity_id}/neighbors",
    response_model=list[NeighborRelationOut],
)
def graph_neighbors(
    knowledge_base_id: str,
    entity_id: str,
    hops: int = Query(default=1, ge=1, le=2),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_read(db, knowledge_base_id, current_user)
    from app.bootstrap.container import get_provider_container

    relations = get_provider_container().graph_store(
        db,
        knowledge_base_id,
    ).get_neighbors([entity_id], hops, limit)
    return [NeighborRelationOut(**relation) for relation in relations]


@router.get(
    "/{knowledge_base_id}/knowledge-graph/subgraph",
    response_model=EntitySubgraphOut,
)
def entity_subgraph(
    knowledge_base_id: str,
    entity_ids: list[str] = Query(min_length=1, max_length=20),
    hops: int = Query(default=1, ge=1, le=2),
    offset: int = Query(default=0, ge=0, le=5000),
    limit: int = Query(default=100, ge=1, le=500),
    graph_kind: str | None = Query(
        default=None,
        pattern="^(general|concept|functional|configuration|standard)$",
    ),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Expand a bounded entity subgraph without loading the entire graph."""
    _require_read(db, knowledge_base_id, current_user)
    from app.bootstrap.container import get_provider_container
    from app.repositories.knowledge_graph_repository import KnowledgeGraphRepository

    repository = KnowledgeGraphRepository(db, knowledge_base_id)
    seed_entities = [repository.get_entity(entity_id) for entity_id in entity_ids]
    if any(entity is None for entity in seed_entities):
        raise not_found("一个或多个实体不存在")

    store = get_provider_container().graph_store(db, knowledge_base_id)
    relations = store.get_neighbors(
        entity_ids,
        hops,
        limit + 1,
        offset=offset,
    )
    page = relations[:limit]
    visible_entity_ids = set(entity_ids)
    for relation in page:
        visible_entity_ids.update(
            (relation["subject_id"], relation["object_id"])
        )
    from app.models.knowledge_graph import KgEntityMention

    mention_counts = dict(db.execute(
        select(KgEntityMention.entity_id, func.count(KgEntityMention.id))
        .where(
            KgEntityMention.knowledge_base_id == knowledge_base_id,
            KgEntityMention.entity_id.in_(visible_entity_ids),
        )
        .group_by(KgEntityMention.entity_id)
    ).all()) if visible_entity_ids else {}
    relation_ids = [relation["relation_id"] for relation in page]
    evidence_rows = store.get_relation_evidence(relation_ids)
    evidence_by_relation: dict[str, list[dict]] = {}
    for evidence in evidence_rows:
        evidence_by_relation.setdefault(evidence["relation_id"], []).append({
            "chunk_id": evidence["chunk_id"],
            "quote": evidence["quote"],
            "confidence": evidence["confidence"],
        })

    nodes: dict[str, dict] = {
        entity.id: {
            "id": entity.id,
            "label": entity.canonical_name,
            "entity_type": entity.entity_type,
            "description": entity.description,
            "mention_count": int(mention_counts.get(entity.id, 0)),
        }
        for entity in seed_entities
        if entity is not None
    }
    edges = []
    for relation in page:
        nodes[relation["subject_id"]] = {
            "id": relation["subject_id"],
            "label": relation["subject_name"],
            "entity_type": relation["subject_type"],
            "mention_count": int(mention_counts.get(relation["subject_id"], 0)),
        }
        nodes[relation["object_id"]] = {
            "id": relation["object_id"],
            "label": relation["object_name"],
            "entity_type": relation["object_type"],
            "mention_count": int(mention_counts.get(relation["object_id"], 0)),
        }
        edges.append({
            "id": relation["relation_id"],
            "source": relation["subject_id"],
            "target": relation["object_id"],
            "predicate": relation["predicate"],
            "confidence": relation["confidence"],
            "evidence": evidence_by_relation.get(relation["relation_id"], []),
            "manual": relation.get("manual", False),
            "correction_id": relation.get("correction_id"),
        })
    payload = {
        "seed_entity_ids": entity_ids,
        "graph_kind": graph_kind,
        "nodes": list(nodes.values()),
        "edges": edges,
        "offset": offset,
        "next_offset": offset + limit if len(relations) > limit else None,
    }
    if graph_kind and graph_kind != "general":
        from rag.knowledge_graph.profiles import project_subgraph

        return project_subgraph(payload, graph_kind)
    return payload


@router.get(
    "/{knowledge_base_id}/documents/{document_id}/subgraph",
    response_model=DocumentSubgraphOut,
)
def document_subgraph(
    knowledge_base_id: str,
    document_id: str,
    limit: int = Query(default=200, ge=1, le=1000),
    graph_kind: str | None = Query(
        default=None,
        pattern="^(general|concept|functional|configuration|standard)$",
    ),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return only graph facts backed by the selected document."""
    _require_read(db, knowledge_base_id, current_user)
    if DocumentIndexService(db, knowledge_base_id).get_document(document_id) is None:
        raise not_found("文档不存在")
    from app.bootstrap.container import get_provider_container

    payload = get_provider_container().graph_store(
        db,
        knowledge_base_id,
    ).get_document_subgraph(document_id, limit)
    if graph_kind and graph_kind != "general":
        from rag.knowledge_graph.profiles import project_subgraph

        return project_subgraph(payload, graph_kind)
    return payload


@router.get(
    "/{knowledge_base_id}/knowledge-graph/corrections",
    response_model=list[GraphCorrectionOut],
)
def list_graph_corrections(
    knowledge_base_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_read(db, knowledge_base_id, current_user)
    from app.repositories.knowledge_graph_repository import KnowledgeGraphRepository

    return KnowledgeGraphRepository(db, knowledge_base_id).list_corrections()


@router.post(
    "/{knowledge_base_id}/knowledge-graph/corrections",
    response_model=GraphCorrectionOut,
    status_code=status.HTTP_201_CREATED,
)
def create_graph_correction(
    knowledge_base_id: str,
    payload: GraphCorrectionCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_edit(db, knowledge_base_id, current_user)
    from app.services.graph_governance_service import GraphGovernanceService

    try:
        return GraphGovernanceService(db, knowledge_base_id).create_correction(
            action=payload.action,
            target_id=payload.target_id,
            payload=payload.payload,
            reason=payload.reason,
            created_by=current_user.id,
        )
    except ValueError as exc:
        raise bad_request(str(exc)) from exc


@router.post(
    "/{knowledge_base_id}/knowledge-graph/corrections/{correction_id}/revert",
    response_model=GraphCorrectionOut,
)
def revert_graph_correction(
    knowledge_base_id: str,
    correction_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_edit(db, knowledge_base_id, current_user)
    from app.services.graph_governance_service import GraphGovernanceService

    correction = GraphGovernanceService(db, knowledge_base_id).revert_correction(
        correction_id, current_user.id
    )
    if correction is None:
        raise not_found("图谱校正不存在")
    return correction


@router.post(
    "/{knowledge_base_id}/knowledge-graph/corrections/reconcile",
)
def reconcile_graph_corrections(
    knowledge_base_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_edit(db, knowledge_base_id, current_user)
    from app.repositories.knowledge_graph_repository import KnowledgeGraphRepository

    return KnowledgeGraphRepository(
        db,
        knowledge_base_id,
    ).reconcile_corrections()


@router.get(
    "/{knowledge_base_id}/folders",
    response_model=list[FolderOut],
)
def list_folders(
    knowledge_base_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_read(db, knowledge_base_id, current_user)
    from app.repositories.document_management_repository import (
        DocumentManagementRepository,
    )

    return DocumentManagementRepository(db, knowledge_base_id).list_folders()


@router.post(
    "/{knowledge_base_id}/folders",
    response_model=FolderOut,
    status_code=201,
)
def create_folder(
    knowledge_base_id: str,
    payload: FolderCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_edit(db, knowledge_base_id, current_user)
    from app.repositories.document_management_repository import (
        DocumentManagementRepository,
    )

    try:
        return DocumentManagementRepository(db, knowledge_base_id).create_folder(
            name=payload.name,
            parent_id=payload.parent_id,
            description=payload.description,
        )
    except ValueError as exc:
        raise bad_request(str(exc)) from exc


@router.get(
    "/{knowledge_base_id}/tags",
    response_model=list[TagOut],
)
def list_tags(
    knowledge_base_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_read(db, knowledge_base_id, current_user)
    from app.repositories.document_management_repository import (
        DocumentManagementRepository,
    )

    return DocumentManagementRepository(db, knowledge_base_id).list_tags()


@router.post(
    "/{knowledge_base_id}/tags",
    response_model=TagOut,
    status_code=201,
)
def create_tag(
    knowledge_base_id: str,
    payload: TagCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_edit(db, knowledge_base_id, current_user)
    from app.repositories.document_management_repository import (
        DocumentManagementRepository,
    )

    try:
        return DocumentManagementRepository(db, knowledge_base_id).create_tag(
            name=payload.name,
            color=payload.color,
        )
    except ValueError as exc:
        raise bad_request(str(exc)) from exc


@router.get(
    "/{knowledge_base_id}/documents/{document_id}/tags",
    response_model=list[str],
)
def get_document_tags(
    knowledge_base_id: str,
    document_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_read(db, knowledge_base_id, current_user)
    service = DocumentIndexService(db, knowledge_base_id)
    if service.get_document(document_id) is None:
        raise not_found("文档不存在")
    return service.management_repo.list_tag_ids_for_document(document_id)


@router.put(
    "/{knowledge_base_id}/documents/{document_id}/tags",
    response_model=list[str],
)
def update_document_tags(
    knowledge_base_id: str,
    document_id: str,
    payload: DocumentTagsUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_edit(db, knowledge_base_id, current_user)
    service = DocumentIndexService(db, knowledge_base_id)
    try:
        service.management_repo.set_document_tags([document_id], payload.tag_ids)
    except ValueError as exc:
        raise bad_request(str(exc)) from exc
    return service.management_repo.list_tag_ids_for_document(document_id)


@router.get(
    "/{knowledge_base_id}/documents/{document_id}/chunks",
    response_model=list[ChunkOut],
)
def list_document_chunks(
    knowledge_base_id: str,
    document_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_read(db, knowledge_base_id, current_user)
    service = DocumentIndexService(db, knowledge_base_id)
    if service.get_document(document_id) is None:
        raise not_found("文档不存在")
    return service.management_repo.list_chunks(
        document_id,
        offset=offset,
        limit=limit,
    )


@router.get(
    "/{knowledge_base_id}/search",
    response_model=list[SearchHitOut],
)
def search_chunks(
    knowledge_base_id: str,
    query: str = Query(min_length=1, max_length=255),
    limit: int = Query(default=20, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """知识库全文搜索：复用 BM25 索引，返回命中片段与摘要。"""
    _require_read(db, knowledge_base_id, current_user)

    from rag.retrievers.bm25_retriever import get_bm25_retriever

    retriever = get_bm25_retriever(knowledge_base_id)
    docs = retriever.get_relevant_documents(query, top_k=limit)
    if not docs:
        return []

    document_names = {
        document.id: document.original_filename
        for document in DocumentIndexService(db, knowledge_base_id).list_documents()
    }
    hits: list[SearchHitOut] = []
    for doc in docs:
        content = doc.page_content
        hits.append(SearchHitOut(
            chunk_id=str(doc.metadata.get("chunk_id", "")),
            document_id=str(doc.metadata.get("document_id", "")),
            document_name=document_names.get(
                str(doc.metadata.get("document_id", "")), "未知文档"
            ),
            chunk_index=int(doc.metadata.get("chunk_index", 0) or 0),
            page=doc.metadata.get("page"),
            section=doc.metadata.get("section"),
            score=float(doc.metadata.get("bm25_score", 0.0) or 0.0),
            snippet=_build_snippet(content, query),
            content=content,
            images=[
                item for item in (doc.metadata.get("images") or [])
                if isinstance(item, dict)
            ],
        ))
    return hits


def _build_snippet(content: str, query: str, window: int = 160) -> str:
    """围绕首个命中词截取摘要片段。"""
    normalized = content.replace("\r\n", "\n")
    compact = normalized.replace("\n", " ")
    anchor = -1
    for term in query.split():
        term = term.strip()
        if term:
            anchor = compact.find(term)
            if anchor >= 0:
                break
    if anchor < 0:
        return compact[:window * 2]
    start = max(0, anchor - window)
    end = min(len(compact), anchor + len(term) + window)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(compact) else ""
    return f"{prefix}{compact[start:end].strip()}{suffix}"


@router.get(
    "/{knowledge_base_id}/chunks/{chunk_id}",
    response_model=ChunkDetailOut,
)
def get_chunk_detail(
    knowledge_base_id: str,
    chunk_id: str,
    context: int = Query(default=1, ge=0, le=5),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """片段详情：全量内容 + 前后相邻片段，用于前端原文定位预览。"""
    _require_read(db, knowledge_base_id, current_user)

    service = DocumentIndexService(db, knowledge_base_id)
    chunk = service.management_repo.get_chunk_by_id(chunk_id)
    if chunk is None or chunk.knowledge_base_id != knowledge_base_id:
        raise not_found("片段不存在")

    document = service.get_document(chunk.document_id)
    document_name = (
        document.original_filename if document is not None else "未知文档"
    )

    neighbors = service.management_repo.list_chunks_around_index(
        chunk.document_id,
        chunk.chunk_index,
        before=context,
        after=context,
    )
    previous: list[ChunkNeighborOut] = []
    next_chunks: list[ChunkNeighborOut] = []
    for item in neighbors:
        if item.id == chunk.id:
            continue
        entry = ChunkNeighborOut(
            chunk_id=item.id,
            chunk_index=item.chunk_index,
            content=item.content,
            page=item.page,
            section=item.section,
        )
        if item.chunk_index < chunk.chunk_index:
            previous.append(entry)
        else:
            next_chunks.append(entry)

    metadata_images = _chunk_images_from_metadata(chunk)
    return ChunkDetailOut(
        chunk_id=chunk.id,
        document_id=chunk.document_id,
        document_name=document_name,
        chunk_index=chunk.chunk_index,
        content=chunk.content,
        page=chunk.page,
        section=chunk.section,
        images=metadata_images,
        previous=previous,
        next=next_chunks,
    )


def _chunk_images_from_metadata(chunk) -> list[dict]:
    """从 chunk 的 metadata_json 中安全提取 images 列表。"""
    import json

    metadata = getattr(chunk, "metadata_json", None)
    if not metadata:
        return []
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            return []
    images = metadata.get("images") if isinstance(metadata, dict) else None
    if not isinstance(images, list):
        return []
    return [item for item in images if isinstance(item, dict)]


@router.get(
    "/{knowledge_base_id}/documents/{document_id}/versions",
    response_model=list[DocumentVersionOut],
)
def list_document_versions(
    knowledge_base_id: str,
    document_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_read(db, knowledge_base_id, current_user)
    service = DocumentIndexService(db, knowledge_base_id)
    if service.get_document(document_id) is None:
        raise not_found("文档不存在")
    return service.management_repo.list_versions(document_id)


@router.post(
    "/{knowledge_base_id}/documents/{document_id}/versions",
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_document_version(
    knowledge_base_id: str,
    document_id: str,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_edit(db, knowledge_base_id, current_user)
    max_bytes = MAX_DOCUMENT_SIZE_MB * 1024 * 1024
    parts: list[bytes] = []
    total = 0
    while chunk := await file.read(_UPLOAD_READ_CHUNK_BYTES):
        total += len(chunk)
        if total > max_bytes:
            raise bad_request(f"File too large: more than {MAX_DOCUMENT_SIZE_MB}MB")
        parts.append(chunk)
    try:
        job, version = DocumentIndexService(
            db,
            knowledge_base_id,
        ).create_document_version(
            document_id,
            file.filename or "upload.txt",
            b"".join(parts),
            current_user.id,
        )
    except Exception as exc:
        from rag.errors import DocumentTooLargeError, DocumentValidationError

        if isinstance(exc, (DocumentValidationError, DocumentTooLargeError)):
            raise bad_request(str(exc)) from exc
        raise
    return {
        "job_id": job.id,
        "version_id": version.id,
        "version_no": version.version_no,
        "status": "accepted",
    }


@router.post(
    "/{knowledge_base_id}/documents/{document_id}/versions/{version_id}/restore",
    status_code=status.HTTP_202_ACCEPTED,
)
def restore_document_version(
    knowledge_base_id: str,
    document_id: str,
    version_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_edit(db, knowledge_base_id, current_user)
    try:
        job, version = DocumentIndexService(
            db,
            knowledge_base_id,
        ).restore_document_version(
            document_id,
            version_id,
            current_user.id,
        )
    except Exception as exc:
        from rag.errors import DocumentValidationError

        if isinstance(exc, DocumentValidationError):
            raise bad_request(str(exc)) from exc
        raise
    return {
        "job_id": job.id,
        "version_id": version.id,
        "version_no": version.version_no,
        "restored_from_version_id": version_id,
        "status": "accepted",
    }


@router.post(
    "/{knowledge_base_id}/documents/batch",
    status_code=status.HTTP_202_ACCEPTED,
)
def batch_documents(
    knowledge_base_id: str,
    payload: BatchDocumentRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_edit(db, knowledge_base_id, current_user)
    try:
        result = DocumentIndexService(db, knowledge_base_id).request_batch(
            payload.document_ids,
            payload.action,
            requested_by=current_user.id,
            tag_ids=payload.tag_ids,
            folder_id=payload.folder_id,
        )
    except Exception as exc:
        from rag.errors import DocumentValidationError

        if isinstance(exc, (ValueError, DocumentValidationError)):
            raise bad_request(str(exc)) from exc
        raise
    if isinstance(result, dict):
        return {**result, "status": "completed"}
    return {"job_id": result.id, "status": "accepted"}
