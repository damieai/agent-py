# Langfuse 基础接入（LF-01 的首个切片）

状态：**implemented，SDK/OTLP MockTransport verified**。这是更新计划中 LF-01 的部分实现，不是 LF-01 整体验收。尚未连接真实 Langfuse、调用付费模型、部署自托管组件或完成生产开销验收；已新增隔离 loopback HTTP 的本地探索性开销测量；LF-02—LF-04 未实现。

## 已有能力

- 可选依赖固定 `langfuse==4.15.4`，默认不安装、不启用、不上传。启用后共享现有 `Telemetry.provider`，不创建全局 provider、不自动抓取 HTTP/模型参数。
- 使用锁定 SDK 的 OTel 属性编码，项目负责元数据选择、protobuf OTLP、有限队列及 HTTP 导出。没有初始化高层 Langfuse client，也没有后台 prompt、评分或媒体上传线程。SDK 内部编码入口集中在 `langfuse_export.py`，升级须重跑 wire-format 测试。
- 一个 Worker tick 是执行段，真实派发的模型调用是 `model.generation` 子 span。单次、多轮调查与候选修复共用模型网关；`asyncio.to_thread` 保留同一 trace 的父子上下文。CLI 模型调用可独立成为根 span，通过稳定 session 归属任务。
- 已保存决策记录为 `model.result_reused`，保持相同推理伪名，不写 usage/cost，不新建 generation。派发 CAS 失败及已结算但缺失结果不会再记录一次实际调用。
- generation 包含模型、内嵌指令/工具 schema 摘要、实际请求和上下文摘要、发布标识摘要、价格、有效 token usage、校验结果与停止标记。`release_digest` 是发布 ID 字符串的 SHA256；legacy `agent-v1` 的摘要不是源码证明。
- usage 仅接受供应商返回并通过网关校验的输入/输出统计；未返回统计则 `usage_state=unknown`，不导出 token=0 或费用=0。`reserved_micro_usd` 单独标为本地预占，不能作为实测费用。费用换算使用配置的单价，不宣称完成账单校准或缓存 token 支持。

## 配置

```bash
uv sync --extra dev --extra langfuse --frozen
```

服务端环境示例，密钥由运维系统注入，不能写入仓库或浏览器：

```dotenv
AGENT_LANGFUSE_ENABLED=true
AGENT_LANGFUSE_BASE_URL=https://your-langfuse-origin
AGENT_LANGFUSE_TENANT=demo
AGENT_LANGFUSE_PUBLIC_KEY=pk-lf-...
AGENT_LANGFUSE_SECRET_KEY=sk-lf-...
AGENT_LANGFUSE_PSEUDONYM_KEY=INDEPENDENT_RANDOM_SECRET_AT_LEAST_32_CHARACTERS
AGENT_LANGFUSE_QUEUE_SIZE=256
AGENT_LANGFUSE_TIMEOUT_SECONDS=2
AGENT_LANGFUSE_FLUSH_SECONDS=3
AGENT_LANGFUSE_SAMPLE_RATE=1
AGENT_LANGFUSE_BATCH_SIZE=16
AGENT_LANGFUSE_BATCH_WAIT_SECONDS=0.05
```

端点必须是 HTTPS origin，不接受 URL 凭证、路径、查询或 fragment；development/test 可使用 loopback HTTP。生产不得降级 HTTP。安装缺失 SDK 或启用时配置缺失会在启动阶段失败；运行中的网络故障则仅影响遥测。

每个进程绑定一个租户/环境与一套独立 project 凭证。只有配置租户的 span 能进入该项目；其他租户仍可按业务配置执行，但其遥测被过滤。需要多租户完整观测时部署独立观测绑定的 Worker/进程，不能把多个租户复用同一 project 当作权限隔离。平台成员权限、地域和项目分配须独立配置并实际验证。

tenant、task session、推理身份使用独立密钥的 HMAC-SHA256 伪名，域包含环境和租户。相同密钥/绑定下可关联重启后的执行段；密钥轮换改变伪名。当前持久化任务执行段关联，但没有工作台跳转或反查 API，不把伪名误称为平台 ACL。

