"""Cohesive task handlers; ownership, leases and lifecycle remain in index_worker."""
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from app.core.config import DOC_DIR
from app.repositories.index_job_repository import JobLeaseLostError

logger = logging.getLogger(__name__)


@dataclass
class JobContext:
    db: object
    job: object
    job_repo: object
    mgr: object
    container: object
    job_id: str
    heartbeat: object
    agent_context: object
    agent_document_ids: object
    agent_cancelled: object
    agent_event_callback: object
    link_followup_jobs: object


@dataclass
class HandlerResult:
    deferred_completion: dict | None = None
    attached_result: object = None
    attached_error_code: str | None = None
    stop: bool = False


def handle_agent_workflow(ctx):
    db, job, job_repo, mgr, container = ctx.db, ctx.job, ctx.job_repo, ctx.mgr, ctx.container
    job_id, heartbeat = ctx.job_id, ctx.heartbeat
    agent_context, agent_document_ids = ctx.agent_context, ctx.agent_document_ids
    agent_cancelled, agent_event_callback = ctx.agent_cancelled, ctx.agent_event_callback
    _link_followup_jobs = ctx.link_followup_jobs
    deferred_completion = attached_result = attached_error_code = None
    from app.services.agent_workflow_service import AgentWorkflowService

    result = AgentWorkflowService(db, job.knowledge_base_id).execute_for_job(job.id)
    status = result.get("status")
    if status not in {"completed", "failed", "cancelled"}:
        raise ValueError("Agent workflow returned an invalid terminal status")
    job_repo.update(
        job, status=status, total_items=1, processed_items=1,
        succeeded_items=int(status == "completed"), failed_items=int(status == "failed"),
        error_message=result.get("error") or result.get("error_type"),
    )
    return HandlerResult(deferred_completion, attached_result, attached_error_code)


def handle_sync(ctx):
    db, job, job_repo, mgr, container = ctx.db, ctx.job, ctx.job_repo, ctx.mgr, ctx.container
    job_id, heartbeat = ctx.job_id, ctx.heartbeat
    agent_context, agent_document_ids = ctx.agent_context, ctx.agent_document_ids
    agent_cancelled, agent_event_callback = ctx.agent_cancelled, ctx.agent_event_callback
    _link_followup_jobs = ctx.link_followup_jobs
    deferred_completion = attached_result = attached_error_code = None
    result = mgr.sync(DOC_DIR, dry_run=False)
    status = result.get("status", "completed")
    deferred_completion = dict(
        status=status,
        total_items=result.get("indexed", 0) + result.get("deleted", 0),
        processed_items=result.get("indexed", 0) + result.get("deleted", 0),
        succeeded_items=result.get("indexed", 0),
        failed_items=result.get("failed", 0),
        error_message=(
            "; ".join(result.get("errors", []))[:500]
            if result.get("errors")
            else None
        ),
    )
    return HandlerResult(deferred_completion, attached_result, attached_error_code)


def handle_rebuild(ctx):
    db, job, job_repo, mgr, container = ctx.db, ctx.job, ctx.job_repo, ctx.mgr, ctx.container
    job_id, heartbeat = ctx.job_id, ctx.heartbeat
    agent_context, agent_document_ids = ctx.agent_context, ctx.agent_document_ids
    agent_cancelled, agent_event_callback = ctx.agent_cancelled, ctx.agent_event_callback
    _link_followup_jobs = ctx.link_followup_jobs
    deferred_completion = attached_result = attached_error_code = None
    from app.services.index_rebuild_service import rebuild_registered_originals
    import uuid

    result = rebuild_registered_originals(
        mgr, container.object_store, job_id=f"{job_id[:8]}_{uuid.uuid4().hex[:8]}", cancelled=agent_cancelled,
    )
    status = result.get("status", "completed")
    deferred_completion = dict(
        status=status,
        total_items=result.get("documents_indexed", 0),
        processed_items=result.get("documents_indexed", 0),
        succeeded_items=result.get("documents_indexed", 0),
        failed_items=result.get("documents_failed", 0),
        error_message=(
            "; ".join(result.get("errors", []))[:500]
            if result.get("errors")
            else None
        ),
    )
    return HandlerResult(deferred_completion, attached_result, attached_error_code)


