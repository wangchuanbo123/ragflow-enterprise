from pathlib import Path

from langchain_community.document_loaders import (
    Docx2txtLoader,
    PyPDFLoader,
    TextLoader,
    UnstructuredHTMLLoader,
    UnstructuredMarkdownLoader,
)


SUPPORTED_EXTENSIONS = {".txt", ".pdf", ".md", ".docx", ".html", ".htm"}


def is_supported_document(path):
    path = Path(path)

    return (
        path.is_file()
        and path.suffix.lower() in SUPPORTED_EXTENSIONS
        and not path.name.startswith("~$")
    )


def load_document(file_path):
    path = Path(file_path)
    suffix = path.suffix.lower()

    if suffix == ".txt":
        loader = TextLoader(
            str(path),
            encoding="utf-8",
            autodetect_encoding=True,
        )
    elif suffix == ".pdf":
        loader = PyPDFLoader(str(path))
    elif suffix == ".md":
        loader = UnstructuredMarkdownLoader(str(path))
    elif suffix == ".docx":
        loader = Docx2txtLoader(str(path))
    elif suffix in {".html", ".htm"}:
        loader = UnstructuredHTMLLoader(str(path))
    else:
        return []

    try:
        docs = loader.load()
    except Exception as e:
        print("加载失败:", path, e)
        return []

    return [doc for doc in docs if doc.page_content.strip()]


def load_documents(doc_path):
    docs = []

    for path in Path(doc_path).rglob("*"):
        if not is_supported_document(path):
            continue

        docs.extend(load_document(path))

    return docs