启用仅影响观测，不改变模型请求、授权、预算或 AgentRelease 的业务运行策略。配置与密钥须单独管理；观测配置尚未纳入发布清单。日常默认仍为关闭。

## 导出与数据边界

允许导出的 span 名仅有 `task.accepted`、`task.dispatch`、`worker.tick`、`model.generation`、`model.result_reused`、`retrieval.compile`、`tool.read`、`sandbox.verify`、下述六种 `operation.*` 与四种任务控制 span 以及显式联调专用的 `diagnostic.probe` / `diagnostic.child`，且 instrumentation scope 必须是项目自己的 `agent-py`。应用端只添加选定字段；处理器在排队前重建干净 span，剔除其余属性、事件、未允许的 links、status 文本和资源元数据。

不导出任务目标、查询原文、证据正文、源代码、补丁、模型输出、异常信息、headers、原始业务 ID 或凭证。正文摘要不等于内容授权，因此首期不支持 redacted/input-output 模式。其他 exporter（包括本地 JSONL）仍执行各自白名单，不能认为 Langfuse 过滤器会替它们脱敏。

排队内容仅为净化后的 protobuf，每项最多 16 KiB；队列默认 256、最多 4096 项，另有一组正在组装或发送的批次（默认 16 条、最多 64 条）。满队列、新请求发生在关闭后、超出大小或净化失败时丢弃遥测，不阻塞业务。单个导出线程将净化后的 OTLP 消息合并成批，每批仅发送一次 HTTP 请求，不重试、不跟随跳转、不继承代理环境；响应限制 64 KiB，非 2xx、无效 protobuf 或 OTLP partial rejection 都将整批按 span 数计为导出失败；部分拒收无法识别具体接受了哪些 span，因此这一计数是保守分类，不表示平台一条都未接收。

请求 I/O timeout 默认 2 秒，关闭最多等待 3 秒后丢弃剩余队列；正在组装的批次会提前结束等待；在途 daemon 线程可能继续至 I/O 结束，完成后整批计入成功/失败。该 timeout 不是网络请求的绝对墙钟终止保证。正常 CLI 退出、API lifespan 和 Worker shutdown 关闭 provider；SIGKILL 可能丢失未导出数据，禁止重跑业务来补 trace。

Prometheus 指标 `agent_langfuse_spans_total{result=...}` 包含 `queued`、`exported`、`filtered`、`dropped`、`sanitization_failed`、`export_failed`、`correlation_failed`、`sampled_out`。`exported` 只表示接收端返回成功 OTLP 应答，不证明平台页面已经完整可见。没有把任务/租户 ID 放入新增指标标签。

## 跨进程执行段关联

启用前运行 `alembic upgrade head`，迁移 `0012_task_traces` 新建租户隔离表。API 创建任务时，在 Task/Outbox 的同一事务中保存服务端创建的 origin；客户端的 `traceparent`、`tracestate` 和 baggage 不参与关联。幂等创建保留第一次 origin。

Dispatcher 保存首次派发的 traceparent，通过 Temporal workflow/activity 参数的 `trace_context` 字段传播。丢失 start ACK 后重试沿用第一次 carrier；历史 workflow 输入没有此字段仍可运行。每次派发和 Worker tick 创建新的根 span，通过 OTel links 指向 origin、已持久化且匹配本任务的 dispatch、上一执行段，避免 span 跨越审批等待或进程生命周期。generation 仍是当前 tick 的子 span；稳定 HMAC session 将这些独立 trace 归到同一任务。

数据库仅保存版本 00 的 traceparent，不保存 baggage、tracestate 或正文。复合外键阻止跨租户引用；PostgreSQL 强制 RLS。Worker 更新上一执行段指针时检查当前工作租约，失效 Worker 无权推进指针。任务创建后的关联读写失败降级并计数，不修改业务版本、幂等身份、预算或恢复决策。任务创建时的原子写入需要迁移和数据库可用；它不是独立的异步写入。

