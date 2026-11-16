import pytest
from rag.knowledge_graph.profiles import composite_profile_schema, profile_catalog, profile_schema, project_subgraph

def test_only_total_graph_is_catalogued_without_domain_templates():
    assert [item["key"] for item in profile_catalog()] == ["general"]
    assert profile_catalog()[0]["label"] == "知识库总图谱"
    assert composite_profile_schema()["graph_profiles"] is None

@pytest.mark.parametrize("kind", ["concept", "functional", "configuration", "standard"])
def test_retired_templates_and_projections_reject_explicitly(kind):
    with pytest.raises(ValueError): profile_schema(kind)
    with pytest.raises(ValueError): project_subgraph({}, kind)

def test_total_graph_does_not_filter_domain_specific_facts():
    payload = {"nodes": [{"id": "1", "entity_type": "CUSTOM_DOMAIN"}], "edges": []}
    assert project_subgraph(payload, "general")["nodes"] == payload["nodes"]
