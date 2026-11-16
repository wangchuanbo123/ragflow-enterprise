"""Per-request retrieval results; never store diagnostics on shared retrievers."""
from __future__ import annotations


class RetrievalDocuments(list):
    def __init__(self, documents=(), *, diagnostics=()):
        super().__init__(documents)
        self.diagnostics = list(diagnostics)


def diagnostic(channel, status, code, count=0, elapsed_ms=0, detail=None):
    value = {"channel": channel, "status": status, "code": code,
             "count": count, "elapsed_ms": round(elapsed_ms, 2)}
    if detail:
        value["detail"] = detail
    return value
