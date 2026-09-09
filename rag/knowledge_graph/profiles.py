"""Built-in business graph perspectives derived from the avionics prototype.

The four perspectives share one canonical entity/relation store.  A profile is
therefore a projection and an extraction contract, not a second copy of data.
"""

from __future__ import annotations

from copy import deepcopy


BUSINESS_GRAPH_PROFILE_ORDER = ("concept", "functional", "configuration", "standard")
GRAPH_PROFILE_ORDER = ("general", *BUSINESS_GRAPH_PROFILE_ORDER)

GENERAL_GRAPH_PROFILE = {
    "key": "general",
    "label": "通用文档图谱",
    "short_label": "通用文档",
    "purpose": "保留原有按知识库、文档查看全部实体与关系的通用知识图谱能力。",
    "positioning": "回答文档中出现了哪些对象、对象之间有什么关系，是不限定业务视角的完整入口。",
    "recommended_layout": "cose",
    "color": "#64748b",
    "document_hints": ["全部已解析文档"],
    "extraction_rules": [
        "遵循知识库当前通用 Schema 抽取实体和关系。",
        "保留来源文档、Chunk 和证据位置，支持按文档查看与人工治理。",
        "不按专业视角过滤；专业图谱由各自独立 Schema 进行投影。",
    ],
    "entity_types": [],
    "relation_types": [],
}