def handle_file(ctx):
    db, job, job_repo, mgr, container = ctx.db, ctx.job, ctx.job_repo, ctx.mgr, ctx.container
    job_id, heartbeat = ctx.job_id, ctx.heartbeat
    agent_context, agent_document_ids = ctx.agent_context, ctx.agent_document_ids
    agent_cancelled, agent_event_callback = ctx.agent_cancelled, ctx.agent_event_callback
    _link_followup_jobs = ctx.link_followup_jobs
    deferred_completion = attached_result = attached_error_code = None
    all_items = job_repo.list_items(job_id)
    items = [item for item in all_items if item.status != "completed"]
    succeeded = sum(
        1 for item in all_items if item.status == "completed"
    )
    failed = 0
    for position, item in enumerate(items, start=1):
        if heartbeat.lease_lost:
            raise JobLeaseLostError("Job lease was lost during processing")
        if job_repo.is_cancel_requested(job_id):
            job_repo.update(
                job,
                status="cancelled",
                processed_items=succeeded + failed,
                succeeded_items=succeeded,
                failed_items=failed,
            )
            return HandlerResult(stop=True)

        item.started_at = datetime.now(timezone.utc)
        item.status = "running"
        db.commit()
        try:
            state = mgr.repo.get_index_state()
            active_coll = state.active_collection_name
            if active_coll is None:
                active_coll = mgr.ensure_active_collection(job_id[:8])

            if item.action == "delete":
                # Older disk-ingested documents used the mutable
                # canonical object as version 1. Freeze that
                # version before removing the canonical file.
                from pathlib import PurePosixPath
                from sqlalchemy import select
                from app.models.document_management import DocumentVersion

                legacy_versions = list(db.scalars(select(DocumentVersion).where(
                    DocumentVersion.knowledge_base_id == job.knowledge_base_id,
                    DocumentVersion.document_id == item.document_id,
                    DocumentVersion.object_key == item.source_path,
                )))
                if legacy_versions and container.object_store.exists(item.source_path):
                    with container.object_store.materialize(item.source_path) as path:
                        content = path.read_bytes()
                    canonical = PurePosixPath(item.source_path)
                    for version in legacy_versions:
                        key = str(canonical.parent / ".versions" / str(version.version_no) / canonical.name)
                        reference = container.object_store.put_bytes(key, content)
                        version.object_key = reference.key
                    db.commit()
                mgr.delete_document(item.source_path, active_coll)
                container.object_store.delete(item.source_path)
            else:
                with container.object_store.materialize(
                    item.source_path
                ) as file_path:
                    from rag.utils.file_hash import file_hash as calc_hash

                    file_digest = calc_hash(file_path)
                    mgr.index_document(
                        item.source_path,
                        file_path,
                        file_digest,
                        active_coll,
                        document_id=item.document_id,
                    )

            item.status = "completed"
            item.finished_at = datetime.now(timezone.utc)
            succeeded += 1
        except Exception as exc:
            item.status = "failed"
            item.error_message = str(exc)[:500]
            item.finished_at = datetime.now(timezone.utc)
            failed += 1
        db.commit()
        job_repo.update(
            job,
            processed_items=succeeded + failed,
            succeeded_items=succeeded,
            failed_items=failed,
        )

    deferred_completion = dict(
        status="completed" if failed == 0 else "partial",
        total_items=len(all_items),
        processed_items=succeeded + failed,
        succeeded_items=succeeded,
        failed_items=failed,
    )
    return HandlerResult(deferred_completion, attached_result, attached_error_code)


def handle_connector_sync(ctx):
    db, job, job_repo, mgr, container = ctx.db, ctx.job, ctx.job_repo, ctx.mgr, ctx.container
    job_id, heartbeat = ctx.job_id, ctx.heartbeat
    agent_context, agent_document_ids = ctx.agent_context, ctx.agent_document_ids
    agent_cancelled, agent_event_callback = ctx.agent_cancelled, ctx.agent_event_callback
    _link_followup_jobs = ctx.link_followup_jobs
    deferred_completion = attached_result = attached_error_code = None
    from app.services.data_source_service import DataSourceService

    source_id = str((job.options_json or {}).get("data_source_id", ""))
    result = DataSourceService(
        db, job.knowledge_base_id, task_queue=container.task_queue
    ).execute_sync(source_id, job.requested_by)
    child_id = result.get("child_job_id")
    if child_id:
        _link_followup_jobs(db, job, [job_repo.get(child_id)])
    failed = 1 if result.get("status") == "failed" else len(result.get("rejected", []))
    job_repo.update(
        job,
        status="failed" if result.get("status") == "failed" else ("partial" if failed else "completed"),
        total_items=1,
        processed_items=1,
        succeeded_items=0 if result.get("status") == "failed" else 1,
        failed_items=failed,
        error_message=result.get("detail"),
    )
    return HandlerResult(deferred_completion, attached_result, attached_error_code)


