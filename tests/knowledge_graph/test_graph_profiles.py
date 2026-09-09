from rag.knowledge_graph.profiles import (
    BUSINESS_GRAPH_PROFILE_ORDER,
    composite_profile_schema,
    profile_catalog,
    profile_schema,
    profile_type_names,
    project_subgraph,
)


def test_composite_profile_schema_has_four_connected_business_views():
    schema = composite_profile_schema()
    assert tuple(schema["graph_profiles"]) == BUSINESS_GRAPH_PROFILE_ORDER
    assert {"CONFIGURATION_ITEM", "REQUIREMENT"} <= set(schema["entity_types"])
    assert {"ALLOCATED_TO", "VERIFIED_BY", "APPLIES_TO"} <= set(schema["relation_types"])
    assert "概念图谱" in schema["extraction_prompt"]
    assert "行业标准图谱" in schema["extraction_prompt"]


def test_each_graph_kind_has_an_independent_schema_contract():
    assert [item["key"] for item in profile_catalog()] == [
        "general", "concept", "functional", "configuration", "standard"
    ]
    concept = profile_schema("concept")
    functional = profile_schema("functional")
    assert set(concept["graph_profiles"]) == {"concept"}
    assert set(functional["graph_profiles"]) == {"functional"}
    assert concept["entity_types"] != functional["entity_types"]
    assert concept["relation_types"] != functional["relation_types"]


def test_project_subgraph_filters_nodes_and_edges_by_business_view():
    payload = {
        "document_id": "doc-1",
        "nodes": [
            {"id": "standard", "label": "DO-178C", "entity_type": "INDUSTRY_STANDARD"},
            {"id": "clause", "label": "6.3.4", "entity_type": "STANDARD_CLAUSE"},
            {"id": "module", "label": "健康管理模块", "entity_type": "MODULE"},
        ],
        "edges": [
            {"id": "e1", "source": "standard", "target": "clause", "predicate": "CONSTRAINS"},
            {"id": "e2", "source": "module", "target": "clause", "predicate": "USES"},
        ],
    }
    projected = project_subgraph(payload, "standard")
    assert projected["graph_kind"] == "standard"
    assert {item["id"] for item in projected["nodes"]} == {"standard", "clause"}
    assert [item["id"] for item in projected["edges"]] == ["e1"]
    entity_types, relation_types = profile_type_names("standard")
    assert "INDUSTRY_STANDARD" in entity_types
    assert "APPLIES_TO" in relation_types
