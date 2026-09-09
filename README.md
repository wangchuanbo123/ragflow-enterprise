# Enterprise RAG

Enterprise RAG 是一套可在本地笔记本和企业服务器上运行的多知识库 RAG 系统。项目把业务服务与模型、数据库、向量库、对象存储、缓存和任务队列解耦：本地环境使用轻量组件完成开发，企业环境使用同一套业务代码，通过配置切换到 PostgreSQL、Chroma Server、MinIO、Redis 和 Celery。

系统不仅支持文档问答，还包含文档知识图谱、代码 AST/符号索引、调用图、依赖图、混合检索路由、数据源同步、图谱治理和完整的 Web 管理工作台。

## 当前状态

截至 2026-09-03，核心功能已经完成产品端接入，本地集中验证已经通过：

- 当前本地业务数据已按要求清空，并初始化为 1 个管理员、1 个默认知识库、0 个文档和 0 个 Chunk 的干净基线。
- 当前 Alembic 唯一 Head 为 `f2d6a8c1e4b7`；从旧本地数据库副本升级到 Head 的迁移验证通过。
- Python 全量回归为 218 项测试和 2 个 subtests 全部通过。
- Vue TypeScript 检查和 Vite 生产构建通过。
- Web 增加知识库创建/编辑/归档、空知识库快速开始向导，以及文档“解析—切片—向量—Schema—图谱”处理阶段展示；内部英文标识由系统自动生成，用户只需填写名称和用途说明。
- 问答支持“全部知识库”跨库检索，也可限定指定知识库、文档、目录或标签；跨库结果会统一融合重排，且只包含当前用户有权访问的知识库。回答中的图片与原文代码以结构化附件返回并随消息历史持久保留，界面同时支持停止生成、复制、导出、回答反馈、继续追问与证据原文侧栏。
- 图谱治理按运行概览、Schema、覆盖报告、人工编辑和治理复核分区；代码图谱支持 ZIP、Git、本地目录三种接入、连接检查、构建进度、增量统计、快照差异和失败文件查看。
- 文档知识图谱保留原有按文档浏览能力并命名为“通用文档图谱”，同时提供概念图谱、系统功能图谱、配置项图谱和行业标准图谱；五类图谱各有独立活动 Schema，并共享规范实体、关系和证据。
- 首批文档索引成功后可自动创建“基于文档生成 Schema”的异步任务；结果保留为带来源文档和采样统计的草稿，必须人工审核后激活。
- 图谱治理页已接入快速/标准/深度三档构建、Schema 影响评估、实体/关系向量状态、社区新鲜度和文档级图谱覆盖报告。
- Schema 可以按文件名、目录、标签筛选并手动选择来源文档，可新建版本或覆盖现有草稿；漂移检查只在发现新类型时产生建议草稿。
- 数据库部分唯一索引与激活事务共同保证一个知识库的每类图谱最多只有一个活动 Schema；通用文档、概念、系统功能、配置项和行业标准五类 Schema 分别版本化，互不覆盖。
- Python 使用标准库 AST 提取符号和关系；其他支持语言使用 Tree-sitter 做语法解析与错误恢复检查，当前持久化事实由确定性规则 emitter 提取，并保留降级路径。

当前笔记本版容器已经完成本机健康验证；企业版仍需在目标服务器完成 PostgreSQL、Chroma Server、MinIO、Redis/Celery 和企业模型端点的实机验收。真实 Git/S3 连接器、大规模实体/关系/代码向量构建、压力与故障恢复仍应使用企业数据单独验收。

> 当前可直接配置切换的企业图存储是 PostgreSQL 上的 SQLAlchemy 图表。Neo4j 和 pgvector 尚未提供经过验收的 Adapter，不能仅修改连接串启用。

## 核心功能

