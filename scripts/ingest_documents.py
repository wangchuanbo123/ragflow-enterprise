from pathlib import Path

import nltk
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_experimental.text_splitter import SemanticChunker

from app.core.config import DOC_DIR, VECTOR_DB_DIR
from rag.embeddings.embedding_model import get_embedding_model
from rag.loaders.document_loader import is_supported_document, load_document
from rag.utils.file_hash import file_hash
from rag.vectorstore.chroma_store import load_vector_store


BATCH_SIZE = 256
COARSE_CHUNK_SIZE = 300
COARSE_CHUNK_OVERLAP = 50
FINAL_CHUNK_SIZE = 300
FINAL_CHUNK_OVERLAP = 50


def ensure_nltk():
    resources = [
        "punkt",
        "punkt_tab",
        "stopwords",
        "wordnet",
        "averaged_perceptron_tagger",
        "averaged_perceptron_tagger_eng",
    ]

    for resource in resources:
        try:
            nltk.download(resource)
        except Exception as e:
            print("NLTK download skipped:", resource, e)


def get_existing_hashes(db):
    existing_hashes = set()

    try:
        existing = db.get(include=["metadatas"])
    except Exception as e:
        print("Read existing vector db failed, maybe first run:", e)
        return existing_hashes

    if not existing or "metadatas" not in existing:
        return existing_hashes

    for metadata in existing["metadatas"]:
        if not metadata:
            continue

        hash_value = metadata.get("file_hash")
        if hash_value:
            existing_hashes.add(hash_value)

    return existing_hashes


def scan_supported_files(doc_dir):
    doc_dir = Path(doc_dir)

    if not doc_dir.exists():
        print("Document directory does not exist:", doc_dir)
        return []

    all_files = [path for path in doc_dir.rglob("*") if path.is_file()]
    supported_files = [path for path in all_files if is_supported_document(path)]
    skipped_count = len(all_files) - len(supported_files)

    print("Scanned files:", len(all_files))
    print("Supported files:", len(supported_files))
    print("Skipped files:", skipped_count)

    return sorted(supported_files)


def add_documents_in_batches(db, docs):
    for start in range(0, len(docs), BATCH_SIZE):
        batch = docs[start:start + BATCH_SIZE]
        db.add_documents(batch)
        print(f"Written chunks: {start + len(batch)}/{len(docs)}")


def split_documents(docs, embedding):
    coarse_splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
        chunk_size=COARSE_CHUNK_SIZE,
        chunk_overlap=COARSE_CHUNK_OVERLAP,
    )
    coarse_docs = coarse_splitter.split_documents(docs)
    print("Coarse chunks:", len(coarse_docs))

    if not coarse_docs:
        return []

    semantic_splitter = SemanticChunker(embedding)

    try:
        semantic_docs = semantic_splitter.split_documents(coarse_docs)
    except ValueError as e:
        if "context length" not in str(e):
            raise

        print("Semantic split skipped because embedding input exceeded context length.")
        print("Using coarse chunks instead.")
        semantic_docs = coarse_docs

    print("Semantic chunks:", len(semantic_docs))

    if not semantic_docs:
        return []

    final_splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
        chunk_size=FINAL_CHUNK_SIZE,
        chunk_overlap=FINAL_CHUNK_OVERLAP,
    )
    final_docs = final_splitter.split_documents(semantic_docs)

    if len(final_docs) != len(semantic_docs):
        print("Applied final length guard.")

    return final_docs


def main():
    ensure_nltk()

    print("Start building/updating vector database...")

    embedding = get_embedding_model()

    db = load_vector_store(
        embedding=embedding,
        persist_dir=str(VECTOR_DB_DIR),
    )

    existing_hashes = get_existing_hashes(db)
    print("Existing indexed files:", len(existing_hashes))

    files = scan_supported_files(DOC_DIR)

    new_files = []
    for path in files:
        hash_value = file_hash(path)
        if hash_value not in existing_hashes:
            new_files.append((path, hash_value))

    print("New files to index:", len(new_files))

    if not new_files:
        print("No new files. Index update finished.")
        return

    all_docs = []

    for path, hash_value in new_files:
        print("Loading:", path)
        docs = load_document(path)

        if not docs:
            print("No readable content, skipped:", path)
            continue

        for doc in docs:
            doc.metadata["file_hash"] = hash_value
            doc.metadata["source"] = str(path)

        all_docs.extend(docs)

    print("Loaded documents:", len(all_docs))

    if not all_docs:
        print("No readable documents. Index update stopped.")
        return

    docs = split_documents(all_docs, embedding)

    if not docs:
        print("No chunks generated. Index update stopped.")
        return

    avg_chunk_size = sum(len(doc.page_content) for doc in docs) / len(docs)
    print("Average chunk size:", avg_chunk_size)

    for index, doc in enumerate(docs):
        doc.metadata["chunk_id"] = index

    print("Final chunks:", len(docs))

    add_documents_in_batches(db, docs)
    db.persist()

    print("Incremental index completed.")
    print("Vector db path:", VECTOR_DB_DIR)


if __name__ == "__main__":
    main()