GRAPH_PROFILES: dict[str, dict] = {
    "concept": {
        "key": "concept",
        "label": "概念图谱",
        "short_label": "概念",
        "purpose": "统一领域术语、概念层级和适用范围，不表示某个项目中的具体实例。",
        "positioning": "回答“它是什么、属于什么、与哪些概念相关”，作为其他业务图谱的语义入口。",
        "recommended_layout": "concentric",
        "color": "#4776bd",
        "document_hints": ["术语", "概念", "总体", "领域", "定义", "架构说明"],
        "extraction_rules": [
            "只抽取可复用的领域概念，不把项目编号、具体设备实例或文档标题当成概念。",
            "优先保留系统—分系统—软件/传感器/设备概念层级，简称写入别名。",
            "IS_A 表示下位概念到上位概念；CONCEPT_CONTAINS 表示整体到组成概念。",
        ],
        "entity_types": [
            {"name": "AVIONICS_SYSTEM", "label": "航电系统概念", "color": "#4776bd"},
            {"name": "SUBSYSTEM", "label": "分系统概念", "color": "#339cff"},
            {"name": "SOFTWARE_CONCEPT", "label": "软件概念", "color": "#8b6de9"},
            {"name": "SENSOR_CONCEPT", "label": "传感器概念", "color": "#5dc977"},
            {"name": "EQUIPMENT_CONCEPT", "label": "设备概念", "color": "#d87932"},
            {"name": "TERM", "label": "领域术语", "color": "#3ab9b1"},
            {"name": "SYSTEM", "label": "系统概念（兼容）", "color": "#4776bd"},
        ],
        "relation_types": [
            {"name": "CONCEPT_CONTAINS", "label": "概念包含", "source_types": ["AVIONICS_SYSTEM", "SUBSYSTEM", "SYSTEM"], "target_types": ["SUBSYSTEM", "SOFTWARE_CONCEPT", "SENSOR_CONCEPT", "EQUIPMENT_CONCEPT", "TERM"]},
            {"name": "CONCEPT_USES", "label": "概念使用", "source_types": ["SUBSYSTEM", "SOFTWARE_CONCEPT", "SYSTEM"], "target_types": ["SENSOR_CONCEPT", "EQUIPMENT_CONCEPT", "SOFTWARE_CONCEPT", "TERM"]},
            {"name": "MOUNTED_ON", "label": "安装于", "source_types": ["SENSOR_CONCEPT", "EQUIPMENT_CONCEPT"], "target_types": ["EQUIPMENT_CONCEPT"]},
            {"name": "IS_A", "label": "是一种", "source_types": ["SUBSYSTEM", "SOFTWARE_CONCEPT", "SENSOR_CONCEPT", "EQUIPMENT_CONCEPT", "TERM"], "target_types": ["AVIONICS_SYSTEM", "SUBSYSTEM", "SOFTWARE_CONCEPT", "SENSOR_CONCEPT", "EQUIPMENT_CONCEPT", "TERM", "SYSTEM"]},
            {"name": "RELATED_TO", "label": "概念相关", "source_types": [], "target_types": []},
        ],
    },
    "functional": {
        "key": "functional",
        "label": "系统功能图谱",
        "short_label": "系统功能",
        "purpose": "表达系统设计基线、业务/作战能力及其向配置项和需求的分配关系。",
        "positioning": "回答“系统要做什么、由哪个配置项承接”，连接系统室设计与软件室交付。",
        "recommended_layout": "breadthfirst",
        "color": "#339cff",
        "document_hints": ["系统需求", "总体设计", "能力", "功能分解", "任务", "基线"],
        "extraction_rules": [
            "功能或能力必须有明确名称和业务结果，避免抽取“处理”“支持”等孤立动词。",
            "ALLOCATED_TO 方向固定为能力/需求到配置项，CONTAINS 方向固定为基线到能力或配置项。",
            "只记录文档明确给出的分配与细化关系，不根据名称相似度推断归属。",
        ],
        "entity_types": [
            {"name": "DESIGN_BASELINE", "label": "系统设计基线", "color": "#4776bd"},
            {"name": "CAPABILITY", "label": "系统能力", "color": "#339cff"},
            {"name": "CONFIGURATION_ITEM", "label": "配置项", "color": "#f3883b"},
            {"name": "REQUIREMENT", "label": "需求", "color": "#eb76b1"},
            {"name": "SYSTEM", "label": "系统（兼容）", "color": "#4776bd"},
            {"name": "MODULE", "label": "功能模块（兼容）", "color": "#8b6de9"},
        ],
        "relation_types": [
            {"name": "CONTAINS", "label": "包含", "source_types": ["DESIGN_BASELINE", "SYSTEM", "CONFIGURATION_ITEM"], "target_types": ["CAPABILITY", "CONFIGURATION_ITEM", "REQUIREMENT", "MODULE"]},
            {"name": "ALLOCATED_TO", "label": "分配到", "source_types": ["CAPABILITY", "REQUIREMENT"], "target_types": ["CONFIGURATION_ITEM", "MODULE"]},
            {"name": "REFINES", "label": "细化", "source_types": ["REQUIREMENT", "CAPABILITY"], "target_types": ["REQUIREMENT", "CAPABILITY"]},
            {"name": "SATISFIES", "label": "满足", "source_types": ["CONFIGURATION_ITEM", "MODULE"], "target_types": ["CAPABILITY", "REQUIREMENT"]},
        ],
    },
    "configuration": {
        "key": "configuration",
        "label": "配置项图谱",
        "short_label": "配置项",
        "purpose": "呈现配置项内部的需求、服务、模块、接口、DDS Topic、数据类型与测试追踪。",
        "positioning": "回答“如何实现、数据怎样流动、由什么验证”，是工程实施和故障定位主视图。",
        "recommended_layout": "cose",
        "color": "#f3883b",
        "document_hints": ["软件需求", "详细设计", "接口", "DDS", "Topic", "测试", "配置项"],
        "extraction_rules": [
            "配置项、需求、服务、模块、接口和 Topic 必须保留完整编号或稳定名称。",
            "PUBLISHES/SUBSCRIBES 的方向为模块到 Topic；REQUIRES_INPUT/PRODUCES_OUTPUT 的方向为需求到 Topic。",
            "VERIFIED_BY 方向为需求到测试用例，EXECUTES 方向为测试执行记录到测试用例。",
            "代码类和函数由代码图谱管理；本文档图谱只保留文档明确声明的实现映射。",
        ],
        "entity_types": [
            {"name": "CONFIGURATION_ITEM", "label": "配置项", "color": "#f3883b"},
            {"name": "REQUIREMENT", "label": "需求", "color": "#eb76b1"},
            {"name": "SERVICE", "label": "服务", "color": "#339cff"},
            {"name": "MODULE", "label": "模块", "color": "#8b6de9"},
            {"name": "SERVICE_INTERFACE", "label": "服务接口", "color": "#3ab9b1"},
            {"name": "DDS_TOPIC", "label": "DDS Topic", "color": "#4776bd"},
            {"name": "DDS_DATA_TYPE", "label": "DDS 数据类型", "color": "#d87932"},
            {"name": "TEST_CASE", "label": "测试用例", "color": "#5dc977"},
            {"name": "TEST_EXECUTION", "label": "测试执行", "color": "#53b96d"},
            {"name": "SYSTEM", "label": "配置系统（兼容）", "color": "#f3883b"},
            {"name": "INTERFACE", "label": "接口（兼容）", "color": "#3ab9b1"},
            {"name": "ENVIRONMENT", "label": "运行环境", "color": "#7e8da6"},
            {"name": "VERSION", "label": "版本基线", "color": "#637187"},
        ],
        "relation_types": [
            {"name": "CONTAINS", "label": "包含", "source_types": ["CONFIGURATION_ITEM", "SYSTEM", "SERVICE"], "target_types": ["REQUIREMENT", "SERVICE", "MODULE", "SERVICE_INTERFACE", "INTERFACE"]},
            {"name": "REFINES", "label": "需求细化", "source_types": ["REQUIREMENT"], "target_types": ["REQUIREMENT"]},
            {"name": "REQUIRES_INPUT", "label": "需要输入", "source_types": ["REQUIREMENT"], "target_types": ["DDS_TOPIC", "INTERFACE"]},
            {"name": "PRODUCES_OUTPUT", "label": "产生输出", "source_types": ["REQUIREMENT"], "target_types": ["DDS_TOPIC", "INTERFACE"]},
            {"name": "ASSOCIATED_WITH", "label": "关联配置项", "source_types": ["DDS_TOPIC", "INTERFACE"], "target_types": ["CONFIGURATION_ITEM", "SYSTEM"]},
            {"name": "BOUND_TO", "label": "绑定实现模块", "source_types": ["SERVICE"], "target_types": ["MODULE"]},
            {"name": "PROVIDES", "label": "提供接口", "source_types": ["SERVICE", "MODULE"], "target_types": ["SERVICE_INTERFACE", "INTERFACE"]},
            {"name": "PUBLISHES", "label": "发布", "source_types": ["MODULE"], "target_types": ["DDS_TOPIC", "INTERFACE"]},
            {"name": "SUBSCRIBES", "label": "订阅", "source_types": ["MODULE"], "target_types": ["DDS_TOPIC", "INTERFACE"]},
            {"name": "PAYLOAD_TYPE", "label": "载荷类型", "source_types": ["DDS_TOPIC", "INTERFACE"], "target_types": ["DDS_DATA_TYPE"]},
            {"name": "IMPLEMENTS", "label": "实现", "source_types": ["MODULE", "SERVICE"], "target_types": ["REQUIREMENT", "SERVICE_INTERFACE", "INTERFACE"]},
            {"name": "VERIFIED_BY", "label": "由测试验证", "source_types": ["REQUIREMENT", "SERVICE", "MODULE"], "target_types": ["TEST_CASE"]},
            {"name": "EXECUTES", "label": "执行测试", "source_types": ["TEST_EXECUTION"], "target_types": ["TEST_CASE"]},
            {"name": "DEPENDS_ON", "label": "依赖", "source_types": [], "target_types": []},
            {"name": "USES", "label": "使用", "source_types": [], "target_types": []},
            {"name": "RELATED_TO", "label": "相关", "source_types": [], "target_types": []},
        ],
    },
    "standard": {
        "key": "standard",
        "label": "行业标准图谱",
        "short_label": "行业标准",
        "purpose": "建立行业标准、章节/条款及其适用配置项和需求之间的符合性索引。",
        "positioning": "回答“受什么标准约束、条款适用于哪里、证据来自何处”，支撑合规审查。",
        "recommended_layout": "breadthfirst",
        "color": "#5dc977",
        "document_hints": ["标准", "规范", "条款", "符合性", "适航", "DO-", "GJB"],
        "extraction_rules": [
            "标准实体使用正式标准号和名称；条款实体保留章节号，不能仅抽取孤立正文。",
            "CONSTRAINS 方向为标准到条款，APPLIES_TO 方向为条款到配置项或需求。",
            "只有原文明示适用、符合、引用或验证时才建立跨图谱关系，不作常识推断。",
        ],
        "entity_types": [
            {"name": "INDUSTRY_STANDARD", "label": "行业标准", "color": "#5dc977"},
            {"name": "STANDARD_CLAUSE", "label": "标准条款", "color": "#3ab9b1"},
            {"name": "CONFIGURATION_ITEM", "label": "适用配置项", "color": "#f3883b"},
            {"name": "REQUIREMENT", "label": "受约束需求", "color": "#eb76b1"},
            {"name": "DOCUMENT", "label": "规范文档（兼容）", "color": "#4776bd"},
            {"name": "TERM", "label": "标准术语", "color": "#8b6de9"},
            {"name": "VERSION", "label": "标准版本", "color": "#637187"},
        ],
        "relation_types": [
            {"name": "CONSTRAINS", "label": "约束条款", "source_types": ["INDUSTRY_STANDARD", "DOCUMENT"], "target_types": ["STANDARD_CLAUSE", "REQUIREMENT"]},
            {"name": "APPLIES_TO", "label": "适用于", "source_types": ["STANDARD_CLAUSE", "INDUSTRY_STANDARD", "DOCUMENT"], "target_types": ["CONFIGURATION_ITEM", "REQUIREMENT", "SYSTEM"]},
            {"name": "REFERENCES", "label": "引用", "source_types": ["STANDARD_CLAUSE", "DOCUMENT", "REQUIREMENT"], "target_types": ["INDUSTRY_STANDARD", "STANDARD_CLAUSE", "DOCUMENT"]},
            {"name": "SATISFIES", "label": "满足", "source_types": ["CONFIGURATION_ITEM", "REQUIREMENT", "SYSTEM"], "target_types": ["STANDARD_CLAUSE", "INDUSTRY_STANDARD"]},
            {"name": "VERIFIED_BY", "label": "由证据验证", "source_types": ["REQUIREMENT", "STANDARD_CLAUSE"], "target_types": ["TEST_CASE", "DOCUMENT"]},
        ],
    },
}


