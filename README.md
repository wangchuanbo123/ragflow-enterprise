# Enterprise RAG

Enterprise RAG 是一套可在本地笔记本和企业服务器上运行的多知识库 RAG 系统。项目把业务服务与模型、数据库、向量库、对象存储、缓存和任务队列解耦：本地环境使用轻量组件完成开发，企业环境使用同一套业务代码，通过配置切换到 PostgreSQL、Chroma Server、MinIO、Redis 和 Celery。

系统不仅支持文档问答，还包含文档知识图谱、代码 AST/符号索引、调用图、依赖图、混合检索路由、数据源同步、图谱治理和完整的 Web 管理工作台。

## 当前状态

截至 2026-08-11，计划内的核心功能代码已经完成，本地集中验证已经通过：

- 27 个现有文档和 3880 个 Chunk 保持完整，残留 `processing` Chunk 为 0。
- 数据库已升级到 Alembic `a38c7d9e0f14 (head)`。
- Python 全量回归为 127 项测试和 2 个 subtests 全部通过。
- Vue TypeScript 检查和 Vite 生产构建通过。
- Python 使用标准库 AST 提取符号和关系；其他支持语言使用 Tree-sitter 做语法解析与错误恢复检查，当前持久化事实由确定性规则 emitter 提取，并保留降级路径。

当前仍处于企业实机验收前：开发机没有 Docker，因此企业 Compose 只做过静态检查，尚未真实启动 PostgreSQL、Chroma Server、MinIO、Redis/Celery 和企业模型端点；真实 Git/S3 连接器、实体向量全量构建、3880 个 Chunk 的知识图谱重抽取、压力与故障恢复也尚未执行。

> 当前可直接配置切换的企业图存储是 PostgreSQL 上的 SQLAlchemy 图表。Neo4j 和 pgvector 尚未提供经过验收的 Adapter，不能仅修改连接串启用。

## 核心功能

| 领域 | 已实现能力 |
| --- | --- |
| 多知识库 | 知识库、成员、文档、Chunk、向量、图谱、代码快照、任务、会话按 `knowledge_base_id` 隔离；支持 owner、editor、viewer 和全局管理员 |
| 文档管理 | 单个/批量上传、目录、标签、元数据、Chunk 查看、不可变版本历史、版本恢复、批量删除和批量重建 |
| 数据源 | 手工上传、本地目录、Git、S3/MinIO；配置检查、同步游标、定时同步、源端删除策略和凭据引用 |
| 索引任务 | 持久化任务与条目、事件、取消、失败项重试、幂等去重、租约、心跳和过期任务恢复 |
| 在线问答 | `/ask`、知识库 `/ask`、Web 同步和 SSE 共用编译后的 LangGraph；回答、来源、路由理由和通道状态可解释 |
| 检索路由 | 用户显式选择、规则意图路由、可选 LLM 结构化回退；文本、知识图谱、代码图条件路由和并行检索 |
| 检索范围 | 按文档、标签、目录和元数据筛选；范围先解析为当前知识库允许的文档 ID，再下推到各检索通道 |
| 文本检索 | Chroma 向量检索、中文 BM25、多查询召回、加权 RRF、BGE/HTTP/透传重排、上下文 Token 预算和来源配额 |
| 文档知识图谱 | Schema Registry、实体类型与关系字典、并行抽取、缓存、标准化、Evidence 校验、置信度过滤、质量门禁和分层社区 |
| 图谱治理 | 文档/实体子图、人工校正与撤销、复核队列、Embedding 合并候选、人工合并/拆分 Overlay、实体语义向量索引 |
| 代码图谱 | 临时 AST、符号索引、源码行号、版本化快照、contains/imports/calls/extends 关系、调用图/依赖图查询 |
| Web 工作台 | 知识问答、文档、数据源与成员、索引任务、知识图谱、图谱治理、代码图谱、检索调试和运行状态页面 |
| 可替换 Provider | 回答/改写/路由/抽取模型、Embedding、Reranker、元数据、向量、图、对象、任务队列和缓存均从业务服务中解耦 |

## 架构文档

- [Enterprise RAG 完整架构图](EnterpriseRAG完整架构图.md)：从 Web、API、服务、LangGraph、离线任务到本地/企业基础设施的完整视图。
- [RAG 架构图](RAG架构图.md)：在线问答、混合路由、三路检索和上下文生成的逻辑架构。
- [RAG 时序图](RAG时序图.md)：同步/SSE 问答、文档索引、图谱构建和连接器/代码索引时序。
- [完整 PlantUML 源文件](EnterpriseRAG完整架构图.puml)
- [RAG PlantUML 源文件](RAG架构图.puml)
- [时序图 PlantUML 源文件](RAG时序图.puml)
- [改造和新增功能实现方案](改造和新增功能实现方案.md)
- [RAG 核心业务处理器基础功能与接口清单](RAG核心业务处理器基础功能与接口清单.md)
- [项目文档导航](docs/README.md)
- [代码阅读指南](docs/代码阅读指南.md)
- [历史实现状态（旧基线）](docs/当前实现状态.md)

