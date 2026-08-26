"""Scoped graph build, schema, quality, and human-governance APIs."""

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.api.errors import bad_request, forbidden, not_found
from app.core.database import get_db
from app.models.user import User
from app.repositories.knowledge_base_repository import KnowledgeBaseRepository
from app.schemas.document import IndexJobOut
from app.schemas.graph_governance import (
    CommunityOut,
    CommunityStateOut,
    EntityMergeCreate,
    EntityMergeOut,
    EntityVectorStateOut,
    GraphAutomationSettings,
    GraphBuildRequest,
    GraphQualityOut,
    GraphSchemaCreate,
    GraphSchemaImpactOut,
    GraphSchemaOut,
    GraphSchemaUpdate,
    RelationMergeCreate,
    ReviewItemOut,
    ReviewResolve,
    RelationVectorStateOut,
)
from app.schemas.knowledge_graph import GraphCorrectionOut
from app.services.entity_vector_service import EntityVectorService
from app.services.graph_governance_service import GraphGovernanceService
from app.services.relation_vector_service import RelationVectorService

router = APIRouter(prefix="/knowledge-bases", tags=["graph-governance"])


def _require(db: Session, knowledge_base_id: str, user: User, *, edit: bool = False) -> None:
    repository = KnowledgeBaseRepository(db)
    allowed = (
        repository.can_edit(knowledge_base_id, user.id, is_admin=user.role == "admin")
        if edit else repository.can_read(knowledge_base_id, user.id, is_admin=user.role == "admin")
    )
    if not allowed:
        if repository.get(knowledge_base_id) is None:
            raise not_found("知识库不存在")
        raise forbidden("没有该知识库的访问权限")