def handle_code_index(ctx):
    try:
        return _handle_code_index(ctx)
    except Exception as exc:
        from app.repositories.index_job_repository import JobLeaseLostError
        from app.agents.contracts import AgentCancelledError
        if isinstance(exc, JobLeaseLostError):
            raise
        if (ctx.job.options_json or {}).get("prepare_archive"):
            from app.repositories.knowledge_base_repository import KnowledgeBaseRepository
            ctx.db.rollback()
            source = KnowledgeBaseRepository(ctx.db).get_data_source(
                ctx.job.knowledge_base_id, (ctx.job.options_json or {}).get("data_source_id"),
            )
            if source is not None:
                source.last_sync_status = "cancelled" if isinstance(exc, AgentCancelledError) else "failed"
                source.last_sync_detail = "代码准备或索引未完成，请查看任务并重试"
                ctx.db.commit()
        raise


def _handle_code_index(ctx):
    db, job, job_repo, mgr, container = ctx.db, ctx.job, ctx.job_repo, ctx.mgr, ctx.container
    job_id, heartbeat = ctx.job_id, ctx.heartbeat
    agent_context, agent_document_ids = ctx.agent_context, ctx.agent_document_ids
    agent_cancelled, agent_event_callback = ctx.agent_cancelled, ctx.agent_event_callback
    _link_followup_jobs = ctx.link_followup_jobs
    deferred_completion = attached_result = attached_error_code = None
    import tempfile
    from pathlib import Path

    from app.services.code_index_service import CodeIndexService
    from app.services.data_source_service import DataSourceService
    from app.repositories.knowledge_base_repository import KnowledgeBaseRepository

    source_id = str((job.options_json or {}).get("data_source_id", ""))
    service = CodeIndexService(db, job.knowledge_base_id)
    if not source_id or source_id == "documents":
        # 文档内代码图谱：直接从已入库 chunk 提取代码块。
        result = service.index_document_code()
    else:
        source = KnowledgeBaseRepository(db).get_data_source(job.knowledge_base_id, source_id)
        if source is None:
            raise ValueError("数据源不存在")
        if (job.options_json or {}).get("prepare_archive"):
            from app.services.code_archive_service import CodeArchiveService
            CodeArchiveService(db, job.knowledge_base_id, container.task_queue).prepare(source, cancelled=agent_cancelled)
        if source.source_type == "local_directory":
            root = DataSourceService(db, job.knowledge_base_id).resolve_local_source(source)
            result = service.index_directory(
                root,
                data_source_id=source.id,
                merge_document_code=True,
            )
        elif source.source_type == "git":
            with tempfile.TemporaryDirectory(prefix="rag-code-git-") as temp_dir:
                root, revision = DataSourceService(db, job.knowledge_base_id)._checkout_git(source, Path(temp_dir))
                result = service.index_directory(
                    root,
                    data_source_id=source.id,
                    revision=revision,
                    merge_document_code=True,
                )
        else:
            raise ValueError("代码索引仅支持本地目录和 Git 数据源")
    from app.core.config import CODE_VECTOR_ENABLED
    if CODE_VECTOR_ENABLED:
        from app.services.code_vector_service import CodeVectorService

        result["code_vectors"] = CodeVectorService(
            db, job.knowledge_base_id, container=container, agent_context=agent_context,
            cancelled=agent_cancelled, on_event=agent_event_callback,
        ).rebuild()
    attached_result = result
    summary_failures = int(result.get("code_vectors", {}).get("summary_agent_failures", 0))
    if summary_failures:
        attached_error_code = "CODE_SUMMARY_AGENT_DEGRADED"
    if source_id and source_id != "documents" and (job.options_json or {}).get("prepare_archive"):
        from datetime import datetime, timezone
        source.last_sync_status = "partial" if summary_failures else "completed"
        source.last_sync_detail = "代码索引已完成"
        source.last_synced_at = datetime.now(timezone.utc).replace(tzinfo=None)
    job_repo.update(
        job, status="partial" if summary_failures else "completed", total_items=1, processed_items=1,
        succeeded_items=int(not summary_failures), failed_items=int(bool(summary_failures)),
        error_message="代码索引可用；Agent功能摘要失败，已用确定性描述" if summary_failures else None,
    )
    return HandlerResult(deferred_completion, attached_result, attached_error_code)


