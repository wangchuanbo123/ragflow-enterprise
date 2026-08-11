# RAG 核心业务处理器基础功能与接口清单

## 1. 文档范围

本文只描述基础版 RAG 核心业务处理器（以下简称 Core）的职责，以及它与应用层、RAGFlow、文档图谱和代码图谱容器之间的接口约定。

基础版采用 HTTP API 和任务状态轮询，不引入消息队列、事件总线、增量图谱或复杂发布流程。RAGFlow 是原始文档、文档 Chunk 和文档检索的来源；Core 不直接读取 RAGFlow 的 MinIO、数据库或检索引擎，也不再实现本地文档解析、切块、关键词检索和向量检索。用于图谱构建的 Chunk 统一通过 RAGFlow 提供的不可变快照文件传递，不同时支持多种传输方式。

Core 生成的文档 Schema 用于文档图谱；代码图谱应按照双方约定的代码 Schema/Ontology 校验，不能直接套用文档 Schema。Schema 生成、检索计划和重排所需的模型通过 Core 的模型 Provider 调用，本文不限定模型部署方式。

## 2. Core 基础功能、风险与验证计划

| 功能点 | 基础功能说明 | 主要风险点 | 验证计划 |
|---|---|---|---|
| 对外 API | 接收应用层发来的 Schema 生成、图谱构建、任务查询和检索请求；验证 JWT 或服务凭据，并从凭据中确定用户、租户和知识库权限 | 直接信任请求中的用户/租户字段；参数缺失；知识库越权；重复请求 | 使用有效、过期、伪造和越权令牌测试；使用相同 `Idempotency-Key` 重复创建任务，确认返回同一个任务而不是重复执行 |
| 获取文档 Chunk 快照 | 请求 RAGFlow 为指定知识库和文档生成不可变 Chunk 快照，获得 `snapshot_id`、快照文件地址、Chunk 数量和文件哈希；Core 读取该快照用于 Schema 生成 | 快照缺少 Chunk；文件被修改；快照内容与所选文档范围不一致 | 校验快照文件哈希、Chunk 数量、dataset/document ID 和必需字段；修改后的文件必须校验失败 |
| 生成并保存文档 Schema | 使用固定的一批文档 Chunk 和业务说明生成 Schema，校验 JSON 格式后保存版本 | 样本不足导致实体或关系缺失；输出格式错误；构建时使用错误版本 | 使用固定输入检查 Schema 格式、实体和关系；创建两个版本，确认构建请求和结果中的 `schema_version` 一致 |
| 发起文档图谱构建 | 向文档图谱容器提交任务 ID、知识库 ID、Schema 版本、`snapshot_id`、Chunk 快照文件地址及哈希 | 文档图谱服务不可用；同一任务重复构建；输入快照与 Schema 不一致 | 模拟服务不可用和重复提交；确认错误可查询、相同任务 ID 不产生重复结果，返回的快照和 Schema 版本与请求一致 |
| 发起代码图谱构建 | 将应用层请求中给出的仓库 ID、固定 commit 和代码来源地址转交代码图谱容器，同时提供任务 ID 和代码 Schema/Ontology 版本 | 应用层未固定 commit；代码来源与返回图谱版本不一致；服务构建失败 | 使用固定 commit 构建；检查返回的仓库 ID、commit、任务 ID 和代码图谱版本；模拟失败并确认 Core 正确记录 |
| 管理构建任务 | 轮询两个图谱任务，记录 `PENDING/RUNNING/VALIDATING/SUCCEEDED/FAILED/TIMED_OUT`；只有两个下游任务成功且 Core 校验通过后，父任务才标记成功 | 任务长期卡住；把下游成功误当成校验成功；一个成功一个失败；Core 重启后状态丢失 | 模拟成功、校验失败、执行失败和超时组合；Core 重启后继续查询；确认必须满足“下游成功 + Core 校验通过” |
| 校验文档图谱 | 获取文档图谱节点、边和证据，校验 Schema 版本、节点/关系类型、边两端节点以及 evidence Chunk ID | 图谱容器只返回统计信息导致 Core 无法校验；证据引用不存在或来自错误文档 | 注入非法类型、悬空边、错误 Schema 版本和不存在的 Chunk ID；确认 Core 拒绝该图谱 |
| 校验代码图谱 | 获取代码图谱节点、边和源码位置，校验代码 Schema/Ontology 版本、commit、文件路径、行号和关系类型 | 错用文档 Schema；源码位置不存在；结果属于其他 commit | 注入错误 Ontology 版本、错误 commit、不存在文件和越界行号；确认 Core 拒绝该图谱 |
| 绑定活动版本 | 保存当前可检索的数据版本组合：RAGFlow dataset revision、`snapshot_id/manifest_sha256`、文档 Schema、文档图谱版本、代码 commit、代码 Ontology 和代码图谱版本 | 在线文档已经变化但仍使用旧文档图；不同版本结果被混合 | 修改任一版本后执行检索；确认版本匹配时正常使用，不匹配时禁用过期图谱并返回明确状态 |
| 生成检索计划和路由 | 基础版优先使用固定规则判断调用 RAGFlow、文档图谱、代码图谱中的一个或多个服务，并限制 `top_k` 等参数 | 问题路由错误；模型生成容器不支持的参数；查询范围越权 | 为文档、代码和混合问题预先写明期望通道集合及 `top_k` 上限，确认实际计划和回退结果完全匹配 |
| 执行并统一检索结果 | 将已验证的权限范围下推到各检索服务，并把结果统一为包含 `candidate_id、channel、rank、text、evidence_key、source_version` 的候选 | 某个服务超时拖垮整个请求；下游未执行权限过滤；图谱结果没有证据文本；不同版本结果混合 | 模拟正常、超时、空结果、越权结果和缺字段；确认可用通道继续返回，越权、缺少正文或版本的候选被拒绝 |
| RRF 与重排 | 按各通道返回顺序生成 rank，使用 rank 做 RRF；文档按 `document_id + chunk_id + content_hash`、代码按 `commit + path + span` 去重，再对正文或源码重排 | 直接相加不同服务的原始分数；同一证据重复出现；重排失败 | 使用固定排名计算预期 RRF；检查文档和代码去重键；模拟重排失败并确认回退到 RRF 顺序 |
| 拼接并返回上下文 | 按长度预算拼接文档 Chunk、图谱证据和源码片段，返回上下文、来源引用、通道状态和错误信息给应用层 | 上下文重复或过长；引用无法定位；部分服务失败但应用层不知道 | 使用重复 Chunk、超长文本和多代码片段测试；检查长度限制、引用 ID、文件行号和通道错误信息 |
| 健康与错误处理 | 提供 Core 健康接口，并检查三个依赖容器；检索服务部分不可用时返回降级信息 | 外部服务不可用但仍错误标记为正常；异常信息不明确 | 依次停止 RAGFlow、文档图谱和代码图谱服务；检查健康状态、构建失败状态和检索降级结果 |

