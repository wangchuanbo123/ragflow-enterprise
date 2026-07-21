from langchain_community.vectorstores import Chroma


class ChromaVectorStoreProvider:
    def load(self, embedding, persist_dir: str):
        return Chroma(
            persist_directory=persist_dir,
            embedding_function=embedding,
        )
