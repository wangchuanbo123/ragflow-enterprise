from app.core.config import INITIAL_ADMIN_PASSWORD, INITIAL_ADMIN_USERNAME


def _login_and_default_kb(client) -> str:
    client.post(
        "/api/v1/auth/login",
        json={
            "username": INITIAL_ADMIN_USERNAME,
            "password": INITIAL_ADMIN_PASSWORD,
        },
    )
    return next(
        item["id"]
        for item in client.get("/api/v1/knowledge-bases").json()
        if item["is_default"]
    )


def test_graph_automation_and_custom_schema_editing_api(client):
    knowledge_base_id = _login_and_default_kb(client)
    base = f"/api/v1/knowledge-bases/{knowledge_base_id}/knowledge-graph"

    settings = client.get(f"{base}/automation")
    assert settings.status_code == 200
    assert settings.json()["auto_build_enabled"] is True
    disabled = client.put(f"{base}/automation", json={"auto_build_enabled": False})
    assert disabled.status_code == 200
    assert disabled.json() == {
        "auto_build_enabled": False,
        "auto_schema_generation_enabled": True,
    }

    generated = client.post(
        f"{base}/schemas/generate",
        json={"document_ids": [], "instructions": "识别核心业务系统"},
    )
    assert generated.status_code == 202
    assert generated.json()["job_type"] == "schema_generation"
    drift = client.post(
        f"{base}/schemas/drift-check",
        json={"document_ids": [], "instructions": "检查新增领域类型"},
    )
    assert drift.status_code == 202
    assert drift.json()["job_type"] == "schema_drift"

    coverage = client.get(f"{base}/coverage")
    assert coverage.status_code == 200
    assert coverage.json()["summary"]["documents"] == 0
    tag_map = client.get(
        f"/api/v1/knowledge-bases/{knowledge_base_id}/documents/tag-map"
    )
    assert tag_map.status_code == 200
    assert tag_map.json() == {}

    created = client.post(
        f"{base}/schemas",
        json={
            "entity_types": ["BUSINESS_SERVICE", "BUSINESS_EVENT"],
            "relation_types": ["ROUTES_TO", "EMITS"],
            "normalization_rules": {"unicode": "NFKC"},
            "extraction_prompt": "只抽取明确的业务调用和事件。",
        },
    )
    assert created.status_code == 201
    assert created.json()["status"] == "draft"
    schema_id = created.json()["id"]

    updated = client.put(
        f"{base}/schemas/{schema_id}",
        json={
            "entity_types": ["BUSINESS_SERVICE", "BUSINESS_EVENT", "BUSINESS_ORDER"],
            "relation_types": ["ROUTES_TO", "EMITS"],
            "normalization_rules": {"unicode": "NFKC", "case": "lower_ascii"},
            "extraction_prompt": "只抽取有原文证据的业务调用和事件。",
        },
    )
    assert updated.status_code == 200
    assert "BUSINESS_ORDER" in updated.json()["entity_types_json"]

    activated = client.post(f"{base}/schemas/{schema_id}/activate")
    assert activated.status_code == 200
    assert activated.json()["status"] == "active"
    immutable = client.put(
        f"{base}/schemas/{schema_id}",
        json={
            "entity_types": ["BUSINESS_SERVICE"],
            "relation_types": ["ROUTES_TO"],
        },
    )
    assert immutable.status_code == 400