导出只允许派发和 tick 的最多三个 links，属性仅保留 `agent.link=origin|dispatch|previous`。关闭开关或非配置租户不会写入关联表。启动观测前已存在的任务可在派发/执行时补充关联，但不补造 origin。该表只保存 origin、首次 dispatch 和最新 tick，完整链依赖已导出的 spans；SIGKILL 或丢弃队列可留下断链，不作为业务审计事实。

本地验证覆盖真实 API 请求、Outbox ACK 丢失、多个独立 Worker 进程、租约 fencing、元数据故障降级，以及原生 Temporal 参数传递和 history replay。原生 PostgreSQL 验证迁移升降级、RLS 和跨租户外键。导出使用 MockTransport；Langfuse 平台中的 links 展示、检索和保留策略仍待真实联调。

## 检索、只读工具与沙盒阶段

三个阶段以普通 Langfuse span 编码，在 Worker 内继承当前 tick 的父上下文；API 上下文预览继承请求 span，CLI 直接调用可独立成根，通过 session 归属任务。没有 task ID 的离线检索不导出阶段记录。

| 阶段 | 白名单元数据 | 语义 |
|---|---|---|
| `retrieval.compile` | lexical/BM25-RRF 策略、纳入/省略数量、上下文字节数、上下文摘要 | 每次实际编译一条；持久化上下文复用、ACL 复核不伪造新检索 |
| `tool.read` | 四类企业读取 provider、局部重试序号 1—3 | 每次进入读取 callback 一条；授权、熔断、并发限制在 callback 前拒绝时不记录读取 |
| `sandbox.verify` | baseline/candidate 阶段、退出码、TIMEOUT/OUTPUT_LIMIT/none | 每次验证器调用一条；基线抛异常时不伪造候选执行 |

各阶段的 `outcome=completed|error` 表示调用返回或抛异常。`completed` 不等于证据已授权入库、回归通过或业务验证成功；沙盒结论仍由验证报告决定。工具 callback 包含凭证检查和客户端初始化，因此进入 callback 不保证已经发出网络请求。attempt 只表示本次读取策略内的重试序号，不是跨 Activity 的全局尝试数。

检索数量是纳入/省略的结果项数量，BM25-RRF 下可能为文档分块。现有 `estimated_tokens` 实际按 UTF-8 序列化字节计算，因此观测明确使用 `context_bytes`，不能用于模型 token 计费。阶段只记录总耗时，不声称拆出了网络或容器启动时间。

不导出查询、证据 ID/正文、URL、工具参数、凭证、文件路径、代码、stdout/stderr、异常文本。严格校验阶段字段的类型、范围及枚举。默认关闭时无外部上传；现有本地 exporter 继续使用自己的白名单。

本地测试通过真实 Harness、读取重试策略和 VerificationRunner，使用 MockTransport 与替代沙盒验证阶段语义和净化结果；没有执行真实企业服务请求或 Docker 回归。写动作观测见下一节，不能把只读工具 span 当作写操作执行。

## 写动作生命周期

| span | 记录边界 |
|---|---|
| `operation.propose` | 提议方法返回/失败，`changed` 区分新建和幂等复用 |
| `operation.approval` | 已通过租户及审批角色检查的决定，记录 APPROVED/REJECTED；`changed=false` 表示重复决定 |
| `operation.execute` | 只有 CAS 成功并提交 PENDING 意图后才进入；包围适配器执行及本地回执落账 |
| `operation.query` | 对 PENDING/UNKNOWN 动作进行权威查询及回执校验；终态调用不产生查询 span |
| `operation.result_reused` | execute 读到已有派发/终态记录，直接复用而不调用适配器 |
| `operation.escalate` | 超时未确认动作的 MANUAL_REVIEW 更新提交后记录；重复扫描、CAS 失败或事务回滚不新增升级记录 |

元数据包含固定工具类型、action_kind=standard/rollback、账本 attempts、执行配置 simulation/live、状态及 operation HMAC 伪名；相同租户/任务/动作的各阶段使用相同伪名。不会导出参数、resource、审批人、外部回执 ID、返回正文或异常文本。生命周期 span 不生成模型 usage/cost。