| 领域 | 已实现能力 |
| --- | --- |
| 多知识库 | 知识库、成员、文档、Chunk、向量、图谱、代码快照、任务、会话按 `knowledge_base_id` 隔离；支持 owner、editor、viewer 和全局管理员 |
| 文档管理 | 单个/批量上传、目录、标签、元数据、Chunk 查看、不可变版本历史、版本恢复、批量删除和批量重建 |
| 数据源 | 手工上传、本地目录、Git、S3/MinIO；配置检查、同步游标、定时同步、源端删除策略和凭据引用 |
| 索引任务 | 持久化任务与条目、事件、取消、失败项重试、幂等去重、租约、心跳和过期任务恢复 |
| 在线问答 | `/ask`、知识库 `/ask`、Web 同步和 SSE 共用编译后的 LangGraph；回答、来源、结构化图片、原文代码片段、路由理由和通道状态可解释，附件随历史消息持久保留 |
| 检索路由 | 用户显式选择、规则意图路由、可选 LLM 结构化回退；文本、知识图谱、代码图条件路由和并行检索 |
| 检索范围 | 按文档、标签、目录和元数据筛选；范围先解析为当前知识库允许的文档 ID，再下推到各检索通道 |
| 文本检索 | Chroma 向量检索、中文 BM25、多查询召回、加权 RRF、BGE/HTTP/透传重排、上下文 Token 预算和来源配额 |
| 文档知识图谱 | 保留原有按文档浏览与编辑能力并命名为“通用文档图谱”；概念、系统功能、配置项、行业标准各有独立 Schema、关系方向和抽取规则，五类图谱共享规范实体与证据库；支持 LLM 一次合并执行、缓存、Evidence 校验、评分过滤、质量门禁和分层社区 |
| 图谱治理 | 文档/实体子图、实体与关系人工增删改/撤销、复核队列、实体/关系消歧候选、人工合并/拆分 Overlay、实体与关系独立语义向量索引 |
| 图谱检索 | 实体名称/别名词法召回、实体向量召回、关系谓词/中英文别名词法召回、关系向量召回、关系端点种子与 1～2 跳图遍历、Evidence Chunk 回溯 |
| 代码图谱 | 临时 AST、文件级增量解析、符号索引、源码行号、版本化快照、contains/imports/calls/extends 关系、调用图/依赖图查询、代码结构/功能摘要双向量与词法/向量 RRF 召回 |
| 备份与恢复 | 整个知识库与单文档知识图谱版本备份、回退前自动保护、手工备份/回退/删除、每 30 天自动备份和自动版本保留 |
| Web 工作台 | 知识库生命周期与快速开始、文档阶段进度、限定范围问答、回答操作和证据原文；数据源、任务、知识图谱、分区图谱治理、代码接入/版本差异、备份恢复、检索调试和运行状态 |
| 可替换 Provider | 回答/改写/路由/抽取模型、Embedding、Reranker、元数据、向量、图、对象、任务队列和缓存均从业务服务中解耦 |

## 架构文档

- [系统总体说明](docs/系统总体说明.md)：系统总览、服务功能边界、数据流详解、部署迁移、使用指南与运维安全的总纲文档（另有 [HTML 版](docs/系统总体说明.html)）。
- [RAG 端到端架构 PlantUML](docs/RAG架构图.puml)：文档/图谱/代码构建，以及提问、路由、三路检索、召回、融合、重排和生成链路。
- [项目文档导航](docs/README.md)
- [代码阅读指南](docs/代码阅读指南.md)

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
GRAPH_AUTO_BUILD_ENABLED=true
GRAPH_SCHEMA_AUTO_GENERATE_ENABLED=true
```

`GRAPH_AUTO_BUILD_ENABLED=true` 是默认值。文档索引成功并产生待构图 Chunk 后，系统会合并短时间内的上传并按快速策略增量构建。`GRAPH_SCHEMA_AUTO_GENERATE_ENABLED=true` 会在知识库首次成功解析文档后自动创建文档归纳任务；生成的 Schema 只是草稿，不会绕过人工审核直接替换活动版本。两项设置都可以在“图谱治理”页面按知识库开关。

自动 Schema 默认最多采样 30 份文档、每份 3 个分布位置的 Chunk、合计 40000 字符：

```dotenv
GRAPH_SCHEMA_SAMPLE_DOCUMENTS=30
GRAPH_SCHEMA_SAMPLE_CHUNKS_PER_DOCUMENT=3
GRAPH_SCHEMA_SAMPLE_MAX_CHARS=40000
```

关系和代码向量召回默认开启，常用参数如下：

```dotenv
# 图谱实体召回
GRAPH_ENTITY_VECTOR_ENABLED=true
GRAPH_ENTITY_SEARCH_K=5