## 3. 其他容器与 Core 的基础接口约定

| 相关容器 | 对方必须向 Core 提供的接口和能力 | Core 需要向对方提供或返回的内容 | Core 收到结果后的处理 |
|---|---|---|---|
| 应用层容器 | 不要求应用层提供业务服务接口；调用 Core 时携带可验证的 JWT/服务令牌、`kb_id`、`request_id`，并按场景提供业务说明、用户问题，以及代码构建所需的 `repository_id`、固定 `commit` 和代码来源地址 | Core 提供 `POST /api/v1/schemas/generate`、`POST /api/v1/builds`、`GET /api/v1/builds/{job_id}`、`POST /api/v1/retrieval`、`GET /health/ready`；创建类请求接受 `Idempotency-Key`；返回 Schema、任务状态、上下文、引用、通道状态和错误信息 | 验证令牌并确定权限范围后执行对应流程；应用层只负责展示和交互，不参与图谱校验、RRF 和重排 |
| RAGFlow 容器 | 原生接口：`GET /api/v1/system/healthz`、`GET /api/v1/datasets/{dataset_id}/documents`、`POST /api/v1/retrieval`。需由 RAGFlow 负责人或其适配层补充：`POST /integration/chunk-snapshots` 生成不可变 Chunk 快照，以及可比较的 `dataset_revision`。快照返回 `snapshot_id`、只读下载地址、Chunk 数量、文件哈希和 Chunk 明细；检索返回 Chunk 正文、来源、结果顺序、分数和实际 `dataset_revision` | Core 提供服务凭据、已验证的 dataset/document/ACL 范围；检索时提供问题、`top_k`、阈值和过滤条件。Core 不向 RAGFlow 写图谱，也不直接访问其 MinIO 或数据库 | 校验并读取快照用于 Schema 和文档图构建；按检索返回顺序补充 rank；比较检索返回的 `dataset_revision` 与活动版本，不一致时不使用旧文档图 |
| 文档图谱容器 | `GET /health/ready`、`POST /builds`、`GET /builds/{job_id}`、`GET /graphs/{graph_version}/export`、`POST /graphs/{graph_version}/search`。导出结果必须不可变且支持分页或流式读取，并返回节点/边/证据总数和 SHA-256；结果绑定 `job_id`、`schema_version`、`snapshot_id`、快照哈希和 `graph_version`；检索返回顺序及 evidence Chunk ID/文本 | Core 使用 `Idempotency-Key` 提交任务 ID、`kb_id`、Schema、`schema_version`、`snapshot_id`、Chunk 快照地址及哈希；检索时提供已验证的权限范围、问题、指定 `graph_version`、`top_k` 和允许的关系类型 | 轮询任务；核对导出总数和 SHA-256，校验 Schema、快照、节点、边和 Chunk 证据；校验通过后保存 `graph_version`；将图检索结果转换为统一候选 |
| 代码图谱容器 | `GET /health/ready`、`GET /capabilities`、`POST /builds`、`GET /builds/{job_id}`、`GET /graphs/{graph_version}/export`、`POST /graphs/{graph_version}/search`。导出结果必须不可变且支持分页或流式读取，并返回节点/边/证据总数和 SHA-256；结果绑定 `job_id`、commit、源码清单哈希、Ontology 版本和 `graph_version`；同时提供源码清单（文件路径、文件哈希、行数）及检索源码片段 | Core 使用 `Idempotency-Key` 转交仓库 ID、固定 commit 和代码来源地址，并提供任务 ID、`kb_id` 和代码 Schema/Ontology 版本；检索时提供已验证的权限范围、问题、指定 `graph_version`、仓库范围和 `top_k` | 轮询任务；核对导出摘要和源码清单，校验 commit、Ontology、文件路径、行号和关系类型；校验通过后保存 `graph_version`；将代码检索结果转换为统一候选 |

## 4. 基础版约束

图谱构建接口必须是异步接口：创建任务后返回 `job_id`，Core 再轮询状态。两个图谱容器如果只返回节点/关系数量而不提供节点、边和证据明细，Core 就无法完成独立校验。

所有接口统一返回 `request_id`、业务 `code`、`message` 和 `data`；错误响应还应返回 `retryable`。`request_id` 只用于追踪，创建 Schema 和构建任务必须另外使用 `Idempotency-Key`；相同幂等键和相同输入应返回同一任务。

RAGFlow v0.26.4 的公开检索结果提供 Chunk 内容及相关分数，但没有独立的 `rank` 字段，Core 应按返回顺序生成 rank。实际部署时应固定 RAGFlow 版本并对原生接口和新增的 Chunk 快照接口做联调测试。

基础版不保证 RAGFlow 在线索引的历史版本查询能力。文档新增、修改或删除后，应将现有文档图谱标记为过期并重新构建；新图谱校验成功前不得与变化后的 Chunk 混合使用。

除表中明确标注的 RAGFlow v0.26.4 原生接口外，其余路径均为建议的容器间目标接口，最终路径可以协商，但请求字段、版本字段和校验数据不能省略。
