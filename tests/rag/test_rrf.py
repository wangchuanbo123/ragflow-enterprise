"""RRF 融合测试。"""

from langchain_core.documents import Document

from rag.retrievers.hybrid_retriever import weighted_rrf_fusion


def _make_doc(chunk_id: str, channel: str = "vector") -> Document:
    return Document(
        page_content=f"content-{chunk_id}",
        metadata={"chunk_id": chunk_id, "_channel": channel},
    )


def test_rrf_fuses_by_chunk_id():
    vector_docs = [_make_doc("c1"), _make_doc("c2"), _make_doc("c3")]
    bm25_docs = [_make_doc("c2", "bm25"), _make_doc("c4", "bm25"), _make_doc("c1", "bm25")]

    result = weighted_rrf_fusion(
        [(vector_docs, 0.7), (bm25_docs, 0.3)],
        rrf_k=60,
        candidate_k=10,
    )

    chunk_ids = [d.metadata["chunk_id"] for d in result]
    assert len(chunk_ids) == 4  # c1, c2, c3, c4
    assert set(chunk_ids) == {"c1", "c2", "c3", "c4"}


def test_rrf_chunk_hit_by_both_channels_ranks_higher():
    vector_docs = [_make_doc("c1"), _make_doc("c2")]
    bm25_docs = [_make_doc("c1", "bm25"), _make_doc("c3", "bm25")]

    result = weighted_rrf_fusion(
        [(vector_docs, 0.7), (bm25_docs, 0.3)],
        rrf_k=60,
        candidate_k=10,
    )

    # c1 is hit by both channels, should rank first
    assert result[0].metadata["chunk_id"] == "c1"
    assert result[0].metadata["fusion_score"] > 0


def test_rrf_respects_candidate_k():
    docs = [_make_doc(f"c{i}") for i in range(20)]
    result = weighted_rrf_fusion(
        [(docs, 1.0)],
        rrf_k=60,
        candidate_k=5,
    )
    assert len(result) == 5


def test_rrf_empty_input():
    result = weighted_rrf_fusion([], rrf_k=60, candidate_k=10)
    assert result == []


def test_rrf_merges_graph_metadata_from_later_channel():
    vector_doc = _make_doc("c1", "vector")
    graph_doc = _make_doc("c1", "graph")
    graph_doc.metadata.update({
        "graph_facts": ["模块A --DEPENDS_ON--> 模块B"],
        "graph_score": 0.9,
    })

    result = weighted_rrf_fusion(
        [([vector_doc], 0.5), ([graph_doc], 0.25)],
        rrf_k=60,
        candidate_k=10,
    )

    assert result[0].metadata["graph_facts"] == [
        "模块A --DEPENDS_ON--> 模块B"
    ]
    assert result[0].metadata["graph_score"] == 0.9
    assert result[0].metadata["retrieval_channels"] == ["vector", "graph"]