# 图谱关系召回
GRAPH_RELATION_VECTOR_ENABLED=true
GRAPH_RELATION_SEARCH_K=8
GRAPH_MAX_HOPS=2
GRAPH_MAX_RELATIONS=50

# 代码 Symbol 召回
CODE_VECTOR_ENABLED=true
CODE_SEARCH_K=12
CODE_VECTOR_SEARCH_K=12
CODE_VECTOR_MAX_CHARS=6000
CODE_FUNCTION_VECTOR_ENABLED=true
CODE_FUNCTION_SUMMARY_LLM_ENABLED=true
CODE_FUNCTION_SUMMARY_BATCH_SIZE=8
CODE_FUNCTION_SUMMARY_MAX_SOURCE_CHARS=3000
CODE_FUNCTION_SUMMARY_MAX_CHARS=800
CODE_FUNCTION_SUMMARY_RELATION_LIMIT=12
```

文档、实体、关系和代码 Symbol 分别使用独立的版本化 Chroma Collection，切换新索引状态后才清理旧 Collection。向量服务异常时，在线检索会降级到仍可用的词法召回和图遍历，不会使整个问答请求失败。

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

如果网关提供 Anthropic Messages `POST /v1/messages` 协议：

```dotenv
LLM_PROVIDER=anthropic_compatible
LLM_MODEL=your-model
LLM_BASE_URL=https://model-gateway.example/v1/messages
LLM_API_KEY=your-api-key
```

远程 Embedding 与 Reranker 可独立配置：

```dotenv
EMBEDDING_PROVIDER=openai_compatible
EMBEDDING_MODEL=your-embedding-model
EMBEDDING_BASE_URL=https://embedding-gateway.example/v1
EMBEDDING_API_KEY=your-api-key

RERANKER_PROVIDER=http
RERANKER_MODEL=your-reranker-model
RERANKER_BASE_URL=https://reranker-gateway.example/v1
RERANKER_API_KEY=your-api-key
```

`RERANKER_PROVIDER=bge` 表示由应用进程使用 Sentence Transformers 加载本地
CrossEncoder，并不是从 Ollama 获取；`http` 才表示调用远程 `/v1/rerank`。

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

也可以在启动前手工升级数据库：

```powershell
.\venv\Scripts\python.exe -m alembic upgrade head
```

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

# 图谱默认在文档解析完成后自动增量构建；以下命令用于手动构建或失败重试
python -m scripts.ingest_documents --graph-only
python -m scripts.ingest_documents --retry-failed
```

多知识库、批量任务、连接器、代码图和图谱治理应优先使用 Web 或 `/api/v1/knowledge-bases/{knowledge_base_id}/...` API。

## 端到端 RAG 架构

系统将离线构建和在线问答分开，文档、图谱和代码使用独立的逻辑存储与向量索引：

```text
离线构建
  文档 → 解析 → Chunk → 文档库 ─┬→ 文档向量索引
                                 ├→ BM25 索引
                                 └→ 自动构图任务 → 活动 Schema 约束 LLM 抽取
                                                   → 消歧候选/人工合并 → 图谱库 ─┬→ 实体向量索引
                                                            └→ 关系向量索引

  代码 → 文件哈希差异分类 → 仅解析新增/修改文件 → 复用未变化 Symbol/Relation
       → 全局关系重解析 → 原子切换代码快照 → 功能摘要生成/复用
       → 增量更新代码结构向量 + 功能摘要向量

在线问答
  提问 → 权限与限制层 → 检索路由 → 按置信度/多轮状态决定是否 Query Rewrite
       → 生成一次共享问题向量
       → [文档通道 | 图谱通道 | 代码通道] 条件并行
       → 通道内召回融合 → 跨通道融合 → Rerank
       → Context Token Budget → LLM 生成 → 引用与答案校验
```

