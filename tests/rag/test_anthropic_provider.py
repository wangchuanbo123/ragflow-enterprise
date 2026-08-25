import json

import httpx

from rag.providers.factory import create_llm_provider
from rag.providers.llm import AnthropicCompatibleLLMProvider


def test_factory_creates_anthropic_compatible_provider():
    provider = create_llm_provider(
        provider="anthropic_compatible",
        model="glm-5.2",
        api_key="test-key",
        base_url="https://example.test/v1/messages",
    )

    assert isinstance(provider, AnthropicCompatibleLLMProvider)
    assert provider.endpoint == "https://example.test/v1/messages"


def test_anthropic_generate_uses_messages_protocol():
    def handler(request: httpx.Request):
        assert request.url.path == "/v1/messages"
        assert request.headers["authorization"] == "Bearer test-key"
        assert request.headers["anthropic-version"] == "2023-06-01"
        payload = json.loads(request.content)
        assert payload["model"] == "glm-5.2"
        assert payload["messages"] == [{"role": "user", "content": "Hello"}]
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": "你好"}],
        })

    provider = AnthropicCompatibleLLMProvider(
        model="glm-5.2",
        api_key="test-key",
        base_url="https://example.test",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert provider.generate("Hello") == "你好"


def test_anthropic_stream_yields_text_deltas():
    body = "\n".join([
        'event: content_block_delta',
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"你"}}',
        "",
        'event: content_block_delta',
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"好"}}',
        "",
    ])

    provider = AnthropicCompatibleLLMProvider(
        model="glm-5.2",
        api_key="test-key",
        base_url="https://example.test/v1",
        client=httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=body)
        )),
    )

    assert "".join(provider.stream("Hello")) == "你好"
