# Enterprise RAG

这是一个面向本地和企业知识库场景的 RAG 项目，包含：

- FastAPI 后端和 Vue 3 Web 聊天界面。
- 用户登录、会话、消息持久化和 SSE 流式回答。
- 向量检索、中文 BM25、问题改写、RRF 融合和 BGE 重排。
- 文档索引元数据、Chroma collection 管理和持久索引任务。
- SQLite 知识图谱抽取、存储和三路检索接线。
- 基于真实文档问题集的检索与回答评估。

## 当前状态

项目仍在开发和验收阶段。模块文件基本齐全，但不能把两份实施方案理解为
“所有功能已经可用于生产”。

截至 2026-07-30：

- 全量重建、分批写入失败补偿、旧向量清理重试、上传 ID 和 readiness
  一致性检查已修复。
- 索引状态变更会跨进程刷新 Runtime、LangGraph 和 BM25 缓存。
- collection 回滚会先校验完整 Chunk ID 集，不兼容的旧索引会被拒绝。
- 实体问题匹配和 RRF `graph_facts` 合并已修复。
- `/ask` 已接入登录鉴权；同步、SSE 和 LangGraph 问答均受统一并发限制。
- Provider 超时、真实 Index Worker readiness 和优雅停止已经接入。
- 图谱 Schema 版本、置信度过滤和单次请求批量抽取已经实现。
- 当前实际活动索引尚未重新构建，仍为 243 条向量与 115 条 SQLite Chunk，
  因此 readiness 正确返回 `not_ready`。
- 知识图谱表当前没有已构建的实体和关系。
- 评估路径归一化代码已修复，但历史结果尚未重新生成，最终 holdout 仍未完成。
- 完整阶段耗时日志和独立 `graph_subgraph` 输出仍未实现。

在对重要数据执行 `--sync`、`--rebuild --yes` 或 `--graph-only` 前，请先阅读
[当前实现状态](docs/当前实现状态.md)。

## 文档导航

- [项目文档导航](docs/README.md)
- [代码阅读指南](docs/代码阅读指南.md)
- [当前实现状态](docs/当前实现状态.md)
- [RAG 核心优化实施方案](RAG核心优化实施方案.md)
- [知识图谱实施方案](知识图谱实施方案.md)
- [RAG 架构图](RAG架构图.puml)
- [RAG 问答时序图](RAG时序图.puml)
- [索引与任务流程图](索引与任务流程图.puml)
- [知识图谱流程图](知识图谱流程图.puml)

## 技术栈

| 类型 | 当前实现 |
| --- | --- |
| API | FastAPI |
| Web | Vue 3、TypeScript、Vite |
| 工作流 | LangGraph |
| LLM | 智谱兼容 API `glm-5.2` |
| 可选本地回退 LLM | Ollama `qwen3:4b` |
| Embedding | Ollama `nomic-embed-text-v2-moe` |
| Reranker | `BAAI/bge-reranker-base` |
| 向量库 | Chroma |
| 业务数据库 | SQLite + SQLAlchemy + Alembic |
| 关键词检索 | rank-bm25 + jieba |
| 图谱存储 | SQLite |

## 环境要求

- Windows 10/11 或兼容的 Linux 环境。
- Python 3.11。项目当前测试环境为 Python 3.11.9。
- Ollama。即使回答使用云端 LLM，默认 Embedding 仍依赖 Ollama。
- Node.js 和 npm。只有运行或构建 Web 前端时需要。

Python 3.12 可能可以运行，但当前锁定依赖和自动化测试以 Python 3.11 为准。

## 首次安装

在项目根目录执行：

```powershell
py -3.11 -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

创建本机配置：

```powershell
if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
}
```

项目只读取 `.env`：

- `.env.example` 是可以提交到 Git 的配置模板。
- `.env` 保存本机实际配置和密钥，已被 `.gitignore` 忽略。

## 模型准备

启动 Ollama 后执行：

```powershell
ollama pull nomic-embed-text-v2-moe
ollama list
```

只有需要无网回退到本地 LLM 时才执行 `ollama pull qwen3:4b`。

首次加载 BGE Reranker 会从 Hugging Face 缓存读取：

```powershell
python -c "from sentence_transformers import CrossEncoder; CrossEncoder('BAAI/bge-reranker-base')"
```

当前 `glm-5.2` 配置依赖可访问的云端兼容 API；Embedding 和 Reranker
仍在本机运行。

## LLM 配置

### 使用 Ollama

```dotenv
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_LLM_MODEL=qwen3:4b
```

### 使用兼容 OpenAI 协议的云端 API

```dotenv
LLM_PROVIDER=zhipu
LLM_MODEL=glm-5.2
LLM_API_KEY=your-api-key
LLM_BASE_URL=https://example.com/v1
GRAPH_EXTRACTION_PROVIDER=current
```

当 `LLM_PROVIDER` 为智谱兼容模式，但 `LLM_API_KEY` 或 `LLM_BASE_URL` 任意一个为空时，
当前 Provider 工厂会回退到 Ollama。
`GRAPH_EXTRACTION_PROVIDER=current` 表示图谱抽取复用同一个 `glm-5.2` Provider。

默认 Embedding 配置：

```dotenv
EMBEDDING_PROVIDER=ollama
EMBEDDING_MODEL=nomic-embed-text-v2-moe
OLLAMA_BASE_URL=http://localhost:11434
```

## 业务文档目录

知识库文件放在：

```text
data/docs/
```

当前 Loader 支持：

```text
.txt  .pdf  .md  .docx  .html  .htm
```

项目开发文档放在根目录 `docs/`，不要放入 `data/docs/`。

## 索引检查与管理

只读检查命令：

```powershell
# 查看文件分类，不修改索引
python -m scripts.ingest_documents --dry-run

