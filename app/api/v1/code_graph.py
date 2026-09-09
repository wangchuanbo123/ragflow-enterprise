"""Code symbol, call, and dependency graph APIs."""

import uuid
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Query, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.api.errors import bad_request, forbidden, not_found
from app.core.database import get_db
from app.core.config import DOC_DIR, INDEX_JOB_MAX_ATTEMPTS, MAX_BATCH_UPLOAD_TOTAL_MB
from app.models.code_graph import CodeFile
from app.models.index_job import IndexJob
from app.models.user import User
from app.repositories.code_graph_repository import CodeGraphRepository
from app.repositories.index_job_repository import IndexJobRepository
from app.repositories.knowledge_base_repository import KnowledgeBaseRepository
from app.schemas.code_graph import (
    CodeGraphStatsOut,
    CodeFileIssueOut,
    CodeIndexRequest,
    CodeRelationOut,
    CodeSnapshotOut,
    CodeSnapshotDiffOut,
    CodeSubgraphOut,
    CodeSymbolOut,
    CodeVectorStateOut,
)
from app.schemas.document import IndexJobOut
from app.schemas.knowledge_base import DataSourceOut
from app.services.code_index_service import CodeIndexService
from app.services.code_vector_service import CodeVectorService

router = APIRouter(prefix="/knowledge-bases", tags=["code-graph"])


def _require(db: Session, knowledge_base_id: str, user: User, *, edit: bool = False) -> None:
    repository = KnowledgeBaseRepository(db)
    allowed = (
        repository.can_edit(knowledge_base_id, user.id, is_admin=user.role == "admin")
        if edit else
        repository.can_read(knowledge_base_id, user.id, is_admin=user.role == "admin")
    )
    if not allowed:
        if repository.get(knowledge_base_id) is None:
            raise not_found("知识库不存在")
        raise forbidden("没有该知识库的访问权限")