def handle_entity_vector_rebuild(ctx):
    db, job, job_repo, mgr, container = ctx.db, ctx.job, ctx.job_repo, ctx.mgr, ctx.container
    job_id, heartbeat = ctx.job_id, ctx.heartbeat
    agent_context, agent_document_ids = ctx.agent_context, ctx.agent_document_ids
    agent_cancelled, agent_event_callback = ctx.agent_cancelled, ctx.agent_event_callback
    _link_followup_jobs = ctx.link_followup_jobs
    deferred_completion = attached_result = attached_error_code = None
    from app.services.entity_vector_service import EntityVectorService

    result = EntityVectorService(
        db, job.knowledge_base_id, container=container
    ).rebuild()
    job_repo.update(
        job, status="completed", total_items=1, processed_items=1,
        succeeded_items=1, failed_items=0,
    )
    return HandlerResult(deferred_completion, attached_result, attached_error_code)


def handle_relation_vector_rebuild(ctx):
    db, job, job_repo, mgr, container = ctx.db, ctx.job, ctx.job_repo, ctx.mgr, ctx.container
    job_id, heartbeat = ctx.job_id, ctx.heartbeat
    agent_context, agent_document_ids = ctx.agent_context, ctx.agent_document_ids
    agent_cancelled, agent_event_callback = ctx.agent_cancelled, ctx.agent_event_callback
    _link_followup_jobs = ctx.link_followup_jobs
    deferred_completion = attached_result = attached_error_code = None
    from app.services.relation_vector_service import RelationVectorService

    result = RelationVectorService(
        db, job.knowledge_base_id, container=container
    ).rebuild()
    job_repo.update(
        job, status="completed", total_items=1, processed_items=1,
        succeeded_items=1, failed_items=0,
    )
    return HandlerResult(deferred_completion, attached_result, attached_error_code)


def handle_code_vector_rebuild(ctx):
    db, job, job_repo, mgr, container = ctx.db, ctx.job, ctx.job_repo, ctx.mgr, ctx.container
    job_id, heartbeat = ctx.job_id, ctx.heartbeat
    agent_context, agent_document_ids = ctx.agent_context, ctx.agent_document_ids
    agent_cancelled, agent_event_callback = ctx.agent_cancelled, ctx.agent_event_callback
    _link_followup_jobs = ctx.link_followup_jobs
    deferred_completion = attached_result = attached_error_code = None
    from app.services.code_vector_service import CodeVectorService

    result = CodeVectorService(
        db, job.knowledge_base_id, container=container, agent_context=agent_context,
        cancelled=agent_cancelled, on_event=agent_event_callback,
    ).rebuild()
    attached_result = result
    summary_failures = int(result.get("summary_agent_failures", 0))
    if summary_failures:
        attached_error_code = "CODE_SUMMARY_AGENT_DEGRADED"
    job_repo.update(
        job, status="partial" if summary_failures else "completed", total_items=1, processed_items=1,
        succeeded_items=int(not summary_failures), failed_items=int(bool(summary_failures)),
        error_message="代码检索可用；Agent功能摘要失败，已用确定性描述" if summary_failures else None,
    )
    return HandlerResult(deferred_completion, attached_result, attached_error_code)