def profile_catalog() -> list[dict]:
    return [deepcopy(GENERAL_GRAPH_PROFILE)] + [
        deepcopy(GRAPH_PROFILES[key]) for key in BUSINESS_GRAPH_PROFILE_ORDER
    ]


def get_graph_profile(kind: str) -> dict:
    if kind == "general":
        return deepcopy(GENERAL_GRAPH_PROFILE)
    if kind not in GRAPH_PROFILES:
        raise ValueError("不支持的图谱类型")
    return deepcopy(GRAPH_PROFILES[kind])


def composite_profile_schema(kinds: list[str] | None = None) -> dict:
    selected = list(dict.fromkeys(kinds or BUSINESS_GRAPH_PROFILE_ORDER))
    profiles = {kind: get_graph_profile(kind) for kind in selected}
    entity_types = sorted({item["name"] for profile in profiles.values() for item in profile["entity_types"]})
    relation_types = sorted({item["name"] for profile in profiles.values() for item in profile["relation_types"]})
    prompt_parts = [
        "按以下业务图谱边界抽取。四类图谱共享同一实体库；相同对象只创建一个规范实体，跨图谱关系保留原文证据。"
    ]
    for profile in profiles.values():
        prompt_parts.append(
            f"【{profile['label']}】{profile['positioning']}\n- "
            + "\n- ".join(profile["extraction_rules"])
        )
    return {
        "entity_types": entity_types,
        "relation_types": relation_types,
        "graph_profiles": profiles,
        "extraction_prompt": "\n\n".join(prompt_parts),
    }


