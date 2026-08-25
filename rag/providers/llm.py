from functools import cached_property
import json
from typing import Iterator

import httpx
from langchain_community.llms import Ollama
from langchain_openai import ChatOpenAI

from rag.providers.interfaces import ProviderHealth, ProviderIdentity


class ZhipuLLMProvider:
    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str,
        temperature: float = 0,
        timeout: float = 120,
    ):
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.temperature = temperature
        self.timeout = timeout

    @cached_property
    def _model(self):
        if not self.api_key:
            raise RuntimeError(
                "LLM_API_KEY is not configured. Add it to the project's .env file."
            )

        return ChatOpenAI(
            model=self.model,
            api_key=self.api_key,
            base_url=self.base_url,
            temperature=self.temperature,
            max_retries=2,
            timeout=self.timeout,
        )

    def get_model(self):
        return self._model

    def identity(self) -> ProviderIdentity:
        return ProviderIdentity(
            kind="llm",
            provider="zhipu_openai_compatible",
            model=self.model,
            capabilities=("generate", "stream", "structured_output"),
        )

    def health(self) -> ProviderHealth:
        if not self.api_key or not self.base_url:
            return ProviderHealth.fail("API key and base URL are required")
        return ProviderHealth.ok(
            provider=self.identity().provider,
            model=self.model,
        )

    def generate(self, prompt: str) -> str:
        response = self._model.invoke(prompt)
        content = getattr(response, "content", response)
        return str(content)

    def stream(self, prompt: str) -> Iterator[str]:
        for chunk in self._model.stream(prompt):
            content = getattr(chunk, "content", chunk)
            if content:
                yield str(content)


class OllamaLLMProvider:
    def __init__(self, model: str, base_url: str, timeout: float = 120):
        self.model = model
        self.base_url = base_url
        self.timeout = timeout

    @cached_property
    def _model(self):
        return Ollama(
            model=self.model,
            base_url=self.base_url,
            timeout=self.timeout,
        )

    def get_model(self):
        return self._model

    def identity(self) -> ProviderIdentity:
        return ProviderIdentity(
            kind="llm",
            provider="ollama",
            model=self.model,
            capabilities=("generate", "stream"),
        )

    def health(self) -> ProviderHealth:
        return ProviderHealth.ok(
            provider=self.identity().provider,
            model=self.model,
            endpoint=self.base_url,
        )

    def generate(self, prompt: str) -> str:
        return str(self._model.invoke(prompt))

    def stream(self, prompt: str) -> Iterator[str]:
        for chunk in self._model.stream(prompt):
            content = getattr(chunk, "content", chunk)
            if content:
                yield str(content)


class OpenAICompatibleLLMProvider(ZhipuLLMProvider):
    """Generic OpenAI-compatible chat endpoint (vLLM, gateways, or vendors)."""

    def identity(self) -> ProviderIdentity:
        return ProviderIdentity(
            kind="llm",
            provider="openai_compatible",
            model=self.model,
            capabilities=("generate", "stream", "structured_output"),
        )


class AnthropicCompatibleLLMProvider:
    """Minimal Anthropic Messages API adapter for compatible gateways."""

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str,
        temperature: float = 0,
        timeout: float = 120,
        max_tokens: int = 4096,
        anthropic_version: str = "2023-06-01",
        client=None,
    ):
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.temperature = temperature
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.anthropic_version = anthropic_version
        self._client = client

    @property
    def endpoint(self) -> str:
        if self.base_url.endswith("/v1/messages"):
            return self.base_url
        if self.base_url.endswith("/v1"):
            return f"{self.base_url}/messages"
        return f"{self.base_url}/v1/messages"

    @property
    def client(self):
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout)
        return self._client

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "anthropic-version": self.anthropic_version,
        }

    def get_model(self):
        return self

    def identity(self) -> ProviderIdentity:
        return ProviderIdentity(
            kind="llm",
            provider="anthropic_compatible",
            model=self.model,
            capabilities=("generate", "stream", "structured_output"),
        )

    def health(self) -> ProviderHealth:
        if not self.api_key or not self.base_url:
            return ProviderHealth.fail("API key and base URL are required")
        return ProviderHealth.ok(
            provider=self.identity().provider,
            model=self.model,
        )

    def _payload(self, prompt: str, *, stream: bool = False) -> dict:
        return {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": [{"role": "user", "content": prompt}],
            "stream": stream,
        }

    @staticmethod
    def _text_from_response(payload: dict) -> str:
        parts = []
        for item in payload.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
        return "".join(parts)

    def generate(self, prompt: str) -> str:
        response = self.client.post(
            self.endpoint,
            headers=self.headers,
            json=self._payload(prompt),
        )
        response.raise_for_status()
        return self._text_from_response(response.json())

    def stream(self, prompt: str) -> Iterator[str]:
        with self.client.stream(
            "POST",
            self.endpoint,
            headers=self.headers,
            json=self._payload(prompt, stream=True),
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                delta = event.get("delta") or {}
                if event.get("type") == "content_block_delta":
                    text = delta.get("text")
                    if text:
                        yield str(text)
