"""Explicit adapter registry: changing the agent does not change business services."""

from app.agents.contracts import AgentBackend
from app.agents.settings import AgentSettings, configured_profiles, profile_settings

_factories = {}


def register_agent_backend(name: str, factory) -> None:
    if not name or not callable(factory):
        raise ValueError("Agent adapters require a name and callable factory")
    _factories[name] = factory


def list_backend_profiles() -> list[str]:
    return ["default", *sorted(configured_profiles())]


def get_agent_backend(profile_name: str | None = None) -> AgentBackend:
    from app.services.model_budget import budgeted_backend
    settings = profile_settings(profile_name)
    if settings.backend == "opencode":
        import os
        from app.core.config import APP_PROFILE
        from app.agents.settings import default_settings, managed_endpoint, same_service_endpoint
        from app.agents.contracts import AgentUnavailableError
        if (APP_PROFILE != "test" and managed_endpoint(settings.base_url) and same_service_endpoint(settings.base_url, default_settings().base_url)
            and os.getenv("OPENCODE_AUTO_START", "true").lower() in {"1", "true", "yes"}):
            from app.agents.runtime import managed_runtime_running
            if not managed_runtime_running():
                raise AgentUnavailableError("专用OpenCode运行时未启动；不会连接个人OpenCode会话")
        from app.agents.opencode import OpenCodeAgentBackend

        return budgeted_backend(OpenCodeAgentBackend(settings))
    if settings.backend in _factories:
        return budgeted_backend(_factories[settings.backend](settings))
    raise ValueError("Unsupported agent backend; install an explicit application adapter")