`stage.outcome=completed` 表示方法正常返回，动作成败必须读取 `status`：响应丢失、未经确认的回执仍为 UNKNOWN；适配器明确拒绝才为 FAILED；只有账本接受权威确认后才记录 SUCCEEDED。本地落账失败记录 error 和派发后的 PENDING，恢复必须查询权威，不依据 span 重发动作。`changed` 只有在 outcome=completed 时才能视作方法成功提交后的变更指示；它不是审计事件。

执行模式来自服务配置，不是外部能力认证。当前测试使用本地持久化 SimulatedSystem，包含响应丢失、回执滞后、并发 CAS、审批重试、无效回执及落账失败；未认证的 live 写适配器仍在派发前拒绝。真实企业写动作验收、自动补偿的因果关联以及平台展示仍未完成。回滚现有路径的审批、派发、UNKNOWN、对账与结果复用都使用相同动作伪名；不能将 rollback 标签当作已实现自动 Saga。

## 取消、接管、恢复与结束

| span | 状态语义 |
|---|---|
| `task.cancel` | 取消请求提交后记录 cancelled/status；不代表在途外部动作被撤销 |
| `task.takeover` | 人工接管及重复请求；不改写取消事实 |
| `task.resume` | 通过版本、权限、截止时间及策略检查后恢复；错误不伪造恢复结果 |
| `task.finish` | 结束检查返回的实际状态；UNKNOWN/PENDING 保持对账等待，接管任务保持等待，缺验证证据不得标成功 |

元数据白名单只包含 task_status、task_result、version、cancelled、taken_over、changed 及少量固定 waiting_reason；不导出操作者、任务目标或任意错误/等待文本。`changed` 区分业务变更与幂等调用，只有 outcome=completed 时可按成功提交解释。finish 调用正常返回不一定终止任务，必须结合 task_status 与 task_result；未结束时不补造结果。方法耗时包含现有数据库操作，不表示用户等待时长。

operation.escalate 是提交后发出的短 observation，其耗时不代表数据库升级耗时。它同时记录动作 UNKNOWN/PENDING 与任务控制状态，人工接管或取消不会被升级扫描覆盖。账本事件仍是审计事实，进程在提交后、导出前终止可能使该 observation 缺失，不为补遥测重做升级。

本地测试覆盖取消后的未知结果对账、重复取消/结束、过期版本恢复失败、并发人工升级、升级事务回滚、导出失败不阻止取消，以及回滚审批后的响应丢失/查询确认/幂等复用。stop/resume/finish 的原有返回契约及授权规则保持一致；观测不会派发补偿动作。真实业务效果仍以外部权威与验证证据为准。

## 任务级采样与本地导出验收

`AGENT_LANGFUSE_SAMPLE_RATE` 范围为 0—1，默认 1。按环境、租户、任务 ID 与独立伪名密钥计算确定性 HMAC，决定整个任务的导出选择。同一配置下跨进程、重启和新的 trace ID 保持一致；不是逐 span 随机采样，也不是 OTel provider 的全局采样器，因此不影响其他 exporter。采样决定先于净化/SDK 编码/入队，未选中的任务不写 `task_traces`；仍会创建普通 OTel span，不能理解为关闭全部埋点开销。

API、Dispatcher、Worker 必须使用相同采样率、环境和伪名密钥。修改采样率或轮换密钥会改变在途任务的选择，可能出现断链；当前未将采样策略固定到任务版本。rate=0 停止新导出与关联写入，但不删除历史数据，也不撤回已在队列中的数据。失败任务没有额外 tail sampling 保留策略。固定实验必须配置 rate=1，并独立核对预期记录是否完整；采样指标不能证明实验完整，更不能据采样成本推算实际总账单。

本地复现实验：

```bash
make langfuse-check
# 指定样本数/重复次数；性能上限须在运行前给定，单位为新增 P95 毫秒
.venv/bin/python scripts/check_langfuse.py --tasks 40 --repeats 3 --max-added-p95-ms 10
```

