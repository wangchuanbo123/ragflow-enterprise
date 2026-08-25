from rag.routing import route_query


def test_relationship_question_selects_graph_when_available():
    decision = route_query("订单模块依赖哪些服务？", graph_available=True)

    assert decision["selected_channels"] == ["text", "graph"]
    assert decision["method"] == "rules"
    assert decision["confidence"] == 0.9


def test_code_question_explains_unavailable_channel_and_falls_back_to_text():
    decision = route_query("这个函数调用了哪些方法？", code_available=False)

    assert "code" in decision["requested_channels"]
    assert "code" in decision["unavailable_channels"]
    assert decision["selected_channels"] == ["text"]
    assert any("尚未就绪" in reason for reason in decision["reasons"])


def test_explicit_channels_override_intent_rules():
    decision = route_query(
        "普通问题",
        explicit_channels=["graph"],
        graph_available=True,
        scope={"document_ids": ["doc-1"]},
    )

    assert decision["method"] == "explicit"
    assert decision["selected_channels"] == ["graph"]
    assert decision["scope"]["document_ids"] == ["doc-1"]


def test_function_location_question_selects_code_channel():
    decision = route_query("订单超时在哪里处理？", code_available=True)

    assert decision["selected_channels"] == ["text", "code"]
    assert decision["method"] == "rules"
