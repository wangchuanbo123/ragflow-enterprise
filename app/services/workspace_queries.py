"""Read models for paged document/task screens and recorded stage progress."""
from collections import defaultdict

from sqlalchemy import select, func, or_
from app.models.knowledge_document import KnowledgeDocument
from app.models.document_management import DocumentTagLink
from app.models.index_job import IndexJob, IndexJobItem, IndexJobEvent
from app.schemas.document import DocumentOut, IndexJobOut, DocumentProcessing, ProcessingStage

STAGES = ("parse", "chunk", "vector", "schema", "graph")


def _stage_rows(db, kb, job_ids):
    if not job_ids:
        return []
    # A completed document can have thousands of phase events over time.
    # Select only the latest event for each document/stage/attempt in SQL.
    payload = IndexJobEvent.payload_json
    latest = select(func.max(IndexJobEvent.id)).where(
        IndexJobEvent.knowledge_base_id == kb,
        IndexJobEvent.job_id.in_(job_ids), IndexJobEvent.event_type == "stage",
    ).group_by(IndexJobEvent.job_id, payload["stage"].as_string(),
               payload["document_id"].as_string()).scalar_subquery()
    return list(db.scalars(select(IndexJobEvent).where(IndexJobEvent.id.in_(latest)).order_by(IndexJobEvent.id)))


def _progress(job, events, document_id=None):
    stages, last = {}, None
    for event in events:
        if event.job_id != job.id:
            continue
        payload = event.payload_json or {}
        if payload.get("document_id") not in {None, document_id} and document_id is not None:
            continue
        if job.started_at and event.created_at and event.created_at < job.started_at:
            continue  # Do not show success from a previous execution attempt.
        name = payload.get("stage")
        state = payload.get("status", event.status)
        if name and state in {"pending", "running", "completed", "failed", "cancelled", "unknown"}:
            stages[name] = state
            last = event
    reason = None
    if job.status == "pending":
        reason = "任务已排队，等待知识库写入或处理额度"
    elif job.cancel_requested and job.status == "running":
        reason = "正在等待当前步骤安全停止"
    return {
        "job_id": job.id,
        "stage": (last.payload_json or {}).get("stage") if last else None,
        "status": job.status,
        "message": last.message if last else job.error_message,
        "updated_at": last.created_at if last else job.finished_at or job.started_at or job.created_at,
        "stages": [{"name": name, "status": stages.get(name, "unknown")} for name in (*STAGES, *sorted(set(stages) - set(STAGES)))],
        "waiting_reason": reason,
    }


def serialize_documents(db, kb, documents):
    identifiers = [document.id for document in documents]
    if not identifiers:
        return []
    tags = defaultdict(list)
    for link in db.scalars(select(DocumentTagLink).where(
            DocumentTagLink.knowledge_base_id == kb, DocumentTagLink.document_id.in_(identifiers))):
        tags[link.document_id].append(link.tag_id)
    ranked = select(IndexJob.id.label("job_id"), IndexJobItem.document_id.label("document_id"),
        func.row_number().over(partition_by=IndexJobItem.document_id,
            order_by=(IndexJob.created_at.desc(), IndexJob.id.desc())).label("position")
    ).join(IndexJobItem, IndexJobItem.job_id == IndexJob.id).where(
        IndexJob.knowledge_base_id == kb, IndexJobItem.document_id.in_(identifiers)).subquery()
    associations = db.execute(select(ranked.c.job_id, ranked.c.document_id).where(ranked.c.position == 1)).all()
    jobs = {job.id: job for job in db.scalars(select(IndexJob).where(IndexJob.id.in_({row.job_id for row in associations})))}
    by_document = {row.document_id: jobs[row.job_id] for row in associations}
    events = _stage_rows(db, kb, list(jobs))
    result = []
    for document in documents:
        out = DocumentOut.model_validate(document)
        out.tag_ids = tags[document.id]
        if document.id in by_document:
            out.processing = DocumentProcessing.model_validate(_progress(by_document[document.id], events, document.id))
        result.append(out)
    return result


def documents_page(db, kb, *, offset=0, limit=30, query=None, status=None, folder_id=None, tag_id=None):
    conditions = [KnowledgeDocument.knowledge_base_id == kb]
    if query:
        conditions.append(KnowledgeDocument.original_filename.contains(query, autoescape=True))
    if status:
        conditions.append(KnowledgeDocument.status == status)
    if folder_id:
        conditions.append(KnowledgeDocument.folder_id == folder_id)
    if tag_id:
        conditions.append(select(DocumentTagLink.document_id).where(
            DocumentTagLink.knowledge_base_id == kb, DocumentTagLink.document_id == KnowledgeDocument.id,
            DocumentTagLink.tag_id == tag_id).exists())
    total = db.scalar(select(func.count()).select_from(KnowledgeDocument).where(*conditions)) or 0
    rows = list(db.scalars(select(KnowledgeDocument).where(*conditions)
        .order_by(KnowledgeDocument.created_at.desc(), KnowledgeDocument.id).offset(offset).limit(limit)))
    return {"items": serialize_documents(db, kb, rows), "total": total, "offset": offset, "limit": limit}


def serialize_jobs(db, kb, jobs):
    identifiers = [job.id for job in jobs]
    if not identifiers:
        return []
    items = defaultdict(dict)
    rows = db.execute(select(IndexJobItem.job_id, KnowledgeDocument.id, KnowledgeDocument.original_filename)
        .join(KnowledgeDocument, KnowledgeDocument.id == IndexJobItem.document_id).where(
            IndexJobItem.job_id.in_(identifiers), KnowledgeDocument.knowledge_base_id == kb))
    for job_id, document_id, name in rows:
        items[job_id][document_id] = name
    events = _stage_rows(db, kb, identifiers)
    result = []
    for job in jobs:
        out = IndexJobOut.model_validate(job)
        out.document_ids = list(items[job.id])
        out.document_names = list(items[job.id].values())
        progress = _progress(job, events)
        out.current_stage, out.waiting_reason = progress["stage"], progress["waiting_reason"]
        out.stages = [ProcessingStage.model_validate(row) for row in progress["stages"]]
        result.append(out)
    return result


def jobs_page(db, kb, *, offset=0, limit=30, status=None, query=None):
    conditions = [IndexJob.knowledge_base_id == kb]
    if status:
        conditions.append(IndexJob.status == status)
    if query:
        documents = select(IndexJobItem.job_id).join(KnowledgeDocument,
            KnowledgeDocument.id == IndexJobItem.document_id).where(
            KnowledgeDocument.knowledge_base_id == kb,
            KnowledgeDocument.original_filename.contains(query, autoescape=True))
        conditions.append(or_(IndexJob.id == query, IndexJob.job_type.contains(query, autoescape=True), IndexJob.id.in_(documents)))
    total = db.scalar(select(func.count()).select_from(IndexJob).where(*conditions)) or 0
    jobs = list(db.scalars(select(IndexJob).where(*conditions)
        .order_by(IndexJob.created_at.desc(), IndexJob.id).offset(offset).limit(limit)))
    return {"items": serialize_jobs(db, kb, jobs), "total": total, "offset": offset, "limit": limit}