脚本为每个场景创建新进程、临时数据库与本地模拟权威，清除继承的 AGENT 配置，固定 loopback HTTP 和假凭证。覆盖关闭、正常导出、50% 任务采样、HTTP 503、阻塞端点/队列满五种场景；按轮次轮换顺序。使用真实 HTTPX、OTLP protobuf 和接收线程，不连接真实 Langfuse、不调用模型。每个任务执行创建、提议和模拟 create_pr，并核对只发生一次副作用。

报告存放在 `.runtime/langfuse/run-*/report.json`，包含源码/锁文件摘要、逐任务墙钟延迟、P50/P95、吞吐、工作负载 CPU 时间、进程峰值 RSS、关闭耗时及导出/失败/丢弃计数。缺失子进程结果、净化泄漏、业务结果变化、队列未排空、未触发预期故障都会使 correctness=FAIL 并返回非零。只有显式传入预设上限才计算 performance=PASS/FAIL；默认 NOT_ASSESSED，不将功能通过冒充性能通过。Bitbucket 自定义 `langfuse-local-probe` 流程保留报告。

这属于小样本本地探索：包含 SDK/解释器启动内存、没有剔除预热、没有置信区间，也没有控制宿主机其他负载；CPU 仅测主动任务阶段，RSS 为整个子进程峰值。此处 P95 是创建/提议/执行三个同步方法的合计耗时，不能替代真实 API/Temporal/模型的端到端性能。批量实现前，2026-09-21 三轮各 40 任务实测：关闭 P95 11.74—19.03 ms，正常导出 P95 26.51—42.94 ms，阻塞场景每轮丢弃 113 条，功能检查通过；未预设性能阈值，不能据此宣布生产验收通过。异步导出仍有可测开销，后续应评估批量发送与生产负载下的队列容量。

### 有界批量发送

`AGENT_LANGFUSE_BATCH_SIZE` 范围 1—64，默认 16；`AGENT_LANGFUSE_BATCH_WAIT_SECONDS` 范围 0—1 秒，默认 0.05。从第一条出队开始计时，批次满或等待到期即发送。显式 force_flush 和 shutdown 会唤醒组装线程提前发送，不额外等待批次定时器。等待时间不包括排队与网络 I/O，不能当作导出的端到端时延上限。

每条队列消息最多 16 KiB，因此单批请求体最多 `batch_size × 16 KiB`，绝对上限 1 MiB。队列容量仍按 span 计算；组装批次也有容量上限，合并请求体时会额外分配一份有界字节缓冲区。只合并已净化的 protobuf，不把原始 span/业务参数留在后台队列。所有 exported/export_failed/queued/dropped 计数仍以 span 为单位，不因请求合并改变单位。

对照命令依次执行，避免两个负载同时运行：

```bash
.venv/bin/python scripts/check_langfuse.py --tasks 40 --repeats 3 --batch-size 1
.venv/bin/python scripts/check_langfuse.py --tasks 40 --repeats 3 --batch-size 16
```

报告新增 batch_size、HTTP 请求数和最大请求体字节数。2026-09-21 依次运行上述两组命令，三轮健康端点的结果如下；两组均完整接收每轮 120 条 span，五种场景的功能检查全部通过。

| 指标（每轮 40 个任务） | batch_size=1 | batch_size=16 |
|---|---|---|
| HTTP 请求数 | 120 | 11—13 |
| 同步任务 P95 | 23.57—45.91 ms | 18.16—22.43 ms |
| 主动负载 CPU 时间 | 0.99—1.57 s | 0.65—0.75 s |

原始报告分别为 `.runtime/langfuse/run-elwzuir2/report.json` 与 `.runtime/langfuse/run-m13pnr4b/report.json`。此测量显示本地请求数减少约 90%，没有预设性能阈值，performance 仍为 NOT_ASSESSED；顺序执行也不能排除宿主机负载变化，不将范围差异直接当作生产提速比例。

单条模式保留为回退配置；它仍使用同一有界队列、脱敏及故障策略。批量发送增加了低流量下的等待时间，并扩大了单次不确定应答影响的 span 数；真实平台吞吐与接收限制仍需实际验收。

## 显式平台回读诊断

完成现有 Langfuse 服务端配置后，可使用以下命令（不会构造 Service、连接业务数据库或调用模型）：