## 部署形态

| 能力 | local Profile | enterprise Profile |
| --- | --- | --- |
| 元数据与逻辑图 | SQLite + SQLAlchemy | PostgreSQL + SQLAlchemy |
| 向量库 | Embedded Chroma | Chroma HTTP Server |
| 文件/对象 | `data/docs` 本地目录 | S3/MinIO Bucket |
| 任务队列 | 进程内唤醒 + 持久化任务 | Celery + Redis；任务事实仍在 PostgreSQL |
| 缓存 | 进程内 Memory Cache | Redis Cache/PubSub |
| Worker | API 进程内单 Worker | 独立 Celery Worker，可增加副本 |
| 定时同步 | 本地 Scheduler | Celery Beat |
| 模型 | Ollama、兼容 OpenAI API、本地 BGE | 明确配置的 OpenAI-compatible 模型网关和可选 HTTP Reranker |

两种 Profile 使用同一套 API、应用服务、LangGraph、Repository 和任务处理代码。企业迁移不要求复制本地文档或索引；可以在空服务器上初始化后重新上传文档。

## 技术栈

| 类型 | 当前实现 |
| --- | --- |
| API | FastAPI 0.110、Pydantic 2 |
| Web | Vue 3、TypeScript、Vite |
| 工作流 | LangGraph |
| ORM / Migration | SQLAlchemy 2、Alembic |
| LLM | Ollama、智谱兼容 API、OpenAI-compatible API |
| Embedding | Ollama 或 OpenAI-compatible Embedding |
| Reranker | BGE、HTTP Reranker 或 Passthrough |
| 向量库 | Embedded Chroma / Chroma HTTP |
| 关键词检索 | rank-bm25 + jieba |
| 任务 | 本地 Worker / Celery + Redis |
| 对象存储 | 本地目录 / S3 / MinIO |
| 代码解析 | Python AST；Tree-sitter Language Pack + 非 Python 确定性规则 emitter |

## 本地快速开始

### 1. 环境要求

- Windows 10/11 或兼容 Linux。
- Python 3.11；当前验证环境为 Python 3.11。
- Node.js 和 npm，仅开发或构建 Web 时需要。
- 默认本地 Embedding 需要 Ollama；也可以配置兼容 OpenAI 协议的远程 Embedding。

### 2. 安装 Python 依赖

```powershell
py -3.11 -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 3. 创建本地配置

```powershell
if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
}
```

`.env.example` 可以提交，`.env` 保存实际密钥并已被 Git 忽略。最小本地 Profile 为：

```dotenv
APP_PROFILE=local
METADATA_STORE_PROVIDER=sqlite
DATABASE_URL=sqlite:///./data/app.db
VECTOR_STORE_PROVIDER=chroma_embedded
OBJECT_STORE_PROVIDER=local
TASK_QUEUE_PROVIDER=inline
CACHE_PROVIDER=memory
GRAPH_STORE_PROVIDER=sqlite
```

### 4. 准备本地模型

使用默认 Ollama Embedding：

```powershell
ollama pull nomic-embed-text-v2-moe
ollama list
```

如果回答模型也使用 Ollama：

```dotenv
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_LLM_MODEL=qwen3:4b
```

如果使用兼容 OpenAI 协议的服务：

```dotenv
LLM_PROVIDER=openai_compatible
LLM_MODEL=your-model
LLM_BASE_URL=https://model-gateway.example/v1
LLM_API_KEY=your-api-key
```

`ANSWER_LLM_*`、`REWRITE_LLM_*`、`ROUTER_LLM_*` 和 `GRAPH_EXTRACTION_*` 可以分别覆盖默认 `LLM_*`。本地 Profile 在远程 LLM 配置缺失时允许回退 Ollama；enterprise Profile 会直接启动失败，防止静默切换模型。

### 5. 启动后端

```powershell
.\venv\Scripts\Activate.ps1
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

启动过程会自动：

1. 执行 Alembic migration 到最新版本。
2. 创建初始管理员和默认知识库。
3. 恢复租约已过期的持久化任务。
4. 按 local Profile 启动进程内 Worker 和连接器 Scheduler。

常用地址：

- Web/API：`http://127.0.0.1:8000`
- Swagger：`http://127.0.0.1:8000/docs`
- 存活：`http://127.0.0.1:8000/api/v1/health`
- 就绪：`http://127.0.0.1:8000/api/v1/ready`

### 6. 启动 Web 开发服务