def profile_schema(kind: str) -> dict:
    """Return the independent extraction contract for one business graph."""
    if kind == "general":
        raise ValueError("通用文档图谱使用知识库的通用 Schema，不使用固定模板")
    profile = get_graph_profile(kind)
    return {
        "entity_types": [item["name"] for item in profile["entity_types"]],
        "relation_types": [item["name"] for item in profile["relation_types"]],
        "graph_profiles": {kind: profile},
        "extraction_prompt": (
            f"仅按【{profile['label']}】的业务边界抽取。{profile['positioning']}\n- "
            + "\n- ".join(profile["extraction_rules"])
        ),
    }


def profile_type_names(kind: str) -> tuple[set[str], set[str]]:
    profile = get_graph_profile(kind)
    return (
        {item["name"] for item in profile["entity_types"]},
        {item["name"] for item in profile["relation_types"]},
    )


def project_subgraph(payload: dict, kind: str) -> dict:
    """Project a canonical subgraph into one business perspective."""
    entity_types, relation_types = profile_type_names(kind)
    nodes = [
        node for node in payload.get("nodes", [])
        if str(node.get("entity_type") or "").upper() in entity_types
    ]
    node_ids = {node["id"] for node in nodes}
    edges = [
        edge for edge in payload.get("edges", [])
        if str(edge.get("predicate") or "").upper() in relation_types
        and edge.get("source") in node_ids
        and edge.get("target") in node_ids
    ]
    return {
        **payload,
        "graph_kind": kind,
        "seed_entity_ids": [
            entity_id for entity_id in payload.get("seed_entity_ids", [])
            if entity_id in node_ids
        ],
        "nodes": nodes,
        "edges": edges,
    }


