"""Context JSON export tests."""

import json

from rag.context.context_exporter import export_context_snapshot


def _state() -> dict:
    return {
        "original_query": "模块 A 依赖什么？",
        "retrieval_query": "模块 A 依赖关系",
        "retrieval_queries": ["模块 A 依赖什么？", "模块 A 依赖关系"],
        "context": "[来源1]\n图谱事实：模块 A --DEPENDS_ON--> 模块 B\n内容：模块 A 依赖模块 B。",
        "sources": [
            {
                "citation_id": "来源1",
                "document_id": "d1",
                "chunk_id": "c1",
                "source": "design.docx",
                "retrieval_channels": ["vector", "graph"],
                "graph_facts": ["模块 A --DEPENDS_ON--> 模块 B"],
            }
        ],
    }


def test_context_export_is_disabled_by_default_argument(tmp_path):
    result = export_context_snapshot(_state(), enabled=False, output_dir=tmp_path)

    assert result is None
    assert list(tmp_path.iterdir()) == []


def test_context_export_writes_structured_utf8_json(tmp_path):
    result = export_context_snapshot(_state(), enabled=True, output_dir=tmp_path)

    assert result is not None
    payload = json.loads(result.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["llm_input"]["query"] == "模块 A 依赖什么？"
    assert "模块 A --DEPENDS_ON--> 模块 B" in payload["llm_input"]["context"]
    assert payload["sources"][0]["retrieval_channels"] == ["vector", "graph"]
    assert payload["graph_facts"][0]["chunk_id"] == "c1"
