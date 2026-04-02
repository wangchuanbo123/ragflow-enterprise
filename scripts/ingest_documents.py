import shutil
from pathlib import Path
import nltk

# ===== nltk 资源 =====
def ensure_nltk():
    resources = [
        "punkt",
        "punkt_tab",
        "stopwords",
        "wordnet",
        "averaged_perceptron_tagger",
        "averaged_perceptron_tagger_eng"
    ]

    for r in resources:
        try:
            nltk.download(r)
        except:
            pass

ensure_nltk()

# ===== 配置 =====
from app.core.config import DOC_DIR, VECTOR_DB_DIR

# ===== RAG模块 =====
from rag.loaders.document_loader import load_documents
from rag.embeddings.embedding_model import get_embedding_model
from rag.vectorstore.chroma_store import load_vector_store

# ===== chunk =====
from langchain_experimental.text_splitter import SemanticChunker
from langchain.text_splitter import RecursiveCharacterTextSplitter

# ===== 新增 =====
from rag.utils.file_hash import file_hash

print("开始构建 / 更新向量数据库...")

# 1 embedding
embedding = get_embedding_model()

# 2 vector db
db = load_vector_store(
    embedding=embedding,
    persist_dir=str(VECTOR_DB_DIR)
)

# 3 获取已有文件hash
existing_hashes = set()

try:
    existing = db.get(include=["metadatas"])

    if existing and "metadatas" in existing:
        for m in existing["metadatas"]:
            h = m.get("file_hash")
            if h:
                existing_hashes.add(h)

except Exception as e:
    print("读取已有向量库失败（可能是首次运行）:", e)

print("已有文件数量:", len(existing_hashes))

# 4 扫描文件
files = []
for p in Path(DOC_DIR).rglob("*"):
    if p.is_file():
        files.append(p)

print("扫描到文件:", len(files))

new_files = []

for file in files:
    h = file_hash(file)

    if h not in existing_hashes:
        new_files.append((file, h))

print("需要新增索引的文件:", len(new_files))

if len(new_files) == 0:
    print("没有新增文件，索引结束")
    exit()

# 5 加载文档
all_docs = []

for file, h in new_files:

    print("新增文件:", file)

    docs = load_documents(str(file.parent))

    for d in docs:
        d.metadata["file_hash"] = h
        d.metadata["source"] = str(file)

    all_docs.extend(docs)

print("加载文档数量:", len(all_docs))

# 6 语义切分
semantic_splitter = SemanticChunker(embedding)
docs = semantic_splitter.split_documents(all_docs)

print("语义分片后:", len(docs))

# 7 判断是否需要第二次切分
avg_chunk_size = sum(len(doc.page_content) for doc in docs) / len(docs)
print("平均 chunk 大小:", avg_chunk_size)

if avg_chunk_size > 600:
    splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
        chunk_size=150,
        chunk_overlap=30
    )
    docs = splitter.split_documents(docs)
    print("进行了第二次分片")

# 8 chunk id
for i, doc in enumerate(docs):
    doc.metadata["chunk_id"] = i

print("最终 chunk 数量:", len(docs))

# 9 写入向量库
db.add_documents(docs)

# 10 持久化
db.persist()

print("增量索引完成")
print("向量库存储位置:", VECTOR_DB_DIR)