三条业务检索通道下面可以包含多种底层召回方式：

| 业务通道 | 底层召回 | 通道内处理 |
| --- | --- | --- |
| 文档通道 | Chroma 向量、BM25、多查询召回 | 共享问题向量、加权 RRF、范围过滤、Chunk 去重 |
| 图谱通道 | 实体词法、实体向量、关系词法、关系向量、图遍历 | 合并实体/关系种子，将关系作为 0 跳种子，以端点扩展 1～2 跳并回溯 Evidence Chunk |
| 代码通道 | Symbol 名称/限定名/签名词法召回、代码结构向量、功能摘要向量 | 多向量按 Symbol 去重融合，再与词法结果 RRF 融合并扩展调用与依赖关系 |

当前代码图谱与文档知识保持独立：没有建立 `Code Symbol → Document Chunk` 显式关联，也不会进行代码—文本跨索引扩展。该能力留待后续单独设计和验收。

## 统一 LangGraph 问答

所有问答入口共享同一份编译后的工作流：

```text
route_request
  → rewrite（仅低路由置信度或多轮问题）
  → route_after_rewrite（改写成功后仅用规则复核意图）
  → embed_query（只生成一次并供全部向量索引复用）
  → [text_retrieve | graph_retrieve | code_retrieve]  条件并行
  → fuse
  → rerank
  → build_context
  → generate（同步请求）/ END（SSE 上下文准备）
```

- `route_request` 支持显式通道、规则优先和可选 LLM 回退，并解析文档、标签、目录、元数据范围。
- `retrieval.strategy` 支持 `fast`、`standard`、`deep`；三档分别控制召回数量、图遍历深度、代码候选、重排数量以及上下文预算，默认 `standard`。
- `rewrite` 仅在路由置信度低于 `QUERY_REWRITE_CONFIDENCE_THRESHOLD`（默认 `0.75`）或存在历史消息时调用 LLM；首轮高置信度问题不会增加改写模型调用。
- `embed_query` 对最终语义查询只生成一次向量，文档、实体、关系和代码向量索引统一使用 `similarity_search_by_vector`，词法检索仍可同时利用原始问题和改写问题。
- `text_retrieve` 在 Chroma 向量与 SQLite BM25 之间执行多查询加权 RRF。
- `graph_retrieve` 并行执行实体词法/向量召回和关系词法/向量召回；关系候选直接作为 0 跳事实，随后从实体与关系端点继续遍历并回链 Evidence Chunk。
- `code_retrieve` 对活动代码快照执行 Symbol 词法/向量 RRF 召回，再补充源码片段及 calls/imports/extends 等调用和依赖事实。
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
| `.../{kb_id}/knowledge-graph/*` | 自动构图/自动 Schema 设置、文档生成 Schema、影响评估、三档构建、文档覆盖报告、实体、子图、质量、人工校正、实体/关系消歧合并、社区以及实体/关系向量状态与重建 |
| `.../{kb_id}/documents/{document_id}/subgraph` | 按文档获取知识子图 |
| `.../{kb_id}/code-graph/*` | 代码快照、索引、词法/向量符号搜索、调用/依赖子图以及代码向量状态与重建 |
| `.../{kb_id}/backups/*` | 整库/文档图谱备份、版本列表、回退和删除；`backup-settings` 返回自动备份策略 |
| `/api/v1/health`、`/api/v1/ready` | 存活和 Provider/模型/索引就绪状态 |

## 知识图谱和代码图谱

