"""Purpose-specific model profiles independent from concrete SDKs."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.core import config


@dataclass(frozen=True)
class ModelProfile:
    purpose: str
    provider: str
    model: str
    api_key: str = ""
    base_url: str = ""
    required_capabilities: tuple[str, ...] = ()

    @property
    def profile_id(self) -> str:
        raw = "|".join(
            (self.purpose, self.provider, self.model, self.base_url)
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def as_dict(self) -> dict:
        return {
            "purpose": self.purpose,
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "required_capabilities": list(self.required_capabilities),
            "profile_id": self.profile_id,
        }


def get_llm_profile(purpose: str) -> ModelProfile:
    purpose = purpose.strip().lower()
    values = {
        "answer": (
            config.ANSWER_LLM_PROVIDER,
            config.ANSWER_LLM_MODEL,
            config.ANSWER_LLM_API_KEY,
            config.ANSWER_LLM_BASE_URL,
            ("generate", "stream"),
        ),
        "rewrite": (
            config.REWRITE_LLM_PROVIDER,
            config.REWRITE_LLM_MODEL,
            config.REWRITE_LLM_API_KEY,
            config.REWRITE_LLM_BASE_URL,
            ("generate",),
        ),
        "router": (
            config.ROUTER_LLM_PROVIDER,
            config.ROUTER_LLM_MODEL,
            config.ROUTER_LLM_API_KEY,
            config.ROUTER_LLM_BASE_URL,
            ("generate",),
        ),
    }
    if purpose == "extraction":
        if config.GRAPH_EXTRACTION_PROVIDER == "current":
            answer = get_llm_profile("answer")
            return ModelProfile(
                purpose="extraction",
                provider=answer.provider,
                model=answer.model,
                api_key=answer.api_key,
                base_url=answer.base_url,
                required_capabilities=("generate",),
            )
        values[purpose] = (
            config.GRAPH_EXTRACTION_PROVIDER,
            config.GRAPH_EXTRACTION_MODEL,
            config.GRAPH_EXTRACTION_API_KEY,
            config.GRAPH_EXTRACTION_BASE_URL,
            ("generate",),
        )
    if purpose == "graph_escalation":
        # High-quality fallback model for empty or low-confidence extractions.
        extraction = get_llm_profile("extraction")
        provider = (
            config.GRAPH_ESCALATION_PROVIDER
            if config.GRAPH_ESCALATION_PROVIDER != "current"
            else extraction.provider
        )
        return ModelProfile(
            purpose="graph_escalation",
            provider=provider,
            model=config.GRAPH_ESCALATION_MODEL or extraction.model,
            api_key=config.GRAPH_ESCALATION_API_KEY or extraction.api_key,
            base_url=config.GRAPH_ESCALATION_BASE_URL or extraction.base_url,
            required_capabilities=("generate",),
        )
    if purpose not in values:
        raise ValueError(f"Unsupported model purpose: {purpose}")
    provider, model, api_key, base_url, capabilities = values[purpose]
    return ModelProfile(
        purpose=purpose,
        provider=provider,
        model=model,
        api_key=api_key,
        base_url=base_url,
        required_capabilities=capabilities,
    )
