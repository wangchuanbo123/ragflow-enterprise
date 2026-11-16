"""Application-owned agent contracts, independent of OpenCode or model SDKs."""

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


TASKS = frozenset({"extraction", "ingestion", "schema_governance", "quality_review",
                  "incremental_maintenance", "knowledge_organization", "evaluation",
                  "business_analysis", "operations"})


class AgentError(RuntimeError):
    code = "AGENT_FAILED"

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code if isinstance(status_code, int) and 100 <= status_code <= 599 else None


class AgentUnavailableError(AgentError):
    code = "AGENT_UNAVAILABLE"


class AgentCancelledError(AgentError):
    code = "AGENT_CANCELLED"


class AgentOutputError(AgentError):
    code = "AGENT_INVALID_OUTPUT"


@dataclass(frozen=True)
class AgentRequest:
    task: str
    knowledge_base_id: str
    instructions: str
    context: dict[str, Any]
    output_schema: dict[str, Any]
    run_id: str = ""
    tool_token: str = field(default="", repr=False)


@dataclass(frozen=True)
class AgentResult:
    output: dict[str, Any]
    backend: str
    session_id: str = ""
    model: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    trace: list[dict[str, Any]] = field(default_factory=list)


class AgentBackend(Protocol):
    def identity(self) -> dict[str, Any]: ...

    def health(self) -> dict[str, Any]: ...

    def run(self, request: AgentRequest, *, cancelled: Callable[[], bool] | None = None,
            on_event: Callable[[dict], None] | None = None) -> AgentResult: ...
