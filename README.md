# 🚀 Enterprise-Level RAG System

一个企业级 RAG（Retrieval-Augmented Generation）系统，支持混合检索、重排序、问题改写、增量索引及评估体系。

---

# 📚 目录

* [📦 安装与部署](#-安装与部署)
* [✨ 功能特性](#-功能特性)
* [🧠 架构说明](#-架构说明)
* [📊 依赖环境](#-依赖环境)

---

# 📦 安装与部署

> ⚠️ 当前环境：Windows 11

## 1️⃣ 环境准备

```bash
python --version
```

推荐版本：

* Python 3.10 / 3.11

---

## 2️⃣ 创建虚拟环境

```bash
python -m venv venv
venv\Scripts\activate
```

成功后提示：

```
(venv)
```

---

## 3️⃣ 安装依赖

```bash
pip install -r requirements.txt
```

⏱️ 首次安装约 10–15 分钟

---

## 4️⃣ 安装本地大模型（Ollama）

```bash
ollama --version
```

拉取模型：

```bash
ollama pull deepseek-coder:1.3b
```

测试运行：

```bash
ollama run deepseek-coder:1.3b
```

退出：

```bash
Ctrl + C
```

---

## 5️⃣ 准备测试文档

创建目录：

```bash
data/docs/
```

放入测试文件（支持 txt / pdf / docx / md）

---

## 6️⃣ 初始化向量数据库（必须）

```bash
python -m scripts/ingest_documents.py
```

成功后生成：

```
data/vector_db/
├── chroma.sqlite3
└── index/
```

---

## 7️⃣ 启动 API 服务

局域网访问：

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

本地访问：

```bash
uvicorn app.main:app --reload
```

---

## 8️⃣ 接口测试

打开：

```
http://127.0.0.1:8000/docs
```

测试接口：

```json
POST /ask
{
  "query": "OpenAI是什么时候成立的？"
}
```

返回：

```json
{
  "answer": "OpenAI was founded in 2015."
}
```

---

## 9️⃣ 运行流程

```
启动 Ollama
→ 初始化向量库
→ 启动 API
→ 调用接口
```

---

## 🔟 运行验证

```bash
ollama run llama3
python scripts/ingest_documents.py
uvicorn app.main:app --reload
```

出现：

```
Application startup complete
```

---

# ✨ 功能特性

## 🔍 检索能力

* 混合检索（向量 + BM25）
* 高精度召回
* 增量索引（避免全量重建）

## 🧠 推理优化

* Reranker 精排优化
* 问题改写（Rewrite）
* Prompt 解耦管理

## ⚙️ 系统优化

* 启动时加载 Retriever（避免重复构建）
* 配置统一管理
* 向量持久化

## 📄 文档处理

* 支持 txt / pdf / docx / md
* 自动跳过损坏文件
* 编码兼容处理

## 📊 可解释 AI

* 返回引用来源
* 输出引用片段

## 📈 评估体系

* 集成 RAGAS 评估

---

# 🧠 架构说明

> 当前架构与代码一致（图待更新）

包含：

* Retrieval Pipeline
* RAG Graph（LangGraph）

多节点处理：

* rewrite
* retrieve
* rerank
* generate

---

# 📊 依赖环境

## Python 依赖

```txt
langchain==0.1.20
langchain-community==0.0.38
langgraph==0.0.32
chromadb==0.4.24
sentence-transformers==2.7.0
transformers==4.41.2
fastapi==0.110.0
uvicorn==0.29.0
pydantic==2.6.4
python-dotenv==1.0.1
rank-bm25==0.2.2
ollama==0.2.1
unstructured[all-docs]==0.14.9
ragas==0.1.9
datasets==2.19.1
psutil
```

---

## NLP 依赖

```
punkt
stopwords
wordnet
averaged_perceptron_tagger
```

---

# 🧩 项目亮点总结

* 企业级 RAG 架构设计
* 高质量检索 + 重排序
* 增量索引优化
* 可解释 AI 支持
* 完整评估体系

---

# 📌 TODO

* [ ] 自动清理旧向量库
* [ ] 架构图更新
* [ ] Docker 部署
* [ ] CI/CD 支持

---

# 📜 License

Apache License 2.0