def handle_graph_rebuild(ctx):
    db, job, job_repo, mgr, container = ctx.db, ctx.job, ctx.job_repo, ctx.mgr, ctx.container
    job_id, heartbeat = ctx.job_id, ctx.heartbeat
    agent_context, agent_document_ids = ctx.agent_context, ctx.agent_document_ids
    agent_cancelled, agent_event_callback = ctx.agent_cancelled, ctx.agent_event_callback
    _link_followup_jobs = ctx.link_followup_jobs
    deferred_completion = attached_result = attached_error_code = None
    from app.services.entity_vector_service import EntityVectorService
    from app.services.graph_governance_service import GraphGovernanceService
    from app.services.knowledge_graph_service import GraphBuildService
    from app.services.relation_vector_service import RelationVectorService

    options = job.options_json or {}
    mode = options.get("mode", "pending")
    vector_update = options.get("vector_update", "full")
    community_update = options.get("community_update", "full")
    result = GraphBuildService(
        db, knowledge_base_id=job.knowledge_base_id
    ).build_graph(
        only_pending=mode != "full",
        retry_failed=mode == "retry_failed",
        document_ids=agent_document_ids,
        cancelled=agent_cancelled,
        agent_context=agent_context,
        on_event=agent_event_callback,
    )
    attached_result = result
    changes = result.get("changes", {})
    governance = GraphGovernanceService(
        db, job.knowledge_base_id, task_queue=container.task_queue
    )
    report = governance.assess_quality(job.requested_by)
    if community_update == "defer":
        # 图谱立即可用，社区延迟到维护任务重建。
        if int(result.get("succeeded", 0)) or changes:
            governance.mark_communities_stale()
    elif community_update == "affected":
        result["communities"] = governance.rebuild_communities(
            changed_entity_ids=changes.get("community_entity_ids")
        )
    else:
        result["communities"] = governance.rebuild_communities()
    if vector_update == "incremental":
        result["entity_vectors"] = EntityVectorService(
            db, job.knowledge_base_id, container=container
        ).apply_changes(
            changes.get("changed_entity_ids"),
            changes.get("deleted_entity_ids"),
        )
        result["relation_vectors"] = RelationVectorService(
            db, job.knowledge_base_id, container=container
        ).apply_changes(
            changes.get("changed_relation_ids"),
            changes.get("deleted_relation_ids"),
        )
    elif vector_update == "full":
        result["entity_vectors"] = EntityVectorService(
            db, job.knowledge_base_id, container=container
        ).rebuild()
        result["relation_vectors"] = RelationVectorService(
            db, job.knowledge_base_id, container=container
        ).rebuild()
    try:
        result["entity_disambiguation"] = (
            governance.scan_semantic_merge_candidates()
        )
        result["relation_disambiguation"] = (
            governance.scan_relation_merge_candidates()
        )
    except Exception as exc:
        logger.warning(
            "Graph disambiguation candidate scan failed for %s: %s",
            job.knowledge_base_id,
            exc,
        )
    failed = int(result.get("failed", 0))
    skipped = int(result.get("skipped", 0))
    deferred_completion = dict(
        status="partial" if failed or report.status != "ready" else "completed",
        total_items=int(result.get("total_chunks", 0)),
        processed_items=int(result.get("succeeded", 0)) + failed + skipped,
        succeeded_items=int(result.get("succeeded", 0)) + skipped,
        failed_items=failed,
        error_message=("图谱质量门禁未全部通过" if report.status != "ready" else None),
    )
    return HandlerResult(deferred_completion, attached_result, attached_error_code)


def handle_graph_maintenance(ctx):
    db, job, job_repo, mgr, container = ctx.db, ctx.job, ctx.job_repo, ctx.mgr, ctx.container
    job_id, heartbeat = ctx.job_id, ctx.heartbeat
    agent_context, agent_document_ids = ctx.agent_context, ctx.agent_document_ids
    agent_cancelled, agent_event_callback = ctx.agent_cancelled, ctx.agent_event_callback
    _link_followup_jobs = ctx.link_followup_jobs
    deferred_completion = attached_result = attached_error_code = None
    from app.services.graph_governance_service import GraphGovernanceService

    governance = GraphGovernanceService(
        db, job.knowledge_base_id, task_queue=container.task_queue
    )
    result = {}
    state = governance.community_state(create=False)
    if state is not None and state.status == "stale":
        result["communities"] = governance.rebuild_communities()
    report = governance.assess_quality(job.requested_by)
    job_repo.update(
        job,
        status="completed" if report.status == "ready" else "partial",
        total_items=1,
        processed_items=1,
        succeeded_items=1,
        failed_items=0,
        error_message=(
            None if report.status == "ready" else "图谱质量门禁未全部通过"
        ),
    )
    return HandlerResult(deferred_completion, attached_result, attached_error_code)