# 查看 Chroma collections
python -m scripts.ingest_documents --list-collections
```

写操作命令：

```powershell
# 增量同步
python -m scripts.ingest_documents --sync

# 创建影子 collection 并切换
python -m scripts.ingest_documents --rebuild --yes

# 仅当完整 Chunk ID 集与当前 SQLite 一致时才允许切换
python -m scripts.ingest_documents --rollback-collection <collection_name> --yes

# 删除非活动 collection
python -m scripts.ingest_documents --cleanup-collection <collection_name> --yes
```

全量重建会重新处理磁盘上的全部支持文档，写入新的影子 collection；任一文件
失败或向量 ID 校验失败都不会切换。单文件更新会先写新向量和 SQLite，再清理
旧向量。重要语料仍建议预先备份 `data/app.db` 和 `data/vector_db`。
旧 collection 默认作为备份保留，但当前 SQLite 只保存一代 Chunk；因此只有
Chunk ID 完全一致的 collection 才允许直接切换，其他情况必须重新构建。

## 启动后端

```powershell
.\venv\Scripts\Activate.ps1
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

启动时会：

1. 执行 Alembic migration 到最新版本。
2. 用户表为空时创建初始管理员。
3. 恢复遗留的 running 索引任务。
4. 根据配置启动单线程 Index Worker。

访问：

- API 文档：`http://127.0.0.1:8000/docs`
- 存活检查：`http://127.0.0.1:8000/api/v1/health`
- 就绪检查：`http://127.0.0.1:8000/api/v1/ready`

readiness 会校验活动 collection 是否存在，并比较 Chroma 与 SQLite 的
Chunk 数量和完整 ID 集合；不一致时返回 `not_ready`。

## 初始管理员

初始管理员由 `.env` 控制：

```dotenv
INITIAL_ADMIN_USERNAME=admin
INITIAL_ADMIN_PASSWORD=
JWT_SECRET_KEY=
```

如果密码或 JWT 密钥为空，当前配置模块会生成随机值并写回 `.env`。应用不会把密码
重复输出到日志，需在本机 `.env` 中查看。

## 启动 Web 前端

### 开发模式

后端运行在 8000 端口时，打开另一个 PowerShell：

```powershell
cd web
npm install
npm run dev
```

访问 `http://localhost:5173`。

### 单进程部署

```powershell
cd web
npm install
npm run build
cd ..
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

如果 `web/dist` 存在，FastAPI 会在 API 路由之后挂载静态前端。

## API 概览

### 兼容问答

```text
POST /ask
```

该接口执行编译后的 LangGraph，并要求先通过 `/api/v1/auth/login` 登录。

### 登录和会话

```text
POST   /api/v1/auth/login
POST   /api/v1/auth/logout
GET    /api/v1/auth/me

POST   /api/v1/conversations
GET    /api/v1/conversations
GET    /api/v1/conversations/{id}
PATCH  /api/v1/conversations/{id}
DELETE /api/v1/conversations/{id}

POST /api/v1/conversations/{id}/messages
POST /api/v1/conversations/{id}/messages/stream
```

Web 聊天接口复用 RAG 节点函数，但当前由 `RAGService` 手工编排，不调用编译后的 Graph。

### 文档和索引任务

```text
POST   /api/v1/documents
GET    /api/v1/documents
GET    /api/v1/documents/{id}
DELETE /api/v1/documents/{id}
POST   /api/v1/documents/{id}/reindex

POST /api/v1/index-jobs/sync
GET  /api/v1/index-jobs
GET  /api/v1/index-jobs/{id}
```

上传、删除、重建和同步操作要求管理员权限。

### 知识图谱

```text
GET /api/v1/knowledge-graph/stats
GET /api/v1/knowledge-graph/entities?query=任务调度
GET /api/v1/knowledge-graph/entities/{id}
GET /api/v1/knowledge-graph/entities/{id}/neighbors?hops=1
```

当前尚未实现按文档获取子图的 API。

## 当前 LangGraph

节点定义：

```text
rewrite
  -> retrieve
  -> rerank
  -> build_context
  -> generate
  -> END
