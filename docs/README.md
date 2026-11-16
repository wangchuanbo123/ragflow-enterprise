# 团队知识库与 RAG 工作台

用于团队集中管理文档、检索问答、知识图谱和代码知识的应用。前端为 Vue，后端为 FastAPI；业务、图谱与向量使用 PostgreSQL + pgvector，Redis 提供共享缓存和运行协调。

本机与服务器使用同一套 Docker 部署：API、Worker、Scheduler、PostgreSQL、Redis，共五个容器。API、Worker、Scheduler 共用一个应用镜像，分工见[五容器架构与职责说明](五容器架构与职责说明.md)。

本文命令均在**项目根目录**执行；`cd web` 后的前端命令除外。正文中的配置、源码和数据路径默认相对项目根目录。

## 1. 功能与入口

| 功能 | 可以做什么 | 网页入口 |
| --- | --- | --- |
| 知识库与权限 | 创建知识库，设置 owner、editor、viewer 成员权限 | 顶部“管理”；数据源与成员 |
| 文档管理 | 上传 TXT、PDF、Markdown、DOCX、HTML，管理原文、版本、目录和标签 | 文档管理 |
| 检索与问答 | 向量与中文 BM25 混合检索、多轮对话、流式回答、来源引用、反馈 | 知识问答、全文搜索、检索调试 |
| 后台处理 | 查看解析、切片、向量和图谱处理阶段，取消或重试失败任务 | 索引任务 |
| 知识图谱 | 查看实体、关系和证据，用口语原则过滤并保存个人视图 | 知识图谱 |
| 图谱治理 | 生成与激活 Schema、构图、人工修订、合并复核和派生索引维护 | 图谱治理 |
| 代码知识 | 接入 Git、本地目录或 ZIP，检索符号与调用依赖，比较代码快照 | 代码图谱 |
| 数据源同步 | 配置本地目录、HTTP(S) Git、S3，手动或定时同步文档 | 数据源与成员 |
| 知识智能体 | 知识抽取、接入编排、结构治理、质量修复、增量维护、知识组织、评测、业务分析、运行诊断 | 知识智能体 |
| 版本与恢复 | 创建知识库或单文档图谱备份、回退版本 | 备份与恢复 |
| 账号与监控 | 用户管理、修改密码、检查组件与模型、任务和资源指标 | 用户管理、账号设置、运行状态 |

管理员管理全局账号和知识库；知识库 owner、editor 可维护内容，viewer 以读取和问答为主。网页根据权限显示可用操作。

## 2. 首次部署与启动

### 准备环境

- 安装 Docker Engine 和 Compose V2。Windows 使用 Docker Desktop 的 Linux 容器。
- 宿主机安装 Python 3.11 或更高版本，用于配置、启动与备份；应用依赖在镜像中安装。
- 准备容器可访问的生成模型和 Embedding 服务，两者均需配置。生成用于回答和抽取，Embedding 用于向量入库与检索。
- 首次构建需要联网下载基础镜像、依赖及固定的 OpenCode 源码。镜像会构建项目自己的 OpenCode 2.2.5，无须预装全局 OpenCode。

### 生成并填写服务器配置

```bash
python -m scripts.configure_server
```

命令创建私有 `.env.server`，自动生成数据库、Redis、JWT 和初始管理员配置；已有文件会保留。首次生成也会继承旧 `.env` 中的模型及登录配置。

用本机编辑器打开 `.env.server`，核对以下配置。下列值是占位示例，需替换为实际服务信息：

```dotenv
LLM_PROVIDER=openai_compatible
LLM_MODEL=你的生成模型名称
LLM_BASE_URL=https://你的生成服务地址/v1
LLM_API_KEY=你的生成模型密钥

EMBEDDING_PROVIDER=openai_compatible
EMBEDDING_MODEL=你的向量模型名称
EMBEDDING_BASE_URL=https://你的向量服务地址/v1
EMBEDDING_API_KEY=你的向量模型密钥

RERANKER_PROVIDER=none
APP_BIND_ADDRESS=127.0.0.1
APP_PORT=8080
```

