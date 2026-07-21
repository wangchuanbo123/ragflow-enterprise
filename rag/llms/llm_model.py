def get_llm():
    from rag.providers.factory import get_llm_provider

    return get_llm_provider().get_model()


def invoke_llm(prompt: str) -> str:
    from rag.providers.factory import get_llm_provider

    return get_llm_provider().generate(prompt)