文档索引任务成功后会按知识库设置自动创建去重的 Schema 归纳任务和增量构图任务。Schema 归纳对已解析文档进行分层采样，让抽取模型输出稳定的实体类型、关系类型和领域约束，并记录来源文档 ID、采样 Chunk 数和归纳理由。管理员也可以按文件名、目录和标签筛选后手动勾选来源文档，选择生成新草稿或覆盖现有草稿。生成结果始终是草稿；管理员可以编辑、查看激活影响范围，再按自动、增量或全量策略激活。构图读取当前活动 Schema，把实体类型、关系字典和领域规则写入 LLM Prompt，并使用同一 Schema 校验模型输出。

每个知识库为 `general`（通用文档）、`concept`（概念）、`functional`（系统功能）、`configuration`（配置项）和 `standard`（行业标准）分别维护一个活动 Schema。激活某类新版本时，只会把该类旧活动版本置为 `superseded`，不会覆盖其他图谱的 Schema；数据库部分唯一索引会拒绝同类并发产生第二个活动版本。构图时系统把五套活动约束合并为一次 LLM 抽取请求，再写入共享的规范实体、关系和证据库，避免重复抽取与重复实体。Schema 漂移检查也只与当前图谱类型的活动版本比较。

图谱治理页提供“处理完成率”和“事实覆盖率”两套文档指标。前者统计每个文档已完成或已失败的构图 Chunk，后者统计实际产生实体提及或关系 Evidence 的 Chunk；无图谱事实的说明性文档会显示 `no_facts`，不会被误判成构建失败。报告同时展示实体数、提及数、关系数、证据数、Schema 版本和错误摘要。

Web“图谱治理”页面可以查看完整 Schema 内容，创建或修改草稿、复制活动版本并激活；也可以搜索实体加载两跳子图，修改实体名称/描述，新增、修改或隐藏关系。实体向量和实体合并结果会产生消歧候选；实体合并后落到同一三元组的关系会产生关系合并候选。人工实体合并、关系合并和图谱校正均保留审计记录，可以拆分或回退；被合并关系的 Evidence 会汇聚到保留关系。

关系词法召回使用标准谓词及其中英文别名识别问题中的关系意图；关系向量文档包含主语、谓词、宾语、关系描述和最多三条原文证据。词法与向量关系候选合并后直接作为 0 跳关系事实，同时把主客体加入图遍历种子，最终通过 `kg_relation_evidence` 回溯到可引用的文档 Chunk。

代码索引不会永久保存完整 AST。Python 使用 `ast`，其他支持语言优先使用 Tree-sitter；解析结果转换为文件、符号和关系。同步时按相对路径和 SHA-256 对新旧代码清单分类：新增或修改文件重新解析，未变化文件直接复用上一快照的持久化文件、Symbol 和 Relation 事实，删除文件不进入新快照；随后对全部关系端点重新解析并原子激活新快照。Parser 版本变化或数据源变化时自动退化为全量解析。

每个 Symbol 默认生成两类向量：结构向量包含语言、路径、类型、限定名、签名和源码片段；功能摘要向量包含由抽取模型根据源码与调用关系生成的中文业务功能摘要。摘要连同来源指纹、模型指纹和生成方式保存在 Symbol metadata 中；未变化摘要直接跨快照复用，只对源码、关系或摘要模型发生变化的 Symbol 重新生成。模型不可用或输出无效时使用确定性摘要降级，并在后续构建中允许重试模型摘要。

两类向量都使用由 `vector_kind + stable_key` 派生的稳定向量 ID。新快照激活后，仅删除已消失 Symbol 的向量、重算内容变化的向量，并保留未变化向量；Embedding 配置变化、旧索引格式或增量基线不可用时自动执行影子全量重建。检索时结构与功能摘要命中先按 Symbol 去重融合，并返回 `vector_match_types` 解释命中类型。Git/本地目录连接器同步代码图后会自动刷新代码向量，旧快照仍按配置保留，用于查看历史和安全切换。

