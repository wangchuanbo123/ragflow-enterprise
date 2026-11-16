"""Validated application actions. Models never receive database write access."""

import hashlib
import json
import re
import time
from sqlalchemy import select, update

from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.knowledge_document import KnowledgeDocument
from app.models.knowledge_graph import KgEntity, KgEntityMention, KgRelationEvidence
from app.models.index_job import IndexJob
from app.models.graph_governance import GraphReviewItem
from app.repositories.document_management_repository import DocumentManagementRepository
from app.schemas.agent_run import WorkflowAction
from app.services.knowledge_workflows.catalog import CATALOG


def fingerprint(action):
    return hashlib.sha256(json.dumps(action.model_dump(), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class WorkflowExecutor:
    def __init__(self, db, run, cancelled=None, on_event=None, context_factory=None):
        self.db, self.run, self.cancelled = db, run, cancelled or (lambda: False)
        self.knowledge_base_id = run.knowledge_base_id
        self.scope = set(run.document_ids_json)
        self.options = {**run.options_json, "graph_kind": "general"}
        self.on_event = on_event
        self._evaluation = None
        self.context_factory = context_factory

    def validate_plan(self, plan):
        if len(plan.actions) > self.options.get("max_actions", 10):
            raise ValueError("代理动作数量超过本次授权上限")
        allowed = self.options.get("allowed_actions") if self.options.get("allowed_actions") is not None else CATALOG[self.run.task][2]
        for action in plan.actions:
            if action.kind not in allowed or not set(action.document_ids).issubset(self.scope):
                raise ValueError("代理动作超出任务类别或文档授权范围")
            permitted_keys = {
                "reindex_documents": set(), "reextract_documents": set(), "rebuild_communities": set(), "evaluate_cases": set(),
                "sync_sources": {"source_ids"}, "create_schema_draft": {"entity_types", "relation_types", "normalization_rules", "extraction_prompt", "graph_profiles", "graph_kind"},
                "tag_documents": {"tags"}, "retry_jobs": {"job_ids"}, "adjust_batch": {"batch_size"},
                "select_model_profile": {"runtime_profile"},
                "propose_correction": {"action", "target_id", "payload", "reason"},
                "apply_correction": {"action", "target_id", "payload", "reason"},
            }
            if set(action.params) - permitted_keys[action.kind]:
                raise ValueError("业务动作包含未支持的参数")
            if action.kind == "create_schema_draft":
                from app.schemas.graph_governance import GraphSchemaCreate
                GraphSchemaCreate.model_validate({**action.params, "graph_kind": self.options["graph_kind"]})
            if action.kind in {"propose_correction", "apply_correction"}:
                self._validate_correction(action)
            if action.kind == "tag_documents":
                names = action.params.get("tags", [])
                if not isinstance(names, list) or not 1 <= len(names) <= 20 or any(not isinstance(name, str) or not 1 <= len(name.strip()) <= 64 for name in names):
                    raise ValueError("标签名称无效")
            if action.kind == "sync_sources":
                ids = action.params.get("source_ids") or self.options.get("source_ids") or ([self.options["data_source_id"]] if self.options.get("data_source_id") else [])
                permitted = set(self.options.get("source_ids") or []) | {self.options.get("data_source_id")}
                if not isinstance(ids, list) or not 1 <= len(ids) <= 20 or not set(ids).issubset(permitted):
                    raise ValueError("同步源超出用户指定范围")
            if action.kind == "retry_jobs" and not set(action.params.get("job_ids") or []).issubset(self.options.get("job_ids") or []):
                raise ValueError("重试任务超出用户指定范围")
            if action.kind == "adjust_batch":
                value = action.params.get("batch_size")
                if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 100:
                    raise ValueError("批次大小必须为1–100")
            if action.kind == "select_model_profile":
                from app.agents.factory import list_backend_profiles
                if action.params.get("runtime_profile") not in list_backend_profiles():
                    raise ValueError("模型配置不在服务器白名单")
        for evidence in plan.evidence:
            chunk = self.db.scalar(select(KnowledgeChunk).where(KnowledgeChunk.id == evidence.chunk_id,
                KnowledgeChunk.knowledge_base_id == self.knowledge_base_id, KnowledgeChunk.document_id.in_(self.scope)))
            if (chunk is None or evidence.quote not in chunk.content or
                (evidence.document_id and evidence.document_id != chunk.document_id)):
                raise ValueError("输出证据不存在、引文不匹配或超出任务范围")

    def default_actions(self, plan):
        if plan.actions or self.options.get("allowed_actions") == []:
            return
        documents = list(self.run.document_ids_json)
        if self.run.task in {"extraction", "incremental_maintenance"} and documents:
            plan.actions.append(WorkflowAction(kind="reextract_documents", document_ids=documents))
        elif self.run.task == "ingestion":
            sources = list(self.options.get("source_ids") or [])
            if self.options.get("data_source_id"):
                sources.append(self.options["data_source_id"])
            if sources:
                plan.actions.append(WorkflowAction(kind="sync_sources", params={"source_ids": list(dict.fromkeys(sources))}))
            elif documents:
                plan.actions.append(WorkflowAction(kind="reindex_documents", document_ids=documents))
        elif self.run.task == "knowledge_organization":
            plan.actions.append(WorkflowAction(kind="rebuild_communities", document_ids=documents))
        if self.options.get("allowed_actions") is not None:
            plan.actions[:] = [action for action in plan.actions if action.kind in self.options["allowed_actions"]]

    def diagnostics(self):
        documents = list(self.db.scalars(select(KnowledgeDocument).where(KnowledgeDocument.id.in_(self.scope),
                            KnowledgeDocument.knowledge_base_id == self.knowledge_base_id)))
        chunks = list(self.db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.document_id.in_(self.scope),
                            KnowledgeChunk.knowledge_base_id == self.knowledge_base_id)))
        return {"documents": len(documents), "chunks": len(chunks), "document_statuses": {
            status: sum(row.status == status for row in documents) for status in sorted({row.status for row in documents})},
            "failed_chunks": sum(row.graph_status == "failed" for row in chunks),
            "empty_chunks": sum(not row.content.strip() for row in chunks),
            "duplicate_chunk_hashes": len(chunks) - len({row.content_hash for row in chunks}),
            "document_versions": {row.id: {"version": row.current_version_no, "file_hash": row.file_hash} for row in documents}}

    def evaluate(self):
        if self._evaluation is not None:
            return self._evaluation
        from app.services import rag_service
        cases = list(self.options.get("gold_cases") or [])
        cases.extend({"question": question} for question in self.options.get("questions") or [])
        rows = []
        retrieval_options = {"scope": {"document_ids": list(self.scope), "knowledge_base_ids": [self.knowledge_base_id]}}
        for case in cases[:20]:
            if self.cancelled():
                break
            started = time.perf_counter()
            try:
                result = rag_service.inspect_retrieval(case["question"], knowledge_base_id=self.knowledge_base_id,
                                                       retrieval_options=retrieval_options)
                sources = result.get("sources") or []
                expected = set(case.get("expected_chunk_ids") or [])
                by_chunk = bool(expected)
                expected = expected or set(case.get("expected_document_ids") or [])
                hits = [source.get("chunk_id") if by_chunk else source.get("document_id") for source in sources]
                found = set(hits) & expected
                row = {"question": case["question"], "sources": sources,
                       "precision": len(found) / len(hits) if expected and hits else (0 if expected else None),
                       "recall": len(found) / len(expected) if expected else None,
                       "mrr": next((1 / (index + 1) for index, item in enumerate(hits) if item in expected), 0) if expected else None,
                       "status": "completed"}
                row["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
            except Exception as exc:
                row = {"question": case["question"], "status": "failed", "error_type": type(exc).__name__,
                       "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)}
                has_gold = bool(case.get("expected_chunk_ids") or case.get("expected_document_ids"))
                row.update({"precision": 0 if has_gold else None, "recall": 0 if has_gold else None, "mrr": 0 if has_gold else None})
            row["case_provenance"] = case.get("provenance", "unlabeled_query")
            row["source_run_id"] = case.get("source_run_id")
            rows.append(row)
        measured = [row["recall"] for row in rows if row.get("recall") is not None]
        self._evaluation = {"cases": rows, "case_count": len(rows), "completed": sum(row["status"] == "completed" for row in rows),
                "mean_recall": sum(measured) / len(measured) if measured else None,
                "elapsed_ms": round(sum(row["elapsed_ms"] for row in rows), 2),
                "cost": None, "cost_note": "Provider inference cost is unavailable unless supplied by its usage response"}
        self._evaluation["reference_set"] = "contains_agent_generated_candidates" if any(row["case_provenance"] == "agent_generated" for row in rows) else "user_supplied"
        return self._evaluation

    def execute_action(self, action):
        ids = action.document_ids or list(self.run.document_ids_json)
        params = action.params
        if action.kind == "reindex_documents":
            from app.services.document_index_service import DocumentIndexService
            kb = self.db.get(KnowledgeBase, self.knowledge_base_id)
            batch_size = int(((kb.settings_json or {}).get("agent_workflows") or {}).get("batch_size", 100))
            jobs = []
            for start in range(0, len(ids), batch_size):
                job = DocumentIndexService(self.db, self.knowledge_base_id).request_batch(ids[start:start + batch_size], "reindex", requested_by=self.run.requested_by)
                jobs.append(job.id)
            return {"job_ids": jobs, "document_ids": ids, "completion": "queued"}
        if action.kind == "sync_sources":
            from app.services.data_source_service import DataSourceService
            sources = params.get("source_ids") or self.options.get("source_ids") or ([self.options["data_source_id"]] if self.options.get("data_source_id") else [])
            return {"sources": [DataSourceService(self.db, self.knowledge_base_id).request_sync(source, self.run.requested_by) for source in sources]}
        if action.kind == "create_schema_draft":
            from app.schemas.graph_governance import GraphSchemaCreate
            from app.services.graph_governance_service import GraphGovernanceService
            schema = GraphSchemaCreate.model_validate({**params, "graph_kind": self.options["graph_kind"]})
            service = GraphGovernanceService(self.db, self.knowledge_base_id)
            draft = service.create_schema(**schema.model_dump(), created_by=self.run.requested_by,
                generation_method="agent_workflow", source_document_ids=list(self.scope), generation_summary={"agent_run_id": self.run.id})
            return {"schema_id": draft.id, "version": draft.version, "status": "draft",
                    "impact": service.estimate_schema_rebuild(draft.id), "activated": False}
        if action.kind in {"propose_correction", "apply_correction"}:
            return self._quality_action(action)
        if action.kind == "reextract_documents":
            from app.services.knowledge_graph_service import GraphBuildService
            requested_ids = list(ids)
            current_versions = self.diagnostics()["document_versions"]
            if self.run.task == "incremental_maintenance":
                old = self.incremental_baselines(ids)
                ids = [identifier for identifier in ids if old.get(identifier) != current_versions.get(identifier)]
            if not ids:
                return {"unchanged": True, "document_ids": [], "verified_document_ids": requested_ids,
                        "document_versions": {identifier: current_versions[identifier] for identifier in requested_ids if identifier in current_versions}}
            if self.options["mode"] == "full" or self.run.task == "incremental_maintenance":
                self.db.execute(update(KnowledgeChunk).where(KnowledgeChunk.knowledge_base_id == self.knowledge_base_id,
                    KnowledgeChunk.document_id.in_(ids)).values(graph_status="pending", graph_error=None))
                self.db.commit()
            result = GraphBuildService(self.db, knowledge_base_id=self.knowledge_base_id).build_graph(
                document_ids=ids, only_pending=self.options["mode"] != "full", retry_failed=self.options["mode"] == "retry_failed" or bool(self.run.parent_run_id),
                cancelled=self.cancelled, agent_context=self.context_factory() if self.context_factory else {}, on_event=self.on_event)
            from app.services.graph_governance_service import GraphGovernanceService
            from app.services.entity_vector_service import EntityVectorService
            from app.services.relation_vector_service import RelationVectorService
            GraphGovernanceService(self.db, self.knowledge_base_id).mark_communities_stale()
            vector_jobs = [EntityVectorService(self.db, self.knowledge_base_id).request_rebuild(self.run.requested_by),
                           RelationVectorService(self.db, self.knowledge_base_id).request_rebuild(self.run.requested_by)]
            chunks = list(self.db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.knowledge_base_id == self.knowledge_base_id,
                                                                       KnowledgeChunk.document_id.in_(ids))))
            completed = [identifier for identifier in ids if any(chunk.document_id == identifier for chunk in chunks)
                and all(chunk.graph_status == "completed" and chunk.file_hash == current_versions[identifier]["file_hash"]
                        for chunk in chunks if chunk.document_id == identifier)]
            return {"document_ids": ids, "completed_document_ids": completed,
                    "document_versions": {identifier: current_versions[identifier] for identifier in completed},
                    "graph": result, "job_ids": [job.id for job in vector_jobs]}
        if action.kind == "tag_documents":
            repository = DocumentManagementRepository(self.db, self.knowledge_base_id)
            names = params.get("tags", [])
            if not isinstance(names, list) or not 1 <= len(names) <= 20 or any(not isinstance(name, str) or not 1 <= len(name.strip()) <= 64 for name in names):
                raise ValueError("标签须为 1–20 个有效名称")
            tags = {tag.name: tag for tag in repository.list_tags()}
            chosen = [tags[name].id if name in tags else repository.create_tag(name=name).id for name in dict.fromkeys(n.strip() for n in names)]
            for identifier in ids:
                repository.set_document_tags([identifier], list(set(repository.list_tag_ids_for_document(identifier)) | set(chosen)))
            return {"document_ids": ids, "tag_ids": chosen}
        if action.kind == "rebuild_communities":
            from app.services.graph_governance_service import GraphGovernanceService
            entities = list(self.db.scalars(select(KgEntityMention.entity_id).where(KgEntityMention.knowledge_base_id == self.knowledge_base_id,
                                                                                 KgEntityMention.document_id.in_(ids)).distinct()))
            if not entities:
                return {"unchanged": True, "entities": 0, "reason": "selected_scope_contains_no_entities"}
            return GraphGovernanceService(self.db, self.knowledge_base_id).rebuild_communities(changed_entity_ids=entities)
        if action.kind == "evaluate_cases":
            return self.evaluate()
        if action.kind == "retry_jobs":
            from app.repositories.index_job_repository import IndexJobRepository, signal_persisted_job
            identifiers = params.get("job_ids") or self.options.get("job_ids") or []
            if len(identifiers) > 20:
                raise ValueError("单次最多重试 20 个任务")
            retried = []
            for identifier in identifiers:
                job = IndexJobRepository(self.db, self.knowledge_base_id).get(identifier)
                if job is None or job.job_type == "agent_workflow" or job.status not in {"failed", "cancelled", "partial"}:
                    raise ValueError("只能重试指定的失败业务任务")
                options = dict(job.options_json or {})
                count = int(options.get("agent_retry_count", 0))
                if count >= 2:
                    raise ValueError("该任务已达到代理重试上限")
                options["agent_retry_count"] = count + 1
                job.options_json = options
                self.db.commit()
                job = IndexJobRepository(self.db, self.knowledge_base_id).retry(identifier)
                from app.bootstrap.container import get_provider_container
                signal_persisted_job(get_provider_container().task_queue, job.id)
                retried.append(job.id)
            return {"job_ids": retried}
        if action.kind in {"adjust_batch", "select_model_profile"}:
            kb = self.db.get(KnowledgeBase, self.knowledge_base_id)
            settings = dict(kb.settings_json or {})
            workflow = dict(settings.get("agent_workflows") or {})
            if action.kind == "adjust_batch":
                value = params.get("batch_size")
                if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 100:
                    raise ValueError("批次大小必须为 1–100")
                workflow["batch_size"] = value
            else:
                from app.agents.factory import list_backend_profiles
                profile = params.get("runtime_profile")
                if profile not in list_backend_profiles():
                    raise ValueError("只能选择服务器已登记的备用模型")
                workflow["runtime_profile"] = profile
            settings["agent_workflows"] = workflow
            kb.settings_json = settings
            self.db.commit()
            return {"settings": workflow, "effective": "subsequent workflows"}
        raise ValueError("不支持的应用动作")

    def incremental_baselines(self, document_ids):
        """Newest completed extraction per document, never a planning snapshot."""
        from app.models.agent_run import AgentRun
        remaining = set(document_ids)
        baselines = {}
        history = self.db.scalars(select(AgentRun).where(
            AgentRun.knowledge_base_id == self.knowledge_base_id, AgentRun.task == "incremental_maintenance",
            AgentRun.status == "succeeded", AgentRun.id != self.run.id,
            AgentRun.options_json["apply"].as_boolean().is_(True),
        ).order_by(AgentRun.finished_at.desc(), AgentRun.created_at.desc(), AgentRun.id.desc()).execution_options(yield_per=100))
        for previous in history:
            for action in (previous.output_json or {}).get("actions", []):
                if action.get("kind") != "reextract_documents" or action.get("status") not in {"applied", "already_applied", "unchanged"}:
                    continue
                result = action.get("result") or {}
                graph = result.get("graph") or {}
                if graph.get("failed", 0):
                    continue
                if "completed_document_ids" in result:
                    completed = set(result["completed_document_ids"])
                elif result.get("unchanged") and "verified_document_ids" in result:
                    completed = set(result["verified_document_ids"])
                elif graph.get("succeeded", 0) or graph.get("skipped", 0):
                    # Compatibility for early runs which recorded the actual
                    # extraction result but not per-document completion proof.
                    completed = set(result.get("document_ids") or [])
                else:
                    continue
                versions = result.get("document_versions") or (previous.context_json or {}).get("document_versions", {})
                for identifier in completed & remaining & set(previous.document_ids_json):
                    if identifier in versions:
                        baselines[identifier] = versions[identifier]
                        remaining.remove(identifier)
            if not remaining:
                break
        return baselines

    def _quality_action(self, action):
        from app.services.graph_governance_service import GraphGovernanceService
        correction = self._validate_correction(action)
        if action.kind == "propose_correction":
            review = GraphReviewItem(knowledge_base_id=self.knowledge_base_id, item_type="agent_correction_candidate",
                target_id=correction.target_id, payload_json={**correction.model_dump(), "agent_run_id": self.run.id},
                reason=correction.reason, confidence=0.0, status="pending")
            self.db.add(review)
            self.db.commit()
            return {"review_id": review.id, "status": "pending"}
        record = GraphGovernanceService(self.db, self.knowledge_base_id).create_correction(**correction.model_dump(), created_by=self.run.requested_by)
        return {"correction_id": record.id, "status": record.status, "reversible": True}

    def _validate_correction(self, action):
        from app.schemas.knowledge_graph import GraphCorrectionCreate
        correction = GraphCorrectionCreate.model_validate(action.params)
        if correction.action not in {"hide_relation", "update_relation", "update_entity", "add_entity", "add_relation"}:
            raise ValueError("代理不允许执行该校正类型")
        entity_ids = set(self.db.scalars(select(KgEntityMention.entity_id).where(KgEntityMention.knowledge_base_id == self.knowledge_base_id,
                                                                             KgEntityMention.document_id.in_(self.scope))))
        relation_ids = set(self.db.scalars(select(KgRelationEvidence.relation_id).where(KgRelationEvidence.knowledge_base_id == self.knowledge_base_id,
                                                                                     KgRelationEvidence.document_id.in_(self.scope))))
        if correction.target_id and correction.target_id not in entity_ids | relation_ids:
            raise ValueError("校正目标超出文档范围")
        if correction.action == "update_entity" and correction.target_id not in entity_ids:
            raise ValueError("实体校正目标超出范围")
        if correction.action in {"update_relation", "hide_relation"} and correction.target_id not in relation_ids:
            raise ValueError("关系校正目标超出范围")
        payload = correction.payload
        for key in ("subject_entity_id", "object_entity_id"):
            if key in payload and payload[key] not in entity_ids:
                raise ValueError("关系端点超出任务范围")
        if "document_id" in payload and payload["document_id"] not in self.scope:
            raise ValueError("校正证据文档超出范围")
        if correction.action.startswith("add_"):
            chunk = self.db.scalar(select(KnowledgeChunk).where(KnowledgeChunk.id == payload.get("chunk_id"),
               KnowledgeChunk.knowledge_base_id == self.knowledge_base_id, KnowledgeChunk.document_id.in_(self.scope)))
            if not chunk or payload.get("document_id") != chunk.document_id or not payload.get("evidence") or payload["evidence"] not in chunk.content:
                raise ValueError("新增知识必须提供范围内的精确原文证据")
            for key in ("subject_entity_id", "object_entity_id"):
                if key in payload and payload[key] not in entity_ids:
                    raise ValueError("关系端点超出任务范围")
        return correction