```bash
# 默认只验证本地配置和可选 SDK，输出 NOT_RUN；不联网
.venv/bin/agent-py langfuse-check --expected-project-id YOUR_PROJECT_ID
# 显式授权向配置端点发送两条合成诊断 span，并用同一凭证回读
.venv/bin/agent-py langfuse-check --expected-project-id YOUR_PROJECT_ID --allow-network
```

`expected-project-id` 必须由操作者从目标项目取得。联网时先调用 `GET /api/public/projects`，只有恰好返回预期项目才写入；端点或凭证误指其他项目时在上传前失败。要求启用 Langfuse 且 sample_rate=1，命令不悄悄覆盖采样配置。

随后通过实际导出适配器创建 `diagnostic.probe` 根 span 和 `diagnostic.child` 子 span，固定添加 `synthetic=true` 元数据。不伪造 generation、usage、费用或业务动作。OTLP 获得成功应答后，用 Observations API v2 按随机 trace ID 和有限时间范围查询，仅选择 core/basic/metadata，不请求输入输出。

命令核对两个 observation ID、项目、trace、父子关系、会话伪名、环境及 synthetic 标记。只收到成功应答但未能回读时返回 INCOMPLETE，不当作平台验收通过。默认最多查询 5 次，可用 `--attempts` 指定 1—10 次；只有记录尚不完整时每隔 2 秒重查，401/403/429/跳转、异常 schema 或数据不匹配直接失败，不重试上传。GET 响应最多 64 KiB，按块读取并检查 15 秒读取窗口，另有 HTTP I/O timeout；不是整个命令的绝对墙钟终止保证。没有启用代理继承或跟随重定向。

报告写入 `.runtime/langfuse-live/run-*/report.json`，目录权限 0700、文件权限 0600。报告包含项目/端点摘要、随机诊断 trace/span ID、SDK 版本、独立的 ingestion_acknowledged / observations_verified 标记及固定错误代码；不保存密钥、项目名称、响应正文或异常文本。PASS 与离线 NOT_RUN 返回 0，FAIL/INCOMPLETE 返回非零；NOT_RUN 只表示本地检查结束，禁止把它用于宣称平台已验收。

每次显式联网调用都会生成新诊断记录，不自动删除平台数据；保留/删除按项目策略单独处理。此入口验证合成元数据的写入和回读，不验证控制台 links 展示、用户权限隔离、真实模型 usage 或三种业务轨迹。当前只以 MockTransport 对官方协议完成测试，尚未用真实项目凭证运行。

