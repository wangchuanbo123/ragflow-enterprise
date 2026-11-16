"""Single knowledge-base graph descriptor; legacy view templates are retired."""
from copy import deepcopy
BUSINESS_GRAPH_PROFILE_ORDER = ()
GRAPH_PROFILE_ORDER = ('general',)
GRAPH_PROFILES = {}
GENERAL_GRAPH_PROFILE = {'key': 'general', 'label': '知识库总图谱', 'short_label': '总图谱', 'purpose': '覆盖知识库全部已解析文档的实体、关系与来源证据。', 'positioning': '使用唯一活动Schema；按实际类型、文档和口语条件筛选同一总图谱。', 'recommended_layout': 'cose', 'color': '#64748b', 'document_hints': ['全部已解析文档'], 'extraction_rules': ['遵守唯一活动Schema的类型、关系方向与成对端点约束。', '保留来源文档、Chunk和逐字证据，不根据常识补充事实。'], 'entity_types': [], 'relation_types': []}

def profile_catalog():
    return [deepcopy(GENERAL_GRAPH_PROFILE)]

def get_graph_profile(kind):
    if kind != 'general':
        raise ValueError('独立专业图谱已停用，请使用知识库总图谱和筛选条件')
    return deepcopy(GENERAL_GRAPH_PROFILE)

def profile_schema(kind):
    get_graph_profile(kind)
    from rag.knowledge_graph.schemas import VALID_ENTITY_TYPES, VALID_PREDICATES
    return {'entity_types': sorted(VALID_ENTITY_TYPES), 'relation_types': sorted(VALID_PREDICATES), 'graph_profiles': None, 'extraction_prompt': None}

def composite_profile_schema(kinds=None):
    for kind in kinds or ['general']:
        get_graph_profile(kind)
    return profile_schema('general')

def profile_type_names(kind):
    get_graph_profile(kind)
    return (set(), set())

def project_subgraph(payload, kind):
    get_graph_profile(kind)
    return {**payload, 'graph_kind': 'general'}

def profiles_for_schema(entity_types, relation_types, entity_details=None, relation_details=None):
    profile = get_graph_profile('general')
    entities = {item.get('name'): item for item in entity_details or []}
    relations = {item.get('name'): item for item in relation_details or []}
    profile['entity_types'] = [{'name': name, 'label': entities.get(name, {}).get('description') or name, 'color': profile['color']} for name in entity_types]
    profile['relation_types'] = [{'name': name, 'label': relations.get(name, {}).get('description') or name, 'source_types': relations.get(name, {}).get('source_types', []), 'target_types': relations.get(name, {}).get('target_types', [])} for name in relation_types]
    return {'general': profile}