完整选项见[服务器配置示例](../deploy/server.env.example)。重排默认关闭；需要时可配置 HTTP 重排服务或本地 BGE，并准备服务或模型资源。已有索引应继续使用原 Embedding 模型，更换模型后需要重建对应向量索引。

索引会核对模型地址、模型版本、Embedding 前缀以及解析和切片配置的指纹。远端服务用同一个模型名称更换权重时，必须更新 `EMBEDDING_REVISION`。旧的非空索引缺少完整指纹或配置发生变化时，系统会要求从原文件创建新索引；在“文档管理”执行全库重建，确认新索引发布后再问答，保留旧版本供回退，不能把新旧向量混写。

所有者/编辑者可在“文档管理”点击“重建全库索引”，确认后在任务中心跟踪。管理员也可在容器中先检查原件，再提交：

```bash
docker compose --env-file .env.server -f deploy/compose.yml exec -T api python -m scripts.rebuild_index --knowledge-base <ID> --admin-username <管理员>
docker compose --env-file .env.server -f deploy/compose.yml exec -T api python -m scripts.rebuild_index --knowledge-base <ID> --admin-username <管理员> --apply
```

命令只处理所选库登记的当前原件，不扫描其他库或历史备份。Worker 创建影子集合，任一原件缺失、解析失败、取消或校验失败均不切换活动索引；新集合全部通过后原子发布，并保留旧集合。重建会重新请求 Embedding 服务，需要预留存储、模型额度和处理时间。

如果模型在宿主机运行，容器内的 `localhost` 指向容器自身，应使用容器可达地址，例如 `http://host.docker.internal:11434`。首次生成配置会转换旧的回环地址，已有 `.env.server` 需手动核对；宿主模型服务也需监听容器可达的接口。

### 启动服务并登录

```bash
python -m scripts.start_server --no-browser
```

Windows 也可双击根目录 `start.cmd`。启动器构建应用镜像，启动基础服务和应用，再检查就绪状态。关闭命令窗口后容器继续运行。

默认地址：`http://127.0.0.1:8080`。新部署使用 `.env.server` 中的 `INITIAL_ADMIN_USERNAME`（默认 `admin`）与 `INITIAL_ADMIN_PASSWORD` 登录，密码不打印到启动日志。迁移已有数据库时使用原账号密码。

| 启动参数 | 用途 |
| --- | --- |
| `--no-browser` | 不自动打开浏览器，适合服务器 |
| `--skip-build` | 使用已有应用镜像启动 |
| `--no-legacy-import` | 新建空实例时跳过旧本地数据导入 |

默认启动会检查旧的 `data/app.db`、活动 Chroma 向量和原文件，必要时迁入空的服务器存储，逐项校验并保留源数据。迁移期间应停止旧应用；导入失败时先排查原因，不要清空数据库或删除数据卷重试。

### 团队访问

内网直接访问时设置 `APP_BIND_ADDRESS=0.0.0.0`，通过服务器地址和 `APP_PORT` 访问。对外服务应使用 HTTPS 反向代理，设置 `COOKIE_SECURE=true` 和实际前端来源 `CORS_ORIGINS`。配置修改后按第 4 节重新创建应用容器。PostgreSQL、Redis 和内部 OpenCode 无须向公网开放端口。

## 3. 网页操作流程

建议先完成“登录 → 验证模型 → 建知识库 → 上传文档 → 等待索引完成 → 提问 → 核对引用”，再批量导入资料。

### 3.1 检查模型与准备账号

1. 管理员打开“运行状态”，确认数据库、Redis、Worker、Scheduler 和 OpenCode 等组件正常。
2. 点击“验证模型连接”，检查生成、Embedding 及已配置重排服务的真实请求。组件就绪和配置通过并不代表模型推理成功。
3. 在“用户管理”创建账号、调整角色或重置密码；成员在“账号设置”修改自己的密码。
4. 初始密码配置只用于创建新账号。账号创建后，再修改 `INITIAL_ADMIN_PASSWORD` 不会改变数据库中的密码；改密、停用或调整角色会撤销用户已有会话。