协议依据：[Langfuse Public API](https://langfuse.com/docs/api-and-data-platform/features/public-api)（项目凭证与 Observations v2）、[弃用 API 迁移](https://langfuse.com/faq/all/deprecated-api-migration)。新入口只支持 Observations v2，不自动退回旧 trace API；部署版本需支持该接口。

## 三类工作流的本地轨迹验收

```bash
make langfuse-trajectories-check
# 等价命令；只使用临时数据和固定替身，不需要平台/模型凭证
.venv/bin/python scripts/check_langfuse_trajectories.py
```

此入口通过实际 Activities.tick、InvestigationHarness、RepairHarness、预算/推理账本、制品存储及真实 OTLP protobuf/回环 HTTP，分别运行单次调查、多轮调查和候选修复。每个工作流在 disabled、healthy、rejected（HTTP 503）三种导出模式下各运行一次，共九个隔离子进程；不启动 Temporal server。清除继承的 AGENT/LANGFUSE/OTEL/模型提供商环境变量，Settings 不读取 .env，模型使用 MockTransport，沙盒只检查固定源码字节，不执行候选代码，企业写执行器始终禁用。

每例先推进到人工审核状态，再关闭 exporter、重建 Service/Harness/provider/数据库连接池并重放 tick，验证持久化决策和制品复用。此处是有序重建，不是 SIGKILL 恢复演练。验收条件包括：

- 单次调查 1 次模型请求、多轮调查 3 次并以 ROUND_LIMIT 停止、候选修复 1 次且基线/候选验证各 1 次；重建后不增加模型或沙盒调用。
- 账本余额、reservation 实际金额与 generation 导出的 usage/cost 相符；result_reused 引用同一推理伪名和请求摘要，不再次记录费用。
- 三种导出模式的业务汇总完全一致；没有 Operation，原始源码不变，任务仍等待审核，不伪造业务 SUCCESS。
- generation/retrieval/sandbox 是对应 worker.tick 的子 span；tick 使用独立根、关联原始 task.accepted 和前一个 tick；Service 重建不切断关联。
- OTLP 不包含注入到目标、证据、模型答复、源码、日志及密钥中的 canary，也不包含原始 task ID 或临时目录；关闭模式无上传，503 模式按 span 计失败且不影响业务。

使用固定合成单价：输入 1、输出 2 micro-USD/token。三个场景账本分别为 50、150、40 micro-USD，**不是实际模型费用或生产质量测量**。不以此样本推断性能收益。

报告位于 `.runtime/langfuse-trajectories/run-*/report.json`（目录 0700、文件 0600），保留源码/锁文件摘要、九组业务账本汇总、经过选择的 OTLP span 身份/父子/link/usage/cost 字段、HTTP 次数及 exporter 计数，不保存请求正文、模型响应或原始 OTLP。父进程从证据重新计算结果；子进程失败、超时、证据缺失/类型错误、源码在运行中变化或任何不变量失败都返回非零。测试包含重复 generation、断链、费用篡改、重复计费和缺失报告等反例。

correctness=PASS 仅表示本地契约通过；platform、real_model、real_sandbox 始终标为 NOT_RUN，performance 为 NOT_ASSESSED。此报告不进入质量优化门禁冒充真实实验。Bitbucket 自定义 `langfuse-trajectory-rehearsal` 保存报告，默认 Python 测试也运行九组验收与反例。真实模型、容器验证和 Langfuse 控制台/回读验收仍需单独执行。

2026-09-21 实测九组 correctness=PASS，原始报告为 `.runtime/langfuse-trajectories/run-0vfjpdc9/report.json`。正常模式分别收到 7、17、10 条 span，重建后无额外模型请求；其余平台/真实执行状态保持 NOT_RUN。

## 独立自托管配置

已提供固定镜像摘要的六服务 Compose、私有凭证初始化、离线策略校验及升级/恢复说明，见 [自托管操作手册](langfuse-selfhost.md)。已通过 Compose 原生解析；尚未启动容器或完成真实平台回读。

## 验证与待办

```bash
.venv/bin/pytest tests/test_langfuse.py tests/test_trace_context.py tests/test_stage_observations.py tests/test_operation_observations.py tests/test_langfuse_batching.py tests/test_langfuse_check.py tests/test_lifecycle_observations.py -q
env AGENT_TEST_TEMPORAL=1 .venv/bin/pytest tests/test_trace_context.py -m integration -q
env AGENT_TEST_POSTGRES=1 .venv/bin/pytest tests/test_postgres.py -q
```

测试使用实际锁定 SDK 的属性编码、实际 OTel span/protobuf 和 MockTransport，不需要外部凭证。覆盖线程上下文、跨租户过滤、多 exporter 数据边界、复用不重复计费、未知 usage、容量丢弃、超时关闭、平台失败/跳转/partial rejection 及 CLI 清理。Python CI 安装 langfuse extra 后执行这些测试；缺少可选 SDK 的常规环境会显式跳过此测试模块。

LF-01 仍待：真实 Langfuse OTLP 联调和三类真实模型轨迹、补偿动作的因果关联、完整项目权限与保留/删除策略、自托管容器启动、部署/断网/吞吐与 P95 性能验收。当前不能用这份基础代码宣称完成完整 LF-01，更不能宣称策略质量已经提高。

官方依据：[SDK 与 OTel](https://langfuse.com/docs/observability/sdk/overview)、[现有 OTel 集成](https://langfuse.com/faq/all/existing-otel-setup)、[Python API 参考](https://python.reference.langfuse.com/langfuse)。实际编码以锁定 4.15.4 源码及 wire-format 测试为准。
