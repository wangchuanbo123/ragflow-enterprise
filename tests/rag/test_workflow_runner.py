from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import subprocess
import sys

from langchain_core.documents import Document

from rag.graph.rag_graph import build_graph
from rag.runtime.runtime import RAGRuntime
from tests.conftest import FakeReranker, FakeStreamLLM


class Retriever:
    def get_relevant_documents(self, query):
        return [Document(page_content=f"Content for {query}", metadata={"chunk_id": query, "source": query + ".txt"})]


def test_workflow_reuse_keeps_independent_request_state_and_context_mode():
    llm = FakeStreamLLM()
    workflow = build_graph(RAGRuntime(llm=llm, retriever=Retriever(), reranker=FakeReranker()))
    inputs = [{"query": query, "original_query": query, "history": [], "generation_mode": "context", "retrieval_options": {"channels": ["text"]}} for query in ["first", "second"]]
    original = deepcopy(inputs)
    with ThreadPoolExecutor(2) as executor:
        results = list(executor.map(workflow.invoke, inputs))
    assert inputs == original
    for query, result in zip(["first", "second"], results):
        assert result["original_query"] == query
        assert result["sources"][0]["source"] == query + ".txt"
        assert result["parallel_markers"] == ["text", "graph", "code"]
        assert "answer" not in result
    assert llm.calls == []


def test_workflow_imports_and_runs_without_langgraph_installed():
    script = '''
import sys
sys.modules['langgraph'] = None
from rag.graph.rag_graph import build_graph
from tests.conftest import make_fake_runtime
result = build_graph(make_fake_runtime()).invoke({'query':'hello','original_query':'hello','history':[],'retrieval_options':{'channels':['text']},'generation_mode':'context'})
assert result['sources']
assert 'langgraph.graph' not in sys.modules
'''
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