### 3.2 知识库、上传与任务

上传完成后先核对接受数和每个文件的拒收原因，再从任务入口查看解析结果。剪贴板只接收受支持的文档文件，PNG/JPG 截图不能作为文档直接上传。PDF 解析预览会标记文字稀疏且含图片的页面；核对后可按建议页或全页重新执行 OCR。单次最多 40 页，复杂扫描件建议先拆分；再次按建议页解析会保留之前选择的 OCR 页面。

1. 点击顶部“当前知识库”旁的“管理”，由管理员创建知识库；已有库可直接选择。
2. 在“数据源与成员”配置成员权限。每次操作前确认顶部选中的知识库。
3. 在“文档管理”选择文件上传，按需创建目录、设置标签、查看原文和版本。
4. 在“索引任务”查看排队、处理阶段和失败原因。上传成功只代表文件已接收，索引任务完成后才能检索新内容；失败时先处理具体原因，再重试。

默认单文件上限 50 MB，每批最多 100 个文件、总量 500 MB。扫描 PDF 对空文本页执行中英文本地 OCR，每份文档最多 40 个 OCR 页，并受像素和超时预算限制；大扫描件需拆分上传。文本 PDF 页数不受这个 OCR 页数上限约束。

### 3.3 搜索与问答

选择“文档”“目录”或“标签”范围后，必须完成具体选择才能发送；界面和接口都会校验。回答展示检索通道降级、证据不足和引用编号校验状态。没有证据时直接拒答；引用编号校验只检查来源编号是否存在，不等于证明答案事实正确。PDF 来源以第 1 页起显示，点击引用可直接定位对应片段；长文片段可继续分页加载。

1. 在“知识问答”新建或选择会话，输入问题。
2. 默认针对当前知识库；需要跨库或限定文档时，明确选择回答范围。
3. 流式回答可随时停止，已生成部分会保留。
4. 点击来源引用核对原文、片段和页码，并提交回答反馈。

查找具体词语和文档可用“全文搜索”；排查召回范围、通道和重排结果用“检索调试”。模型答案与图谱抽取结果都应结合证据核验。

### 3.4 知识图谱与治理

1. 在“图谱治理 → Schema 管理”中选择样本文档生成草稿，或手动填写实体类型、关系类型和抽取规则。
2. 评估影响并激活合适的 Schema；一个知识库共用一个活动版本。
3. 按需要运行快速、标准或深度构建，在“索引任务”跟踪进度。
4. 在“知识图谱”查看实体、关系与原文证据；在“图谱治理”处理人工修订、重复实体和关系。
5. 在“用口语描述你想看的图”中添加原则，勾选并“应用原则”，例如“只显示有原文证据的关系”“围绕订单系统看两跳依赖”。可保存为自己的命名视图。

多条原则共同生效，清除原则可恢复全库视图。过滤基于已有事实，不产生新事实或自动重抽文档；结果为空时检查原则、名称及图谱是否已经构建。

### 3.5 数据源同步与代码图谱

**同步文档：**在“数据源与成员”添加本地目录、Git 或 S3，填写位置、授权凭据、同步间隔和源端删除策略；保存后先“检查配置”，再“同步”。索引进度在“索引任务”查看，大数据源可能分批完成。

**分析代码：**在“代码图谱”展开“首次接入代码仓库”，上传 ZIP，或填写 Git 仓库/已授权本地目录并“保存并构建代码图谱”。已有数据源可直接选择并“创建索引任务”。完成后搜索类、函数、方法，查看源码行号、调用依赖与快照差异。

连接器需先完成服务器授权：

