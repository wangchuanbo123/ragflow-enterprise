# 🚀 Enterprise-Level RAG System

一个企业级 RAG（Retrieval-Augmented Generation）系统，支持混合检索、重排序、问题改写、增量索引及评估体系。

---

# 📚 目录

* [📦 安装与部署](#-安装与部署)
* [🛠️ 常用命令](#️-常用命令)
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

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

成功后提示：

```
(venv)
```

---

## 3️⃣ 安装依赖

```powershell
pip install -r requirements.txt
```

⏱️ 首次安装约 10–15 分钟

---

## 4️⃣ 配置模型

项目根目录包含两个配置文件：

* `.env.example`：配置模板，可以提交到 Git，不填写真实密钥。
* `.env`：本机实际配置，包含真实密钥，已被 `.gitignore` 忽略。

首次配置时执行：

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

程序只读取 `.env`。不建议合并两个文件，否则容易把真实 API Key 提交到 Git。

只有 `LLM_API_KEY` 和 `LLM_BASE_URL` 都存在时才使用云端模型；任意一个为空时，系统自动使用 Ollama 的 `OLLAMA_LLM_MODEL`。Ollama 同时负责本地向量化。

云端模型示例：

```dotenv
LLM_PROVIDER=zhipu
LLM_MODEL=glm-5.2
LLM_API_KEY=your-api-key
LLM_BASE_URL=https://api.z.ai/api/coding/paas/v4
```

本地回退模型：

```dotenv
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_LLM_MODEL=qwen3:4b
```

```powershell
ollama --version
```

拉取本地模型：

```powershell
ollama pull qwen3:4b
ollama pull nomic-embed-text-v2-moe
```

确认模型已经下载：

```powershell
ollama list
```

---

## 5️⃣ 准备测试文档

创建目录：

```bash
data/docs/
```

放入测试文件（支持 txt / pdf / docx / md）

---

## 6️⃣ 初始化向量数据库

首次运行或文档发生变化时执行：

```powershell
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

```powershell
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

本地访问：

```powershell
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
确认 Ollama 正在运行
→ 首次运行或文档变化时更新向量库
→ 启动 API
→ 第一次提问时懒加载 RAGRuntime
→ 调用接口
```

---

## 🔟 运行验证

```powershell
cd D:\MyCode\ragflow-enterprise
.\venv\Scripts\Activate.ps1
ollama list
uvicorn app.main:app --reload
```

如果 `ollama list` 无法连接，请先启动 Ollama 应用，或者在另一个 PowerShell 窗口执行 `ollama serve`。如果 `data/vector_db` 不存在，再先执行 `python -m scripts.ingest_documents`。

出现：

```
Application startup complete
```

---

# 🛠️ 常用命令

除 `ollama serve` 外，以下命令都在项目根目录的 PowerShell 中执行。

## 首次安装

只需执行一次：

```powershell
cd D:\MyCode\ragflow-enterprise
py -3.11 -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
ollama pull qwen3:4b
ollama pull nomic-embed-text-v2-moe
python -c "from sentence_transformers import CrossEncoder; CrossEncoder('BAAI/bge-reranker-base')"
python -m scripts.ingest_documents
```

## 日常启动

先确认 Ollama 可用：

```powershell
ollama list
```

如果无法连接，请启动 Ollama 应用；也可以在单独的 PowerShell 窗口运行：

```powershell
ollama serve
```

然后在项目窗口启动 API：

```powershell
cd D:\MyCode\ragflow-enterprise
.\venv\Scripts\Activate.ps1
uvicorn app.main:app --reload
```

本地接口文档：`http://127.0.0.1:8000/docs`

即使 LLM 使用智谱云端模型，Embedding 仍使用本地 Ollama，因此 Ollama 服务仍需运行。

## 更新知识库

向 `data/docs` 添加或修改文档后执行。脚本采用增量索引，不需要每次启动都运行：

```powershell
.\venv\Scripts\Activate.ps1
python -m scripts.ingest_documents
```

## 调用问答接口

API 启动后，可以在另一个 PowerShell 窗口执行：

```powershell
$body = @{ query = "知识库中有哪些系统测试要求？" } | ConvertTo-Json
Invoke-RestMethod `
    -Uri "http://127.0.0.1:8000/ask" `
    -Method Post `
    -ContentType "application/json; charset=utf-8" `
    -Body $body
```

## 运行测试

```powershell
.\venv\Scripts\Activate.ps1
python -m unittest discover -s tests -v
```

## 运行 RAGAS 评估

评估会调用当前配置的 LLM，并可能产生云端 API 费用或消耗本地计算资源：

```powershell
.\venv\Scripts\Activate.ps1
python -m scripts.evalute_rag
```

## 停止项目

在运行 `uvicorn` 或 `ollama serve` 的窗口按 `Ctrl + C`。退出 Python 虚拟环境：

```powershell
deactivate
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

* 第一次提问时懒加载 Retriever（避免重复构建）
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

项目通过 Provider 接口隔离业务流程和具体模型实现：

```text
FastAPI API
    ↓
RAG Service
    ↓
LangGraph Workflow
    ↓
RAGRuntime（依赖容器）
    ↓
LLMProvider / EmbeddingProvider / Reranker / VectorStoreProvider
    ↓
Zhipu / Ollama / BGE / Chroma
```

`RAGRuntime` 在第一次提问时懒加载模型、向量库和检索器，并将依赖注入 LangGraph 节点。节点不直接创建模型，因此可以在测试或部署时替换具体 Provider。

多节点处理：

* rewrite
* retrieve
* rerank
* generate

切换模型供应商只需修改 `.env`。例如切换回 Ollama LLM：

```dotenv
LLM_PROVIDER=ollama
OLLAMA_LLM_MODEL=qwen3:4b
OLLAMA_BASE_URL=http://localhost:11434
```

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
* [ ] Docker 部署
* [ ] CI/CD 支持

---

# 📜 License

Apache License 2.0