def profiles_for_schema(
    entity_types: list[str],
    relation_types: list[str],
    entity_details: list[dict] | None = None,
    relation_details: list[dict] | None = None,
) -> dict:
    """Assign induced types to business profiles, retaining prototype rules."""
    allowed_entities = set(entity_types)
    allowed_relations = set(relation_types)
    explicit_entities = {
        item.get("name"): item.get("graph_kind")
        for item in entity_details or [] if item.get("graph_kind") in GRAPH_PROFILES
    }
    explicit_relations = {
        item.get("name"): item.get("graph_kind")
        for item in relation_details or [] if item.get("graph_kind") in GRAPH_PROFILES
    }
    profiles: dict[str, dict] = {}
    assigned_entities: set[str] = set()
    assigned_relations: set[str] = set()
    for kind in BUSINESS_GRAPH_PROFILE_ORDER:
        profile = get_graph_profile(kind)
        profile["entity_types"] = [
            item for item in profile["entity_types"]
            if item["name"] in allowed_entities
            and explicit_entities.get(item["name"], kind) == kind
        ]
        profile["relation_types"] = [
            item for item in profile["relation_types"]
            if item["name"] in allowed_relations
            and explicit_relations.get(item["name"], kind) == kind
        ]
        assigned_entities.update(item["name"] for item in profile["entity_types"])
        assigned_relations.update(item["name"] for item in profile["relation_types"])
        profiles[kind] = profile

    # Domain-specific induced types not present in the built-in vocabulary still
    # need a deterministic home.  Respect the LLM classification when supplied;
    # otherwise place them in the engineering/configuration perspective.
    detail_by_entity = {item.get("name"): item for item in entity_details or []}
    detail_by_relation = {item.get("name"): item for item in relation_details or []}
    for name in sorted(allowed_entities - assigned_entities):
        detail = detail_by_entity.get(name, {})
        kind = detail.get("graph_kind") if detail.get("graph_kind") in profiles else "configuration"
        profiles[kind]["entity_types"].append({
            "name": name,
            "label": detail.get("description") or name,
            "color": profiles[kind]["color"],
        })
    for name in sorted(allowed_relations - assigned_relations):
        detail = detail_by_relation.get(name, {})
        kind = detail.get("graph_kind") if detail.get("graph_kind") in profiles else "configuration"
        profiles[kind]["relation_types"].append({
            "name": name,
            "label": detail.get("description") or name,
            "source_types": detail.get("source_types") or [],
            "target_types": detail.get("target_types") or [],
        })
    return profiles
