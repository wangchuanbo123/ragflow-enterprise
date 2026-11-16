"""Deterministic evidence availability and citation identifier checks.

An existing citation identifier does not prove that the source supports a claim.
This module explicitly reports that limit instead of treating IDs as fact checks.
"""
from __future__ import annotations

import re

NO_EVIDENCE_ANSWER = "知识库中没有检索到足够的相关证据，无法据此回答。请补充资料，或调整问题后重试。"
_CITATION = re.compile(r"[\[【]\s*(来源\s*\d+(?:\s*[,，、;；]\s*(?:来源\s*)?\d+)*)\s*[\]】]")


def _identifiers(match):
    return ["来源" + number for number in re.findall(r"\d+", match.group(1))]


def evidence_unavailable(sources, route=None) -> bool:
    return sources == [] or (route or {}).get("evidence_status") in {"insufficient", "unavailable"}


def validate_answer(answer: str, sources: list[dict] | None, route=None) -> tuple[str, dict]:
    """Return ``(answer, validation)``; remove references to nonexistent IDs.

    ``sources=None`` preserves compatibility for integrations lacking evidence
    metadata and produces an unverified result. Explicitly empty sources refuse.
    """
    if evidence_unavailable(sources, route):
        return NO_EVIDENCE_ANSWER, {"status": "not_applicable", "invalid_ids": [],
            "valid_ids": [], "support_status": "no_evidence"}
    if sources is None:
        return answer, {"status": "unverified", "invalid_ids": [], "valid_ids": [], "support_status": "not_checked"}
    known = {str(item.get("citation_id", "")).replace(" ", "") for item in sources or []}
    cited = list(dict.fromkeys(identifier for match in _CITATION.finditer(answer or "") for identifier in _identifiers(match)))
    valid = [identifier for identifier in cited if identifier in known]
    previous_invalid = ((route or {}).get("citation_validation") or {}).get("invalid_ids") or []
    invalid = list(dict.fromkeys([*previous_invalid, *(identifier for identifier in cited if identifier not in known)]))
    sanitized = _CITATION.sub(lambda match: "".join(f"[{identifier}]" for identifier in _identifiers(match) if identifier in known), answer or "") if sources is not None else answer
    return sanitized, {"status": "invalid" if invalid else "valid" if valid else "unverified",
        "invalid_ids": invalid, "valid_ids": valid, "support_status": "not_checked"}


def filter_citation_stream(fragments, sources, route=None):
    """Buffer only bracket spans; validate split IDs before displaying them."""
    pending = ""
    for fragment in fragments:
        pending += fragment
        while pending:
            starts = [pos for token in ("[", "【") if (pos := pending.find(token)) >= 0]
            if not starts:
                yield pending
                pending = ""
                break
            start = min(starts)
            if start:
                yield pending[:start]
                pending = pending[start:]
            end = pending.find("]" if pending.startswith("[") else "】")
            if end < 0 and len(pending) <= 160:
                break
            if end < 0:
                yield pending[:1]
                pending = pending[1:]
                continue
            checked, validation = validate_answer(pending[:end + 1], sources, route)
            if route is not None and validation["invalid_ids"]:
                route["citation_validation"] = validation
            if checked:
                yield checked
            pending = pending[end + 1:]
    if pending:
        yield pending