- 本地来源必须挂载进读取它的应用容器，并同时登记 `LOCAL_SOURCE_ALLOWED_ROOTS` 和 `LOCAL_SOURCE_ROOTS_JSON`；网页填写容器内路径。例如挂载为 `/sources/team`，为真实知识库登记 `{"知识库ID":["/sources/team"]}`。内部对象存储目录不能作为普通目录源。
- 公开 Git 可不选凭据；私有 Git 使用登记的 HTTPS token。支持 HTTP(S) 仓库，不支持 SSH 或 `git@...` 地址。
- 私有 Git 和 S3 凭据通过 `.env.server` 的 `CONNECTOR_CREDENTIALS_JSON` 登记，每项指定 `secret_ref`、`knowledge_base_ids`、`source_type`、`allowed_locations`；S3 自定义服务还需匹配 `endpoint_url`、`region`。
- 网页仅选择 `credential://登记ID`；`secret_ref` 中的 `env://变量名` 或 `file://绝对路径` 仅供服务器使用，密钥文件也需挂载且可读。不要把密钥写入数据位置或普通配置。
- 修改挂载或环境配置后重新创建相关容器，再检查同步。源端删除策略默认保留文档，启用同步删除前先核对同步范围和结果。

### 3.6 知识智能体

1. 打开“知识智能体”，选择任务类型、目标和文档范围（全库、文档、目录或标签）。
2. 按需设置执行配置、评测问题等，先使用默认预览检查计划和结果。
3. 需要写入知识库时，勾选允许的具体动作并设置动作上限，再提交任务。
4. 查看执行事件、证据、用量、已应用动作及关联索引任务；必要时取消、重试或导出报告。

知识结构治理生成的 Schema 草稿仍需在“图谱治理”评估和激活。自动生成的评测题属于候选，人工审阅后再作为业务金标准。

### 3.7 知识库版本回退

在“备份与恢复”选择整个知识库或单文档图谱，创建备份。回退时选择版本并确认，系统先生成回退前保护备份，再恢复数据。受影响的检索索引会重建，需要可用的 Embedding 服务；完成后检查文档、图谱及问答。

应用内备份用于知识库版本回退。服务器换机和灾难恢复使用第 5 节的完整服务器备份。

## 4. 服务管理与升级

以下命令使用[Compose 配置](../deploy/compose.yml)。

```bash
# 查看服务状态
docker compose --env-file .env.server -f deploy/compose.yml ps
# 查看应用日志
docker compose --env-file .env.server -f deploy/compose.yml logs --tail=100 api worker scheduler
# 停止服务
docker compose --env-file .env.server -f deploy/compose.yml stop
# 启动已有服务
docker compose --env-file .env.server -f deploy/compose.yml up -d --wait
# 修改配置后重新创建应用容器
docker compose --env-file .env.server -f deploy/compose.yml up -d --force-recreate api worker scheduler
```

修改 PostgreSQL、Redis 自身配置时，也需按变更更新对应服务；已有数据库和 Redis 的密码不能仅修改环境变量，必须同步变更实际服务凭据。

升级前先做完整服务器备份，更新代码后运行 `python -m scripts.start_server --no-browser`，构建新镜像并执行数据库迁移。不要使用 `docker compose down -v` 处理故障，该命令会删除持久卷。

### 使用已发布镜像

CI 先执行源码回归，再构建实际生产镜像，检查依赖、运行数据库迁移及登录等烟测，通过后发布 `ghcr.io/<组织>/<仓库>:sha-<完整提交号>`。在 `.env.server` 把 `APP_IMAGE` 设置为该版本或 `@sha256:<摘要>`，再执行：

```bash
docker compose --env-file .env.server -f deploy/compose.yml pull api worker scheduler
python -m scripts.start_server --skip-build --no-legacy-import --no-browser
```

