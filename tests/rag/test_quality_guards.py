"""Evidence refusal, pipeline isolation and observable partial retrieval failures."""
from types import SimpleNamespace

import pytest
from langchain_core.documents import Document

from rag.answer_validation import NO_EVIDENCE_ANSWER, validate_answer
from rag.indexing.profiles import embedding_fingerprint, pipeline_fingerprint
from rag.nodes.generate_node import create_generate_node, create_stream_generate_node
from rag.retrievers.hybrid_retriever import HybridRRFRetriever
from rag.retrievers.diagnostics import RetrievalDocuments


def test_empty_evidence_never_calls_model():
    def fail(*_):
        pytest.fail("an empty retrieval must not call the model")
    model = SimpleNamespace(generate=fail, stream=fail)
    state = {"query": "无证据问题", "sources": []}
    result = create_generate_node(model)(state)
    assert result["answer"] == NO_EVIDENCE_ANSWER
    assert result["route"]["citation_validation"]["support_status"] == "no_evidence"
    assert "".join(create_stream_generate_node(model)(state)) == NO_EVIDENCE_ANSWER


def test_citation_ids_are_checked_without_claiming_fact_verification():
    answer, validation = validate_answer("答案[来源1]。伪引用[来源99]。", [{"citation_id": "来源1"}])
    assert "[来源1]" in answer and "来源99" not in answer
    assert validation == {"status": "invalid", "valid_ids": ["来源1"],
                          "invalid_ids": ["来源99"], "support_status": "not_checked"}


def test_combined_and_streamed_invalid_citations_remain_auditable():
    from rag.answer_validation import filter_citation_stream
    route = {}
    sources = [{"citation_id": "来源1"}]
    rendered = "".join(filter_citation_stream(["答案[来源1、来", "源99]。"], sources, route))
    assert rendered == "答案[来源1]。"
    _, validation = validate_answer(rendered, sources, route)
    assert validation["invalid_ids"] == ["来源99"]
    assert validation["status"] == "invalid"
    assert validate_answer("历史[来源1]", None)[1]["status"] == "unverified"


def test_stream_handles_citations_split_across_fragments():
    model = SimpleNamespace(stream=lambda *_: iter(["答[来", "源9", "]。证[", "来源1]。"]))
    state = {"query": "问题", "sources": [{"citation_id": "来源1"}]}
    assert "".join(create_stream_generate_node(model)(state)) == "答。证[来源1]。"


def test_embedding_profile_changes_for_instructions_endpoint_and_revision(monkeypatch):
    model = SimpleNamespace(model="same-alias", base_url="https://example.test/v1",
                            embed_instruction="passage: ", query_instruction="query: ")
    original = embedding_fingerprint(model)
    model.embed_instruction = "document: "
    assert embedding_fingerprint(model) != original
    model.embed_instruction = "passage: "
    model.base_url = "https://other.test/v1"
    assert embedding_fingerprint(model) != original
    model.base_url = "https://example.test/v1"
    monkeypatch.setenv("EMBEDDING_REVISION", "artifact-2")
    assert embedding_fingerprint(model) != original


def test_chunk_parameters_change_pipeline_fingerprint(monkeypatch):
    from app.core import config
    original = pipeline_fingerprint()
    monkeypatch.setattr(config, "CHUNK_SIZE", config.CHUNK_SIZE + 10)
    assert pipeline_fingerprint() != original


def test_vector_failure_is_visible_when_bm25_succeeds(monkeypatch):
    from rag.retrievers import hybrid_retriever as module
    def fail(*args, **kwargs):
        raise ConnectionError("private endpoint must not leak")
    vector = SimpleNamespace(similarity_search_with_relevance_scores=fail)
    lexical = SimpleNamespace(get_relevant_documents=lambda *args, **kwargs: [
        Document(page_content="证据", metadata={"chunk_id": "c", "source": "a"})])
    monkeypatch.setattr(module, "get_bm25_retriever", lambda *_: lexical)
    result = HybridRRFRetriever(vector).retrieve_single("问题")
    assert isinstance(result, RetrievalDocuments) and len(result) == 1
    assert result.diagnostics[0]["status"] == "error"
    assert "private" not in str(result.diagnostics)
    assert result.diagnostics[1]["status"] == "ok"


