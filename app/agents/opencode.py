"""Compatibility import for the renamed application agent adapter."""

from app.agents.codecode import CodeCodeAgentBackend, SYSTEM_PROMPT

OpenCodeAgentBackend = CodeCodeAgentBackend