def handle_schema_generation(ctx):
    db, job, job_repo, mgr, container = ctx.db, ctx.job, ctx.job_repo, ctx.mgr, ctx.container
    job_id, heartbeat = ctx.job_id, ctx.heartbeat
    agent_context, agent_document_ids = ctx.agent_context, ctx.agent_document_ids
    agent_cancelled, agent_event_callback = ctx.agent_cancelled, ctx.agent_event_callback
    _link_followup_jobs = ctx.link_followup_jobs
    deferred_completion = attached_result = attached_error_code = None
    from app.services.graph_schema_induction_service import (
        GraphSchemaInductionService,
    )

    options = job.options_json or {}
    schema = GraphSchemaInductionService(
        db,
        job.knowledge_base_id,
        cancelled=agent_cancelled, agent_context=agent_context, on_event=agent_event_callback,
    ).generate_draft(
        created_by=job.requested_by,
        document_ids=agent_document_ids,
        instructions=options.get("instructions") or None,
        target_schema_id=options.get("target_schema_id") or None,
        graph_kind=options.get("graph_kind") or "general",
    )
    options = dict(options)
    options["generated_schema_id"] = schema.id
    options["generated_schema_version"] = schema.version
    job.options_json = options
    attached_result = {"schema_id": schema.id, "schema_version": schema.version,
                       "generation_summary": getattr(schema, "generation_summary_json", {})}
    job_repo.update(
        job,
        status="completed",
        total_items=len(schema.source_document_ids_json or []) or 1,
        processed_items=len(schema.source_document_ids_json or []) or 1,
        succeeded_items=len(schema.source_document_ids_json or []) or 1,
        failed_items=0,
    )
    return HandlerResult(deferred_completion, attached_result, attached_error_code)


def handle_schema_drift(ctx):
    db, job, job_repo, mgr, container = ctx.db, ctx.job, ctx.job_repo, ctx.mgr, ctx.container
    job_id, heartbeat = ctx.job_id, ctx.heartbeat
    agent_context, agent_document_ids = ctx.agent_context, ctx.agent_document_ids
    agent_cancelled, agent_event_callback = ctx.agent_cancelled, ctx.agent_event_callback
    _link_followup_jobs = ctx.link_followup_jobs
    deferred_completion = attached_result = attached_error_code = None
    from app.services.graph_schema_induction_service import (
        GraphSchemaInductionService,
    )

    options = dict(job.options_json or {})
    drift = GraphSchemaInductionService(
        db,
        job.knowledge_base_id,
        cancelled=agent_cancelled, agent_context=agent_context, on_event=agent_event_callback,
    ).detect_drift(
        created_by=job.requested_by,
        document_ids=agent_document_ids,
        instructions=options.get("instructions") or None,
        graph_kind=options.get("graph_kind") or "general",
    )
    options["drift_result"] = drift
    attached_result = drift
    job.options_json = options
    source_count = len(drift.get("source_document_ids") or []) or 1
    job_repo.update(
        job,
        status="completed",
        total_items=source_count,
        processed_items=source_count,
        succeeded_items=source_count,
        failed_items=0,
    )
    return HandlerResult(deferred_completion, attached_result, attached_error_code)


HANDLERS = {
    'agent_workflow': handle_agent_workflow,
    'sync': handle_sync,
    'rebuild': handle_rebuild,
    'file': handle_file,
    'delete': handle_file,
    'connector_sync': handle_connector_sync,
    'code_index': handle_code_index,
    'entity_vector_rebuild': handle_entity_vector_rebuild,
    'relation_vector_rebuild': handle_relation_vector_rebuild,
    'code_vector_rebuild': handle_code_vector_rebuild,
    'graph_rebuild': handle_graph_rebuild,
    'graph_maintenance': handle_graph_maintenance,
    'schema_generation': handle_schema_generation,
    'schema_drift': handle_schema_drift,
}


def execute_job(ctx):
    try:
        handler = HANDLERS[ctx.job.job_type]
    except KeyError:
        raise ValueError(f"Unsupported index job type: {ctx.job.job_type}") from None
    return handler(ctx)