def test_failed_shared_embedding_does_not_retry_the_model(monkeypatch):
    from rag.retrievers import hybrid_retriever as module
    from rag.nodes.retrieve_node import create_retrieve_node
    def fail(*args, **kwargs):
        pytest.fail("failed shared embedding must never be retried per channel")
    vector = SimpleNamespace(similarity_search_with_relevance_scores=fail,
                             similarity_search_by_vector_with_relevance_scores=fail)
    lexical = SimpleNamespace(get_relevant_documents=lambda *args, **kwargs: [])
    monkeypatch.setattr(module, "get_bm25_retriever", lambda *_: lexical)
    result = create_retrieve_node(HybridRRFRetriever(vector))({
        "query": "问题", "query_embedding": None, "route": {"query_embedding": {"status": "error"}}})
    assert result["docs"] == []
    assert result["retrieval_diagnostics"][0]["code"] == "shared_embedding_unavailable"


def test_failed_bm25_build_backs_off_until_invalidation(monkeypatch):
    from rag.retrievers import bm25_retriever as module
    from rag.errors import RetrievalError
    calls = []
    def count(*_):
        calls.append(1)
        return module.BM25_MAX_CHUNKS_PER_KNOWLEDGE_BASE + 1
    monkeypatch.setattr(module, "KnowledgeRepository", lambda *_: SimpleNamespace(count_chunks=count))
    retriever = module.ChunkBM25Retriever()
    for _ in range(2):
        with pytest.raises(RetrievalError):
            retriever.ensure_built(db=object())
    assert len(calls) == 1
    retriever.invalidate()
    with pytest.raises(RetrievalError):
        retriever.ensure_built(db=object())
    assert len(calls) == 2


def test_recursive_tail_keeps_page_anchor_and_size_budget():
    from rag.indexing.chunker import split_documents
    pages = [Document(page_content="第一页独有文本。", metadata={"page": 0}),
             Document(page_content="第二页独有文本。", metadata={"page": 1})]
    result = split_documents(pages, strategy="recursive", chunk_size=50, chunk_overlap=0, min_chunk_size=40)
    assert len(result.chunks) == 2
    assert [chunk.metadata["page"] for chunk in result.chunks] == [0, 1]
    assert all(count <= 50 for count in result.token_counts)


def test_fact_evidence_metric_requires_cited_expected_quote():
    from scripts.evaluate_rag import fact_evidence_metrics
    question = {"required_facts": [{"phrases": ["五个容器"],
        "evidence": [{"source": "架构.md", "quote": "共五个容器"}]}]}
    sources = [{"source": "架构.md", "citation_id": "来源1", "preview": "部署共五个容器"}]
    assert fact_evidence_metrics("共五个容器[来源1]。", question, sources)["fact_evidence_coverage"] == 1
    assert fact_evidence_metrics("共五个容器。", question, sources)["fact_evidence_coverage"] == 0


def test_sparse_image_page_diagnostics_preserve_native_text(monkeypatch):
    from rag.loaders import document_loader as loader
    doc = Document(page_content="页眉", metadata={"page": 0})
    image = SimpleNamespace(get_object=lambda: {"/Subtype": "/Image"})
    resources = SimpleNamespace(get_object=lambda: {"/XObject": SimpleNamespace(get_object=lambda: {"i": image})})
    monkeypatch.setattr(loader, "PdfReader", lambda *_: SimpleNamespace(pages=[{"/Resources": resources}]))
    loader._pdf_density_diagnostics("fake.pdf", [doc])
    assert doc.page_content == "页眉"
    assert doc.metadata["ocr_recommended"] is True