功能摘要默认复用 `extraction_llm`。如果企业代码不允许发送到配置的模型端点，可设置 `CODE_FUNCTION_SUMMARY_LLM_ENABLED=false`；系统仍会用 Symbol、签名和调用关系生成确定性摘要向量，但业务语义表达能力会弱于模型摘要。

使用者在 Web“代码图谱”页面首次接入时，只需选择 Git 仓库或服务可见目录并填写地址。系统自动创建内部数据源 ID；Git 分支可以省略，Commit SHA 在拉取后自动识别并记录到代码快照，不要求使用者提前查询。Git 地址同时适用于笔记本和服务器；本地目录始终指运行服务的机器可访问的路径，容器部署时需要先挂载该目录。构建、符号检索、调用子图和快照历史都在同一页面管理。

图谱构建任务默认同时重建实体和关系向量；代码索引任务和启用代码索引的 Git/本地目录同步默认增量刷新活动 Snapshot 的代码向量。也可以独立查询或触发重建：

```text
GET  /api/v1/knowledge-bases/{kb_id}/knowledge-graph/relation-vector
POST /api/v1/knowledge-bases/{kb_id}/knowledge-graph/relation-vector/rebuild

GET  /api/v1/knowledge-bases/{kb_id}/code-graph/vector
POST /api/v1/knowledge-bases/{kb_id}/code-graph/vector/rebuild
```

## 备份与恢复

Web“备份与恢复”页面提供两个范围：整个知识库备份包含文档对象、Chunk、Schema、文档图谱、人工 Overlay/合并记录、数据源配置引用和代码图谱；单文档图谱备份只包含所选文档涉及的实体、关系、证据和人工修订。回退会先自动创建一份“回退前保护”版本，再恢复数据并排队重建文档、实体、关系和代码向量，同时把图社区标记为待重建。

默认每 30 天为每个活动知识库创建一次整库备份，自动版本保留 12 个；手工版本和回退前保护版本不会被定时清理，只能在页面中手工删除。备份内容存入当前 Object Store，本地版使用持久化数据卷，企业版使用 MinIO/S3。可通过以下配置调整：

```dotenv
BACKUP_MONTHLY_ENABLED=true
BACKUP_MONTHLY_INTERVAL_DAYS=30
BACKUP_RETENTION_VERSIONS=12
BACKUP_SCHEDULER_INTERVAL_SECONDS=86400
```

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

web/src/components/       十个 Web 工作台页面及聊天组件
deploy/enterprise/        企业 Docker Compose 和环境模板
migrations/               Alembic 迁移
scripts/                  索引、部署检查和评估工具
eval/                     评估集和结果
tests/                    自动化测试
data/                     本地运行数据，不进入企业镜像
docs/                     项目开发文档
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
- 尚未完成真实 Git/S3/MinIO 凭据联调以及生产语料的全量图谱、实体向量、关系向量和代码向量构建。
- Neo4j、pgvector 和其他厂商数据库需要新增 Adapter 与统一契约测试。
- 静态代码分析不保证解析动态调用、反射、宏或运行时注入；不确定边会保留解析状态。
- 当前没有建立代码 Symbol 与文档 Chunk 的显式关联；代码检索结果和文档检索结果只在跨通道融合阶段共同参与排序，不进行跨库关系扩展。
- 当前不提供本地历史文档、向量、图谱、会话和任务向企业服务器的搬迁。
- Metrics、Tracing、集中告警、容量规划和生产灾难演练仍属于企业运维收尾。
- Redis 目前负责共享缓存和失效消息发布，但 API 端尚未实现订阅消费；分布式 Worker 激活新的代码快照后，API 进程需要通过后续索引签名变化或重启才能确保立即加载新的 Code Retriever。
- 文档页尚缺完整的目录/标签编辑体验；普通聊天页尚缺标签、目录和 metadata 检索范围选择控件，相关后端 API 与检索 DTO 已具备。

## License

Apache License 2.0