@router.get("/{knowledge_base_id}/knowledge-graph/schemas", response_model=list[GraphSchemaOut])
def list_graph_schemas(knowledge_base_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require(db, knowledge_base_id, current_user)
    service = GraphGovernanceService(db, knowledge_base_id)
    service.active_schema()
    return service.list_schemas()


@router.post("/{knowledge_base_id}/knowledge-graph/schemas", response_model=GraphSchemaOut, status_code=201)
def create_graph_schema(knowledge_base_id: str, payload: GraphSchemaCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require(db, knowledge_base_id, current_user, edit=True)
    try:
        return GraphGovernanceService(db, knowledge_base_id).create_schema(
            entity_types=payload.entity_types,
            relation_types=payload.relation_types,
            normalization_rules=payload.normalization_rules,
            extraction_prompt=payload.extraction_prompt,
            created_by=current_user.id,
        )
    except ValueError as exc:
        raise bad_request(str(exc)) from exc


@router.put("/{knowledge_base_id}/knowledge-graph/schemas/{schema_id}", response_model=GraphSchemaOut)
def update_graph_schema(knowledge_base_id: str, schema_id: str, payload: GraphSchemaUpdate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require(db, knowledge_base_id, current_user, edit=True)
    try:
        schema = GraphGovernanceService(db, knowledge_base_id).update_schema(
            schema_id,
            entity_types=payload.entity_types,
            relation_types=payload.relation_types,
            normalization_rules=payload.normalization_rules,
            extraction_prompt=payload.extraction_prompt,
        )
    except ValueError as exc:
        raise bad_request(str(exc)) from exc
    if schema is None:
        raise not_found("图谱 Schema 不存在")
    return schema


@router.get("/{knowledge_base_id}/knowledge-graph/automation", response_model=GraphAutomationSettings)
def graph_automation_settings(knowledge_base_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require(db, knowledge_base_id, current_user)
    return GraphGovernanceService(db, knowledge_base_id).automation_settings()


@router.put("/{knowledge_base_id}/knowledge-graph/automation", response_model=GraphAutomationSettings)
def update_graph_automation_settings(knowledge_base_id: str, payload: GraphAutomationSettings, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require(db, knowledge_base_id, current_user, edit=True)
    try:
        return GraphGovernanceService(db, knowledge_base_id).update_automation_settings(
            auto_build_enabled=payload.auto_build_enabled
        )
    except ValueError as exc:
        raise bad_request(str(exc)) from exc


@router.get("/{knowledge_base_id}/knowledge-graph/schemas/{schema_id}/impact", response_model=GraphSchemaImpactOut)
def estimate_graph_schema_impact(knowledge_base_id: str, schema_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """激活前的差异化重建影响估算：受影响文档/Chunk 与预计 LLM 请求。"""
    _require(db, knowledge_base_id, current_user, edit=True)
    estimate = GraphGovernanceService(db, knowledge_base_id).estimate_schema_rebuild(schema_id)
    if estimate is None:
        raise not_found("图谱 Schema 不存在")
    return estimate


@router.post("/{knowledge_base_id}/knowledge-graph/schemas/{schema_id}/activate", response_model=GraphSchemaOut)
def activate_graph_schema(
    knowledge_base_id: str,
    schema_id: str,
    reprocess: str = Query(default="auto", pattern="^(auto|incremental|full)$"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require(db, knowledge_base_id, current_user, edit=True)
    try:
        schema = GraphGovernanceService(db, knowledge_base_id).activate_schema(
            schema_id, reprocess=reprocess
        )
    except ValueError as exc:
        raise bad_request(str(exc)) from exc
    if schema is None:
        raise not_found("图谱 Schema 不存在")
    return schema


@router.post("/{knowledge_base_id}/knowledge-graph/build", response_model=IndexJobOut, status_code=status.HTTP_202_ACCEPTED)
def request_graph_build(knowledge_base_id: str, payload: GraphBuildRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require(db, knowledge_base_id, current_user, edit=True)
    service = GraphGovernanceService(db, knowledge_base_id)
    if payload.profile is not None:
        return service.request_build(requested_by=current_user.id, profile=payload.profile)
    vector_update = None
    if payload.rebuild_entity_vectors is not None or payload.rebuild_relation_vectors is not None:
        full_vectors = bool(
            (payload.rebuild_entity_vectors if payload.rebuild_entity_vectors is not None else True)
            and (payload.rebuild_relation_vectors if payload.rebuild_relation_vectors is not None else True)
        )
        vector_update = "full" if full_vectors else "none"
    community_update = None
    if payload.rebuild_communities is not None:
        community_update = "full" if payload.rebuild_communities else "defer"
    return service.request_build(
        payload.mode,
        current_user.id,
        vector_update=vector_update,
        community_update=community_update,
    )


@router.get("/{knowledge_base_id}/knowledge-graph/quality", response_model=GraphQualityOut | None)
def latest_graph_quality(knowledge_base_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require(db, knowledge_base_id, current_user)
    return GraphGovernanceService(db, knowledge_base_id).latest_quality()


@router.post("/{knowledge_base_id}/knowledge-graph/quality", response_model=GraphQualityOut)
def assess_graph_quality(knowledge_base_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require(db, knowledge_base_id, current_user, edit=True)
    return GraphGovernanceService(db, knowledge_base_id).assess_quality(current_user.id)


@router.get("/{knowledge_base_id}/knowledge-graph/entity-vector", response_model=EntityVectorStateOut)
def entity_vector_state(knowledge_base_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require(db, knowledge_base_id, current_user)
    state = EntityVectorService(db, knowledge_base_id).state()
    return state or EntityVectorStateOut()


@router.post("/{knowledge_base_id}/knowledge-graph/entity-vector/rebuild", response_model=IndexJobOut, status_code=202)
def rebuild_entity_vectors(knowledge_base_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require(db, knowledge_base_id, current_user, edit=True)
    return EntityVectorService(db, knowledge_base_id).request_rebuild(current_user.id)


@router.get("/{knowledge_base_id}/knowledge-graph/relation-vector", response_model=RelationVectorStateOut)
def relation_vector_state(knowledge_base_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require(db, knowledge_base_id, current_user)
    state = RelationVectorService(db, knowledge_base_id).state()
    return state or RelationVectorStateOut()


@router.post("/{knowledge_base_id}/knowledge-graph/relation-vector/rebuild", response_model=IndexJobOut, status_code=202)
def rebuild_relation_vectors(knowledge_base_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require(db, knowledge_base_id, current_user, edit=True)
    return RelationVectorService(db, knowledge_base_id).request_rebuild(current_user.id)


@router.get("/{knowledge_base_id}/knowledge-graph/reviews", response_model=list[ReviewItemOut])
def list_graph_reviews(knowledge_base_id: str, review_status: str = Query(default="pending", alias="status"), current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require(db, knowledge_base_id, current_user)
    return GraphGovernanceService(db, knowledge_base_id).list_reviews(review_status)


@router.post("/{knowledge_base_id}/knowledge-graph/reviews/{review_id}/resolve", response_model=ReviewItemOut)
def resolve_graph_review(knowledge_base_id: str, review_id: str, payload: ReviewResolve, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require(db, knowledge_base_id, current_user, edit=True)
    item = GraphGovernanceService(db, knowledge_base_id).resolve_review(
        review_id, payload.decision, current_user.id
    )
    if item is None:
        raise not_found("复核项不存在")
    return item


@router.get("/{knowledge_base_id}/knowledge-graph/merges", response_model=list[EntityMergeOut])
def list_entity_merges(knowledge_base_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require(db, knowledge_base_id, current_user)
    return GraphGovernanceService(db, knowledge_base_id).list_merges()


@router.post("/{knowledge_base_id}/knowledge-graph/merge-candidates/scan")
def scan_entity_merge_candidates(knowledge_base_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require(db, knowledge_base_id, current_user, edit=True)
    return GraphGovernanceService(db, knowledge_base_id).scan_semantic_merge_candidates()


@router.post("/{knowledge_base_id}/knowledge-graph/merges", response_model=EntityMergeOut, status_code=201)
def create_entity_merge(knowledge_base_id: str, payload: EntityMergeCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require(db, knowledge_base_id, current_user, edit=True)
    try:
        return GraphGovernanceService(db, knowledge_base_id).merge_entities(
            payload.canonical_entity_id, payload.merged_entity_id,
            payload.reason, current_user.id,
        )
    except ValueError as exc:
        raise bad_request(str(exc)) from exc


@router.post("/{knowledge_base_id}/knowledge-graph/merges/{merge_id}/revert", response_model=EntityMergeOut)
def revert_entity_merge(knowledge_base_id: str, merge_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require(db, knowledge_base_id, current_user, edit=True)
    record = GraphGovernanceService(db, knowledge_base_id).revert_merge(merge_id, current_user.id)
    if record is None:
        raise not_found("实体合并记录不存在")
    return record


@router.get("/{knowledge_base_id}/knowledge-graph/relation-merges", response_model=list[GraphCorrectionOut])
def list_relation_merges(knowledge_base_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require(db, knowledge_base_id, current_user)
    return GraphGovernanceService(db, knowledge_base_id).list_relation_merges()


@router.post("/{knowledge_base_id}/knowledge-graph/relation-merge-candidates/scan")
def scan_relation_merge_candidates(knowledge_base_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require(db, knowledge_base_id, current_user, edit=True)
    return GraphGovernanceService(db, knowledge_base_id).scan_relation_merge_candidates()


@router.post("/{knowledge_base_id}/knowledge-graph/relation-merges", response_model=GraphCorrectionOut, status_code=201)
def create_relation_merge(knowledge_base_id: str, payload: RelationMergeCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require(db, knowledge_base_id, current_user, edit=True)
    try:
        return GraphGovernanceService(db, knowledge_base_id).merge_relations(
            payload.canonical_relation_id,
            payload.merged_relation_id,
            payload.reason,
            current_user.id,
        )
    except ValueError as exc:
        raise bad_request(str(exc)) from exc


@router.post("/{knowledge_base_id}/knowledge-graph/relation-merges/{merge_id}/revert", response_model=GraphCorrectionOut)
def revert_relation_merge(knowledge_base_id: str, merge_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require(db, knowledge_base_id, current_user, edit=True)
    record = GraphGovernanceService(db, knowledge_base_id).revert_relation_merge(
        merge_id, current_user.id
    )
    if record is None or record.action != "merge_relation":
        raise not_found("关系合并记录不存在")
    return record


@router.get("/{knowledge_base_id}/knowledge-graph/communities", response_model=list[CommunityOut])
def list_graph_communities(knowledge_base_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require(db, knowledge_base_id, current_user)
    return GraphGovernanceService(db, knowledge_base_id).list_communities()


@router.get("/{knowledge_base_id}/knowledge-graph/community-status", response_model=CommunityStateOut)
def graph_community_status(knowledge_base_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require(db, knowledge_base_id, current_user)
    state = GraphGovernanceService(db, knowledge_base_id).community_state(create=False)
    return state or CommunityStateOut()


@router.post("/{knowledge_base_id}/knowledge-graph/communities/rebuild")
def rebuild_graph_communities(knowledge_base_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require(db, knowledge_base_id, current_user, edit=True)
    return GraphGovernanceService(db, knowledge_base_id).rebuild_communities()
