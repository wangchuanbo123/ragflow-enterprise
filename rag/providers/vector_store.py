from langchain_community.vectorstores import Chroma


class ChromaVectorStoreProvider:
    def load(self, embedding, persist_dir: str, collection_name: str | None = None):
        kwargs = {
            "persist_directory": persist_dir,
            "embedding_function": embedding,
        }
        if collection_name:
            kwargs["collection_name"] = collection_name
        return Chroma(**kwargs)