```powershell
cd web
npm install
npm run dev
```

访问 `http://localhost:5173`。生产式本地运行可先执行：

```powershell
cd web
npm install
npm run build
cd ..
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

当 `web/dist` 存在时，FastAPI 会托管构建后的 Web。

### 7. 初始管理员

```dotenv
INITIAL_ADMIN_USERNAME=admin
INITIAL_ADMIN_PASSWORD=
JWT_SECRET_KEY=
```

本地配置为空时会生成随机值并写回 `.env`。实际密码和 JWT 密钥不要提交到仓库。

## 文档、连接器与索引

Web 工作台是推荐入口。管理员或 editor 可以在当前知识库中上传文档、批量删除/重建、查看 Chunk、版本和文档子图，也可以添加本地目录、Git 或 S3/MinIO 数据源。目录、标签和批量移动/打标签的 API 已实现，但当前文档页尚未提供完整编辑控件。

支持的文档格式：

```text
.txt  .pdf  .md  .docx  .html  .htm
```

连接器的 `connection_ref` 只保存引用，不保存原始凭据：

```text
env://RAG_GIT_CREDENTIAL
file://C:/absolute/path/credential.json
```

环境变量或文件内容可以是 JSON 对象；Git 也接受单个 Token 字符串。S3 凭据示例：

```json
{"access_key":"...","secret_key":"...","session_token":"..."}
```

旧的本地默认知识库 CLI 仍可使用：

```powershell
# 只读分类
python -m scripts.ingest_documents --dry-run

# 增量索引或影子重建
python -m scripts.ingest_documents --sync
python -m scripts.ingest_documents --rebuild --yes

# Chroma collection 管理
python -m scripts.ingest_documents --list-collections
python -m scripts.ingest_documents --rollback-collection <collection_name> --yes
python -m scripts.ingest_documents --cleanup-collection <collection_name> --yes

# 图谱增量构建或失败重试
python -m scripts.ingest_documents --graph-only
python -m scripts.ingest_documents --retry-failed
```

多知识库、批量任务、连接器、代码图和图谱治理应优先使用 Web 或 `/api/v1/knowledge-bases/{knowledge_base_id}/...` API。

## 统一 LangGraph 问答

所有问答入口共享同一份编译后的工作流：

```text
rewrite
  → route_request
  → [text_retrieve | graph_retrieve | code_retrieve]  条件并行
  → fuse
  → rerank
  → build_context
  → generate（同步请求）/ END（SSE 上下文准备）