```

- `rewrite`：保留原始问题并生成改写问题。
- `retrieve`：向量、中文 BM25、知识图谱检索和 RRF 融合。
- `rerank`：使用原始问题进行重排。
- `build_context`：去重、来源配额、Token 预算和引用。
- `generate`：使用 LLM 生成答案。

知识图谱不是独立节点，而是 `retrieve` 内部的第三条检索通道。

## 知识图谱构建

必须先保证文档索引完整、一致，再执行图谱构建：

```powershell
# 对 pending Chunk 构建图谱
python -m scripts.ingest_documents --graph-only

# 重试失败 Chunk
python -m scripts.ingest_documents --retry-failed
```

当前实现复用 `glm-5.2`，按字符预算把多个 Chunk 合并为一次 API 请求，经过
Schema 白名单、原文 evidence 和 `GRAPH_MIN_CONFIDENCE` 校验后写入 SQLite：

```text
kg_entities
kg_aliases
kg_entity_mentions
kg_relations
kg_relation_evidence
```

当前尚未实现实体向量 collection，也没有把 `text_context` 和 `graph_subgraph`
分别传给智能体。图谱事实暂存在 Document metadata 的 `graph_facts`。

## 导出回答前的上下文 JSON

需要检查“最终给 LLM 的知识内容”时，在 `.env` 中启用：

```dotenv
CONTEXT_JSON_EXPORT_ENABLED=true
CONTEXT_JSON_EXPORT_DIR=data/debug/contexts
```

重启后端后，每次问答都会生成一个 `context_*.json`。文件包含：

- 原始问题、改写后的检索问题。
- `llm_input.context`：回答 Prompt 使用的最终上下文。
- 来源、Chunk ID、检索通道和融合分数。
- 当前上下文中保留下来的 `graph_facts`。

该开关默认关闭。导出内容可能包含业务文档原文，仅建议在本地调试时开启；
`data/` 已被 `.gitignore` 忽略。

## 项目结构

```text
app/                       FastAPI 应用
├── api/                   路由、依赖和错误处理
├── core/                  配置、数据库、日志和安全
├── models/                ORM
├── repositories/          数据访问
├── schemas/               API Schema
└── services/              业务服务

rag/                       RAG 核心
├── context/               上下文构建
├── graph/                 LangGraph
├── indexing/              切片和索引管理
├── knowledge_graph/       图谱抽取、存储和检索
├── nodes/                 五个 RAG 节点
├── prompts/               Prompt 模板
├── providers/             模型和存储 Provider
├── retrievers/            BM25、向量、图谱和 RRF
├── runtime/               运行时依赖
└── state/                 RAGState

scripts/                   索引和评估命令
eval/                      评估问题与结果
tests/                     自动化测试
web/                       Vue 前端
docs/                      项目开发文档
data/docs/                 业务知识库
data/vector_db/            Chroma
data/app.db                SQLite
```

更详细的阅读顺序见[代码阅读指南](docs/代码阅读指南.md)。

## 测试

```powershell
.\venv\Scripts\python.exe -m pytest -q
```

2026-07-30 的检查结果：

```text
75 passed
2 subtests passed
14 warnings
```

测试已覆盖 unchanged 全量重建、失败回滚、单文件保留旧索引、readiness
不一致、分批写入补偿、旧向量清理失败后的自愈重试、不兼容回滚拒绝、
上传校验、`/ask` 鉴权、图谱
Schema 重抽取、批量调用、置信度过滤、自然语言实体匹配和 RRF 图谱 metadata
合并。真实 27 文档全量重建和最终 holdout 仍需单独执行。

## 评估

问题集位于 `eval/data/questions.jsonl`，包含 tuning 和 holdout。

```powershell
# 只评估检索
python -m scripts.evaluate_rag --mode retrieval --split tuning --label run

# 完整回答评估
python -m scripts.evaluate_rag --mode full --split tuning --label run

# 最终留出集
python -m scripts.evaluate_rag --mode full --split holdout --label final

# 对比两个结果
python -m scripts.evaluate_rag --compare <baseline.json> <final.json>
```

评估脚本已经能把 Windows 绝对路径和相对路径统一为 `data/docs` 相对路径。
历史 baseline 仍保留旧错误结果，不应直接作为最终验收依据；重建完成后应重新
生成 tuning 和 holdout。详情见 [评估说明](eval/README.md)。

## 停止服务

在运行 `uvicorn`、Vite 或 `ollama serve` 的窗口按 `Ctrl+C`。

退出虚拟环境：

```powershell
deactivate
```

## License

Apache License 2.0