@router.get("/{knowledge_base_id}/code-graph/stats", response_model=CodeGraphStatsOut)
def code_graph_stats(
    knowledge_base_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require(db, knowledge_base_id, current_user)
    return CodeGraphRepository(db, knowledge_base_id).stats()


@router.get("/{knowledge_base_id}/code-graph/snapshots", response_model=list[CodeSnapshotOut])
def list_code_snapshots(
    knowledge_base_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require(db, knowledge_base_id, current_user)
    return CodeGraphRepository(db, knowledge_base_id).list_snapshots(limit)


@router.post(
    "/{knowledge_base_id}/code-graph/upload",
    response_model=DataSourceOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_code_archive(
    knowledge_base_id: str,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Safely unpack a ZIP into the persistent allowed source root."""
    _require(db, knowledge_base_id, current_user, edit=True)
    filename = Path(file.filename or "code.zip").name
    if Path(filename).suffix.lower() != ".zip":
        raise bad_request("代码压缩包必须是 ZIP 格式")
    max_bytes = MAX_BATCH_UPLOAD_TOTAL_MB * 1024 * 1024
    body = await file.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise bad_request(f"代码压缩包不能超过 {MAX_BATCH_UPLOAD_TOTAL_MB} MB")
    archive_id = str(uuid.uuid4())
    archive_root = (DOC_DIR / "code_uploads" / knowledge_base_id / archive_id).resolve()
    archive_root.mkdir(parents=True, exist_ok=False)
    archive_path = archive_root / "source.zip"
    archive_path.write_bytes(body)
    try:
        with zipfile.ZipFile(archive_path) as package:
            members = [item for item in package.infolist() if not item.is_dir()]
            if not members:
                raise ValueError("代码压缩包中没有文件")
            if len(members) > 10000:
                raise ValueError("压缩包文件数量超过 10000 个")
            expanded = sum(item.file_size for item in members)
            if expanded > max_bytes * 4:
                raise ValueError("压缩后的代码体积异常，已停止解压")
            for item in members:
                unix_mode = (item.external_attr >> 16) & 0o170000
                if unix_mode == 0o120000:
                    raise ValueError("代码压缩包不能包含符号链接")
                target = (archive_root / item.filename).resolve()
                if archive_root not in target.parents:
                    raise ValueError("压缩包包含不安全的文件路径")
            package.extractall(archive_root)
    except (zipfile.BadZipFile, ValueError) as exc:
        import shutil
        shutil.rmtree(archive_root, ignore_errors=True)
        raise bad_request(str(exc) or "ZIP 文件损坏") from exc
    archive_path.unlink(missing_ok=True)
    children = [item for item in archive_root.iterdir()]
    code_root = children[0] if len(children) == 1 and children[0].is_dir() else archive_root
    repository = KnowledgeBaseRepository(db)
    display_name = Path(filename).stem[:100] or "上传代码"
    display_name = f"{display_name}-{archive_id[:6]}"
    try:
        return repository.create_data_source(
            knowledge_base_id,
            source_type="local_directory",
            name=display_name,
            connection_ref=None,
            config={"location": str(code_root), "index_code": True, "uploaded_archive": True},
        )
    except ValueError as exc:
        raise bad_request(str(exc)) from exc


@router.get(
    "/{knowledge_base_id}/code-graph/snapshots/{snapshot_id}/diff",
    response_model=CodeSnapshotDiffOut,
)
def code_snapshot_diff(
    knowledge_base_id: str,
    snapshot_id: str,
    base_snapshot_id: str = Query(min_length=1, max_length=36),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require(db, knowledge_base_id, current_user)
    repository = CodeGraphRepository(db, knowledge_base_id)
    if repository.get_snapshot(snapshot_id) is None or repository.get_snapshot(base_snapshot_id) is None:
        raise not_found("代码快照不存在")
    def manifest(target_id: str) -> dict[str, str]:
        return {
            path: digest for path, digest in db.execute(
                select(CodeFile.path, CodeFile.content_hash).where(CodeFile.snapshot_id == target_id)
            ).all()
        }
    base = manifest(base_snapshot_id)
    target = manifest(snapshot_id)
    added = sorted(set(target) - set(base))
    deleted = sorted(set(base) - set(target))
    modified = sorted(path for path in set(base) & set(target) if base[path] != target[path])
    unchanged = len(set(base) & set(target)) - len(modified)
    return CodeSnapshotDiffOut(
        base_snapshot_id=base_snapshot_id,
        target_snapshot_id=snapshot_id,
        added_files=added,
        modified_files=modified,
        deleted_files=deleted,
        unchanged_files=unchanged,
    )


@router.get(
    "/{knowledge_base_id}/code-graph/snapshots/{snapshot_id}/issues",
    response_model=list[CodeFileIssueOut],
)
def code_snapshot_issues(
    knowledge_base_id: str,
    snapshot_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require(db, knowledge_base_id, current_user)
    if CodeGraphRepository(db, knowledge_base_id).get_snapshot(snapshot_id) is None:
        raise not_found("代码快照不存在")
    rows = db.scalars(
        select(CodeFile).where(CodeFile.snapshot_id == snapshot_id, CodeFile.parse_status != "completed")
        .order_by(CodeFile.path).limit(500)
    )
    return [
        CodeFileIssueOut(path=item.path, language=item.language, status=item.parse_status, errors=list(item.parse_errors_json or []))
        for item in rows
    ]


@router.post(
    "/{knowledge_base_id}/code-graph/index",
    response_model=IndexJobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def request_code_index(
    knowledge_base_id: str,
    payload: CodeIndexRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require(db, knowledge_base_id, current_user, edit=True)
    try:
        return CodeIndexService(db, knowledge_base_id).request_index(
            payload.data_source_id,
            current_user.id,
        )
    except ValueError as exc:
        raise bad_request(str(exc)) from exc


@router.post(
    "/{knowledge_base_id}/code-graph/index-documents",
    response_model=IndexJobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def request_document_code_index(
    knowledge_base_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """从已入库文档的代码块构建代码图谱（无需 Git/目录数据源）。"""
    _require(db, knowledge_base_id, current_user, edit=True)
    from app.core.config import DOCUMENT_CODE_GRAPH_ENABLED

    if not DOCUMENT_CODE_GRAPH_ENABLED:
        raise bad_request("文档代码图谱未启用（DOCUMENT_CODE_GRAPH_ENABLED=false）")
    service = CodeIndexService(db, knowledge_base_id)
    existing = IndexJobRepository(db, knowledge_base_id).find_open(
        "code_index", option_key="data_source_id", option_value="documents"
    )
    if existing is not None:
        return existing
    job = IndexJobRepository(db, knowledge_base_id).create(IndexJob(
        job_type="code_index",
        status="pending",
        requested_by=current_user.id,
        total_items=1,
        max_attempts=INDEX_JOB_MAX_ATTEMPTS,
        options_json={"data_source_id": "documents"},
    ))
    from app.bootstrap.container import get_provider_container

    get_provider_container().task_queue.enqueue(job.id)
    return job


@router.get("/{knowledge_base_id}/code-graph/symbols", response_model=list[CodeSymbolOut])
def search_code_symbols(
    knowledge_base_id: str,
    query: str = Query(min_length=1, max_length=255),
    limit: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require(db, knowledge_base_id, current_user)
    try:
        return CodeVectorService(db, knowledge_base_id).hybrid_search(query, limit)
    except Exception:
        return CodeGraphRepository(db, knowledge_base_id).search_symbols(query, limit)


@router.get("/{knowledge_base_id}/code-graph/vector", response_model=CodeVectorStateOut)
def code_vector_state(
    knowledge_base_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require(db, knowledge_base_id, current_user)
    state = CodeVectorService(db, knowledge_base_id).state()
    return state or CodeVectorStateOut()


@router.post(
    "/{knowledge_base_id}/code-graph/vector/rebuild",
    response_model=IndexJobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def rebuild_code_vectors(
    knowledge_base_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require(db, knowledge_base_id, current_user, edit=True)
    return CodeVectorService(db, knowledge_base_id).request_rebuild(current_user.id)


@router.get("/{knowledge_base_id}/code-graph/subgraph", response_model=CodeSubgraphOut)
def get_code_subgraph(
    knowledge_base_id: str,
    symbol_ids: list[str] = Query(min_length=1, max_length=20),
    limit: int = Query(default=100, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require(db, knowledge_base_id, current_user)
    repository = CodeGraphRepository(db, knowledge_base_id)
    nodes = [item for symbol_id in symbol_ids if (item := repository.get_symbol(symbol_id))]
    edges = repository.symbol_relations(symbol_ids, limit)
    related_ids = {
        value
        for edge in edges
        for value in (edge["source"], edge["target"])
        if value and value not in symbol_ids
    }
    nodes.extend(
        item for symbol_id in sorted(related_ids)
        if (item := repository.get_symbol(symbol_id))
    )
    return CodeSubgraphOut(nodes=nodes, edges=edges)
