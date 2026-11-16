from functools import cached_property
from dataclasses import dataclass, field
import json
from typing import Iterator
from types import SimpleNamespace

import httpx
from openai import OpenAI

from rag.providers.interfaces import ProviderHealth, ProviderIdentity
from app.services.model_budget import measured_model_call, record_usage


@dataclass
class _TextResponse:
    content: str
    usage_metadata: dict = field(default_factory=dict)

    def __str__(self):
        return self.content


def _usage(value):
    if hasattr(value, 'model_dump'):
        return value.model_dump(exclude_none=True)
    return value if isinstance(value, dict) else {}


class ChatOpenAI:
    """Small invoke/stream adapter over the official OpenAI SDK.

    The local class name preserves the previous injection seam for probes/tests;
    it does not import or depend on langchain-openai.
    """
    def __init__(self, *, model, api_key, base_url, temperature=0, timeout=120,
                 max_retries=2, http_client=None):
        self.model, self.base_url, self.temperature = model, base_url, temperature
        self.timeout, self.max_tokens = timeout, None
        self._sdk = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout,
                           max_retries=max_retries, http_client=http_client)
        self.client = self._sdk.chat.completions

    @property
    def max_retries(self):
        return self._sdk.max_retries

    @max_retries.setter
    def max_retries(self, value):
        self._sdk.max_retries = value

    def _payload(self, prompt):
        payload = {'model': self.model, 'messages': [{'role': 'user', 'content': str(prompt)}],
                   'temperature': self.temperature}
        if self.max_tokens is not None:
            payload['max_tokens'] = self.max_tokens
        return payload

    def invoke(self, prompt):
        response = self.client.create(**self._payload(prompt))
        content = response.choices[0].message.content if response.choices else ''
        return _TextResponse(content or '', _usage(response.usage))

    def stream(self, prompt):
        complete = False
        with self.client.create(**self._payload(prompt), stream=True, stream_options={'include_usage': True}) as response:
            for event in response:
                if event.choices and event.choices[0].finish_reason:
                    complete = True
                content = event.choices[0].delta.content if event.choices else ''
                yield _TextResponse(content or '', _usage(event.usage))
        if not complete:
            raise RuntimeError('OpenAI-compatible generation stream ended before completion')

    def close(self):
        self._sdk.close()


class Ollama:
    """Local Ollama /api/generate adapter with deterministic stream cleanup."""
    def __init__(self, *, model, base_url, timeout=120, http_client=None):
        self.model, self.base_url, self.timeout = model, base_url.rstrip('/'), timeout
        self.num_predict = None
        # Expose an owned client resource so explicit model probes can close it.
        self.client = SimpleNamespace(_client=http_client or httpx.Client(timeout=timeout, trust_env=False))

    def _payload(self, prompt, stream):
        payload = {'model': self.model, 'prompt': str(prompt), 'stream': stream}
        if self.num_predict is not None:
            payload['options'] = {'num_predict': self.num_predict}
        return payload

    @staticmethod
    def _result(payload):
        if payload.get('error'):
            raise RuntimeError('Ollama generation failed')
        usage = {target: payload[source] for source, target in (
            ('prompt_eval_count', 'input_tokens'), ('eval_count', 'output_tokens')) if source in payload}
        return _TextResponse(str(payload.get('response') or ''), usage)

    def invoke(self, prompt):
        response = self.client._client.post(self.base_url + '/api/generate', json=self._payload(prompt, False))
        response.raise_for_status()
        return self._result(response.json())

    def stream(self, prompt):
        with self.client._client.stream('POST', self.base_url + '/api/generate', json=self._payload(prompt, True)) as response:
            response.raise_for_status()
            complete = False
            for line in response.iter_lines():
                if not line.strip():
                    continue
                payload = json.loads(line)
                yield self._result(payload)
                if payload.get('done'):
                    complete = True
                    break
            if not complete:
                raise RuntimeError('Ollama generation stream ended before completion')

    def close(self):
        self.client._client.close()


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

    @measured_model_call
    def generate(self, prompt: str) -> str:
        response = self._model.invoke(prompt)
        record_usage(getattr(response, "usage_metadata", None) or (getattr(response, "response_metadata", {}) or {}).get("token_usage", {}))
        content = getattr(response, "content", response)
        return str(content)

    @measured_model_call
    def stream(self, prompt: str) -> Iterator[str]:
        usage = {}
        chunks = self._model.stream(prompt)
        try:
            for chunk in chunks:
                reported = getattr(chunk, "usage_metadata", None) or (getattr(chunk, "response_metadata", {}) or {}).get("token_usage", {})
                if isinstance(reported, dict):
                    usage.update(reported)
                content = getattr(chunk, "content", chunk)
                if content:
                    yield str(content)
        finally:
            if hasattr(chunks, 'close'):
                chunks.close()
            record_usage(usage)


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

    @measured_model_call
    def generate(self, prompt: str) -> str:
        response = self._model.invoke(prompt)
        record_usage(getattr(response, 'usage_metadata', {}))
        return str(response)

    @measured_model_call
    def stream(self, prompt: str) -> Iterator[str]:
        usage = {}
        chunks = self._model.stream(prompt)
        try:
            for chunk in chunks:
                usage.update(getattr(chunk, 'usage_metadata', {}))
                content = getattr(chunk, "content", chunk)
                if content:
                    yield str(content)
        finally:
            if hasattr(chunks, 'close'):
                chunks.close()
            record_usage(usage)


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

    @measured_model_call
    def generate(self, prompt: str) -> str:
        response = self.client.post(
            self.endpoint,
            headers=self.headers,
            json=self._payload(prompt),
        )
        response.raise_for_status()
        payload = response.json()
        record_usage(payload.get("usage", {}))
        return self._text_from_response(payload)

    @measured_model_call
    def stream(self, prompt: str) -> Iterator[str]:
        usage = {}
        try:
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
                    reported = event.get("usage") or (event.get("message") or {}).get("usage")
                    if isinstance(reported, dict):
                        usage.update(reported)
                    delta = event.get("delta") or {}
                    if event.get("type") == "content_block_delta":
                        text = delta.get("text")
                        if text:
                            yield str(text)
        finally:
            record_usage(usage)