```

- `route_request` 支持显式通道、规则优先和可选 LLM 回退，并解析文档、标签、目录、元数据范围。
- `text_retrieve` 在 Chroma 向量与 SQLite BM25 之间执行多查询加权 RRF。
- `graph_retrieve` 使用词法实体和实体向量匹配，遍历关系并回链 Evidence Chunk。
- `code_retrieve` 搜索活动代码快照中的符号、源码片段和调用/依赖关系。
- 三个通道互不写同一个状态字段；单通道失败会记录状态并允许其余通道继续。
- `fuse` 再次按权重融合并按 `chunk_id` 去重，之后进行重排、来源配额和 Token 预算控制。
- 同步请求由 LangGraph 完成生成；SSE 使用同一个 Graph 准备上下文，再从同一个 Runtime 流式调用回答模型。

## API 概览

完整定义以 Swagger 为准。主要资源组如下：

| 路径组 | 功能 |
| --- | --- |
| `/ask` | 默认知识库兼容问答，仍要求登录 |
| `/api/v1/auth/*` | 登录、退出、当前用户 |
| `/api/v1/conversations/*` | 会话、同步消息、SSE 消息 |
| `/api/v1/knowledge-bases/*` | 知识库、成员、问答和检索调试 |
| `.../{kb_id}/documents/*` | 上传、批量上传、详情、标签、Chunk、版本、恢复和批量操作 |
| `.../{kb_id}/sources/*` | 数据源创建、检查、同步、启停和删除 |
| `.../{kb_id}/index-jobs/*` | 任务、事件、取消和失败项重试 |
| `.../{kb_id}/knowledge-graph/*` | 实体、子图、Schema、构建、质量、复核、合并、社区和实体向量 |
| `.../{kb_id}/documents/{document_id}/subgraph` | 按文档获取知识子图 |
| `.../{kb_id}/code-graph/*` | 代码快照、索引、符号搜索和调用/依赖子图 |
| `/api/v1/health`、`/api/v1/ready` | 存活和 Provider/模型/索引就绪状态 |

## 知识图谱和代码图谱

文档图谱构建会读取当前活动 Schema，恢复异常中断状态，以 Chunk 为输入并行调用抽取模型。候选必须通过实体/关系字典、原文 Evidence、置信度、自环和端点校验，之后才会标准化、去重并写入逻辑图表。抽取缓存、质量报告、人工复核、可逆 Overlay、社区层级和实体向量均按知识库隔离。

代码索引不会永久保存完整 AST。Python 使用 `ast`，其他支持语言优先使用 Tree-sitter；解析结果转换为文件、符号和关系，完成跨文件名称解析后才激活新快照。旧快照按配置保留，用于查看历史和安全切换。

## 企业服务器部署

企业编排文件位于 `deploy/enterprise/`，包括 8 个服务：PostgreSQL、Redis、Chroma、MinIO、MinIO 初始化、API、Worker 和 Beat。

```powershell
Copy-Item deploy/enterprise/.env.example deploy/enterprise/.env
# 编辑 deploy/enterprise/.env，填写密码和模型端点

docker compose `
  --env-file deploy/enterprise/.env `
  -f deploy/enterprise/docker-compose.yml `
  up -d --build
```

服务启动后执行 Provider 和写入检查：

```powershell
docker compose `
  --env-file deploy/enterprise/.env `
  -f deploy/enterprise/docker-compose.yml `
  exec api python -m scripts.validate_enterprise_profile --write-check
```

首次企业验收应使用空数据库和空 Bucket，按“登录 → 新建知识库 → 上传新文档 → 索引 → 可选构图/代码图 → `/ask` 与 Web SSE”完成端到端验证。不要挂载本地 `data/`，本项目当前也不包含历史数据搬迁工具。

## 项目结构

```text
app/
├── adapters/              Metadata / Object / Queue / Cache Adapter
├── api/                   FastAPI 路由、鉴权依赖和错误处理
├── bootstrap/             Provider 容器与 Profile 校验
├── core/                  配置、数据库、迁移启动、安全和日志
├── models/                业务、任务、文档图和代码图 ORM
├── repositories/          按知识库隔离的数据访问
├── schemas/               API DTO
├── services/              问答、索引、连接器、图谱和代码图服务
└── tasks/                 Celery Worker 与 Beat 任务

rag/
├── code_graph/            Parser、临时 AST 结果、符号/关系检索
├── context/               上下文预算、引用和调试导出
├── graph/                 统一编译 LangGraph
├── indexing/              文档切片、增量索引和影子重建
├── knowledge_graph/       抽取、Schema、标准化、存储和检索
├── nodes/                 路由、三路检索、融合、重排、上下文和生成节点
├── providers/             模型、Embedding、Reranker、向量和图 Provider
├── retrievers/            Chroma、BM25、图谱、代码图和 RRF
└── runtime/               每知识库 Runtime 与索引签名缓存

web/src/components/       九个 Web 工作台页面及聊天组件
deploy/enterprise/        企业 Docker Compose 和环境模板
migrations/               Alembic 迁移
scripts/                  索引、部署检查和评估工具
eval/                     评估集和结果
tests/                    自动化测试
data/                     本地运行数据，不进入企业镜像
```

## 测试与评估

后端测试：

```powershell
.\venv\Scripts\python.exe -m pytest -q
```

前端生产检查：

```powershell
cd web
npm run build
```

RAG 评估：

```powershell
python -m scripts.evaluate_rag --mode retrieval --split tuning --label run
python -m scripts.evaluate_rag --mode full --split holdout --label final
python -m scripts.evaluate_rag --compare <baseline.json> <final.json>
```

路由规则评估与可选真实模型路由评估：

```powershell
python -m scripts.evaluate_routing
python -m scripts.evaluate_model_routing --cases eval/routing_model_cases.example.jsonl
```

本地测试通过不等于企业验收完成。上线前仍需执行真实 Provider Contract、多 Worker、浏览器 E2E、性能、安全、备份恢复和故障演练。

## 已知边界

- 尚未完成真实企业 Compose 启动和横向扩缩容验收。
- 尚未完成真实 Git/S3/MinIO 凭据联调和全量图谱/实体向量构建。
- Neo4j、pgvector 和其他厂商数据库需要新增 Adapter 与统一契约测试。
- 静态代码分析不保证解析动态调用、反射、宏或运行时注入；不确定边会保留解析状态。
- 当前不提供本地历史文档、向量、图谱、会话和任务向企业服务器的搬迁。
- Metrics、Tracing、集中告警、容量规划和生产灾难演练仍属于企业运维收尾。
- Redis 目前负责共享缓存和失效消息发布，但 API 端尚未实现订阅消费；分布式 Worker 激活新的代码快照后，API 进程需要通过后续索引签名变化或重启才能确保立即加载新的 Code Retriever。
- 文档页尚缺完整的目录/标签编辑体验；普通聊天页尚缺标签、目录和 metadata 检索范围选择控件，相关后端 API 与检索 DTO 已具备。

## License

Apache License 2.0
