from functools import cached_property

from langchain_community.llms import Ollama
from langchain_openai import ChatOpenAI


class ZhipuLLMProvider:
    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str,
        temperature: float = 0,
    ):
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.temperature = temperature

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
            timeout=120,
        )

    def get_model(self):
        return self._model

    def generate(self, prompt: str) -> str:
        response = self._model.invoke(prompt)
        content = getattr(response, "content", response)
        return str(content)


class OllamaLLMProvider:
    def __init__(self, model: str, base_url: str):
        self.model = model
        self.base_url = base_url

    @cached_property
    def _model(self):
        return Ollama(model=self.model, base_url=self.base_url)

    def get_model(self):
        return self._model

    def generate(self, prompt: str) -> str:
        return str(self._model.invoke(prompt))