三个角色始终使用同一版本。回退时将 `APP_IMAGE` 改回已验证版本再拉取和启动；如果发布包含不兼容数据库迁移，使用升级前的完整备份恢复到对应版本，不能只回退镜像。基础镜像固定摘要；更新摘要和 Python 依赖后需重新通过发布流程。镜像策略参考 [Docker 构建建议](https://docs.docker.com/build/building/best-practices/)。

生产默认只安装外部模型服务与原生文档解析所需依赖。`INSTALL_LEGACY=true` 仅用于旧 Chroma 数据迁移，迁移成功后启动器重建不含旧依赖的常驻镜像；旧迁移组件不宜用于开放网络服务。使用进程内 BGE 重排时构建增加 `--build-arg INSTALL_LOCAL_MODELS=true`，并准备 CPU 模型权重和内存。模型接口重排不需要这组依赖。

运行诊断与账号恢复：

```bash
docker compose --env-file .env.server -f deploy/compose.yml exec -T api python -m scripts.check_operations
docker compose --env-file .env.server -f deploy/compose.yml exec api python -m scripts.manage_users reset-password --username admin
```

诊断存在告警时返回非零状态，请查看具体内容。恢复命令交互读取新密码，不通过命令传明文；用户名不是 `admin` 时替换为实际用户名。

`/api/v1/health` 检查进程存活，`/api/v1/ready` 检查组件连接、结构与心跳，不遍历全部片段。管理员可显式调用 `POST /api/v1/operations/index-audit` 分批核对全部索引；索引发布期间审计结果可能变化，待任务结束后复核。真实模型请求通过网页“验证模型连接”检查；管理员在“运行状态”查看任务等待、模型耗时、数据库和资源指标。指标只在内存累积，后台每秒批量上报 Redis，故障时限时重试，不在业务请求中同步写监控。

## 5. 完整服务器备份与恢复

在宿主机执行，下列路径需替换为实际备份位置：

```bash
python -m scripts.server_backup backup --output /mounted-backups/rag
python -m scripts.server_backup restore --backup /mounted-backups/rag/server-时间戳-标识
```

Windows 可将路径换成例如 `D:/rag-backups`。完整备份包含 PostgreSQL 业务数据和四类向量、原文件、历史版本、图片、应用内备份及服务器配置。捕获数据库和原文件快照时暂停应用服务；先恢复此前运行的服务，再压缩原文件、计算校验和与完整验证。捕获期间仍有维护窗口，需要为未压缩快照和压缩产物预留空间。命令结果和运行状态记录维护、备份及恢复耗时；恢复前校验并创建保护备份，失败时自动恢复保护备份，完成后核对账号、权限、文档及问答。

该备份采用一致性快照，不提供 PostgreSQL WAL 连续归档或任意时间点恢复。需要更小 RPO 时，须同时设计 WAL、基准备份及与文档版本一致的对象存储快照，并完成恢复演练；仅启用数据库 WAL 无法恢复同步时点的原文件。

备份包含密钥，需限制访问并加密保存到异机。原文件在 `documents` 持久卷，数据库和向量在 `postgres` 持久卷；仅复制主机 `data/` 无法备份完整服务器。

每日备份：

1. 在 `.env.server` 按需设置 `SERVER_BACKUP_DIRECTORY`（本地目录）、`SERVER_BACKUP_MIRROR_DIRECTORY`（已挂载的异机/NAS 目录）和备份间隔。
2. 在宿主机执行一次备份检查，再安装计划任务：

```bash
python -m scripts.backup_schedule --once
python -m scripts.install_backup_schedule
```

Windows 默认注册当前登录用户每日 03:00 的任务，需要该用户会话及 Docker Desktop 可用；Linux 生成 `data/backup-schedule/` 下的 systemd 模板，需管理员安装启用。未配置真实异机挂载时只有本机备份。恢复点默认目标为 24 小时，实际恢复耗时需通过演练确认。

## 6. 常见问题与容量边界

| 现象 | 处理方式 |
| --- | --- |
| 页面打不开 | 检查 Docker、五服务状态、绑定地址、端口和反向代理，查看 API 日志 |
| 组件正常但无法问答或入库 | 验证真实模型连接，核对模型名称、地址、密钥、余额和限流；402/503 需处理上游服务问题 |
| 上传后搜索不到 | 检查索引任务和向量阶段错误，核对知识库、文档范围及权限 |
| 任务一直排队 | 检查 Worker 心跳、运行任务、同库写入锁和模型额度 |
| 提示系统繁忙 | 默认完整 RAG 并发 8、等待配额 30 秒；生成模型总额度 8、后台最多占 2。先检查耗时和模型容量，再调整配置 |
| 图谱或过滤结果为空 | 检查 Schema、构图任务、证据和筛选原则；任务完成不代表原文必然存在可抽取关系 |
| 本地目录或凭据被拒绝 | 核对容器挂载、真实知识库 ID、授权根目录及凭据允许的目标 |
| 扫描 PDF 失败 | 按提示拆分文档，检查 OCR 页数、像素和时间限制 |
| 修改初始密码仍无法登录 | 初始配置不修改现有账号，通过用户管理或账号恢复重置 |

容量取决于片段数、向量维度、图谱规模、硬件和模型吞吐。默认每进程缓存最多 8 个知识库的 BM25 索引，每库最多 10 万个活动片段；超限时关键词通道失败，向量通道仍可能工作。默认向量搜索为精确模式；HNSW 需维护建索引并在真实语料上通过召回与延迟评测后启用。

BM25 构建失败后会退避，避免每个请求重复全量构建；界面显示降级原因。管理员在已配置的 API 容器内执行维护：

```bash
docker compose --env-file .env.server -f deploy/compose.yml exec -T api python -m scripts.maintain_search hnsw --knowledge-base <ID> --collection <活动集合> --admin-username <管理员> --apply
docker compose --env-file .env.server -f deploy/compose.yml exec -T api python -m scripts.maintain_search validate-hnsw --knowledge-base <ID> --collection <活动集合> --admin-username <管理员> --apply
```

只有采样 ANN 对精确搜索的召回和 p95 延迟通过门槛，且模型指纹、索引版本和参数匹配时，`PGVECTOR_SEARCH_MODE=auto` 才使用该索引；权限或文档过滤仍走精确路径。抽样通过只适用于验收语料，需结合业务金标准复核。

8 路并发属于配置额度，不能据此承诺 8 人同时生成的延迟。当前为单服务器架构，没有数据库集群高可用、自动故障切换、SSO/MFA；跨主机扩展需共享对象存储和一致的连接器挂载。图片理解及 Excel/PPT 专项解析不在当前支持范围。

## 7. 开发、测试与效果评测

后端使用 Python 3.11；前端开发需 Node.js 22.12 或以上，Docker 构建使用 Node.js 24。创建开发虚拟环境：

```bash
python -m venv venv
```

Windows PowerShell 使用 `venv\Scripts\Activate.ps1` 激活，Linux 使用 `source venv/bin/activate`。然后安装依赖并验证：

```bash
python -m pip install -c constraints-server.txt -r requirements.txt
python -m pytest -q --disable-warnings
python -m scripts.evaluate_routing
cd web
npm ci
npm run test
npm run build
npx playwright install chromium
npm run test:e2e
```

默认开发安装包含旧数据兼容和测试工具，生产只安装 `requirements-runtime.txt`。需要外部 Ragas 等评估工具时单独安装 `requirements-eval.txt`；项目自带的确定性评测脚本不要求这组依赖。进程内 BGE 重排依赖单独放在 `requirements-local.txt`。

后端 PostgreSQL/Redis 集成测试和真实模型测试需相应服务及显式配置；跳过不代表已完成验收。前端浏览器回归使用模拟 API。

对本地开发数据库，可在项目根目录运行真实效果评测。该方式读取本地 `.env`：

```bash
python -m scripts.evaluate_rag --mode retrieval --split tuning --questions eval/data/questions.jsonl --label baseline
python -m scripts.evaluate_rag --mode full --split holdout --questions eval/data/questions.jsonl --label final
```

验收 Docker 服务器时，在容器内指定知识库，并把题集传入、结果取回：

```bash
docker compose --env-file .env.server -f deploy/compose.yml cp eval/data/questions.jsonl api:/tmp/rag-evaluation-questions.jsonl
docker compose --env-file .env.server -f deploy/compose.yml exec -T api python -m scripts.evaluate_rag --mode full --split holdout --knowledge-base <测试知识库ID> --questions /tmp/rag-evaluation-questions.jsonl --output /tmp/rag-evaluation-final.json --label final
docker compose --env-file .env.server -f deploy/compose.yml cp api:/tmp/rag-evaluation-final.json eval/results/final.json
```

先创建本地 `eval/results` 目录；容器中的命令使用 `.env.server` 注入的数据库和模型配置，避免误测本地旧库。

`eval/questions.example.jsonl` 提供问题格式示例，业务问题与金标准需自行准备。完整问答、启用改写或模型路由的检索评测会请求模型。评测应检查召回、引用支持、答案事实、延迟和错误率；合成测试及组件基准不能替代目标服务器上的真实模型并发验收。

可回答题的完整评测需提供 `required_facts`（事实短语及对应原文 `source`/`quote`）和可选 `forbidden_claims`；未标注事实集不能通过默认完整验收。字面短语和证据命中仍需人工抽查语义准确性。

### 真实并发验收

在测试知识库准备固定文档和 JSONL 用例，每行示例：

```json
{"id":"case-1","query":"退货期限是多少天？","document_ids":["测试文档ID"],"expected_document_ids":["测试文档ID"],"expected_terms":["七天"],"forbidden_terms":["三十天"]}
```

将登录会话的令牌放到环境变量 `RAG_TEST_TOKEN`（多用户可重复指定 `--token-env`），运行：

```bash
python -m scripts.verify_capacity --target http://127.0.0.1:8080 --knowledge-base <测试知识库ID> --token-env RAG_TEST_TOKEN --cases eval/data/capacity.jsonl --report eval/results/capacity.json --concurrency 1,4,8 --samples 40
```

默认每档至少 20 个完成样本、3 轮及每题 3 次，输出首字/总时延 p50/p95、错误率、拒答率、证据命中及降级率；不足为 `unverified` 并返回非零。阈值可通过 `--help` 调整。添加 `--background-create-isolated-kb` 会新建临时知识库并有限量上传，比较后台索引干扰；结束后删除本次文档并归档临时库，需有创建和删除权限。该选项会请求实际模型服务。记录服务器规格、文档/片段数、向量维度、冷热缓存和模型配额，再逐步扩大语料；最终只报告通过门槛的实测范围。

## 8. 项目目录

| 路径 | 用途 |
| --- | --- |
| `app/` | API、权限、业务服务、数据库模型和任务执行 |
| `rag/` | 文档解析、切片、检索、生成、图谱与模型适配 |
| `web/` | Vue 工作台、前端构建和测试 |
| `deploy/` | Compose 与服务器配置示例 |
| `scripts/` | 启动、迁移、备份、账号恢复、诊断与评测 |
| `migrations/` | 数据库迁移 |
| `integrations/` | OpenCode 固定源码清单、上游许可证和构建产物 |
| `tests/`、`.github/` | 自动化测试与持续集成 |
| `eval/` | 评测样例、私有语料、基线和结果 |
| `docs/` | 本 README 与五容器架构说明 |
| `data/` | 原文、兼容数据、迁移记录、智能体状态、运维状态和备份 |
| `vendor/opencode/` | 按固定版本准备的 OpenCode 源码 |
| `.env.server` | 私有服务器配置，不提交到版本库 |

依赖目录、OpenCode 源码和构建产物服务于本项目的运行或开发，删除后需重新安装或构建。运行数据、服务器密钥、原文和备份应按实际用途管理。
