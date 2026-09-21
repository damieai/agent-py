# Langfuse 基础接入（LF-01 的首个切片）

状态：**implemented，SDK/OTLP MockTransport verified**。这是更新计划中 LF-01 的部分实现，不是 LF-01 整体验收。尚未连接真实 Langfuse、调用付费模型、部署自托管组件或完成开销实测；LF-02—LF-04 未实现。

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
```

端点必须是 HTTPS origin，不接受 URL 凭证、路径、查询或 fragment；development/test 可使用 loopback HTTP。生产不得降级 HTTP。安装缺失 SDK 或启用时配置缺失会在启动阶段失败；运行中的网络故障则仅影响遥测。

每个进程绑定一个租户/环境与一套独立 project 凭证。只有配置租户的 span 能进入该项目；其他租户仍可按业务配置执行，但其遥测被过滤。需要多租户完整观测时部署独立观测绑定的 Worker/进程，不能把多个租户复用同一 project 当作权限隔离。平台成员权限、地域和项目分配须独立配置并实际验证。

tenant、task session、推理身份使用独立密钥的 HMAC-SHA256 伪名，域包含环境和租户。相同密钥/绑定下可关联重启后的执行段；密钥轮换改变伪名。当前持久化任务执行段关联，但没有工作台跳转或反查 API，不把伪名误称为平台 ACL。

启用仅影响观测，不改变模型请求、授权、预算或 AgentRelease 的业务运行策略。配置与密钥须单独管理；观测配置尚未纳入发布清单。日常默认仍为关闭。

## 导出与数据边界

允许导出的 span 名仅有 `task.accepted`、`task.dispatch`、`worker.tick`、`model.generation`、`model.result_reused`、`retrieval.compile`、`tool.read`、`sandbox.verify`，且 instrumentation scope 必须是项目自己的 `agent-py`。应用端只添加选定字段；处理器在排队前重建干净 span，剔除其余属性、事件、未允许的 links、status 文本和资源元数据。

不导出任务目标、查询原文、证据正文、源代码、补丁、模型输出、异常信息、headers、原始业务 ID 或凭证。正文摘要不等于内容授权，因此首期不支持 redacted/input-output 模式。其他 exporter（包括本地 JSONL）仍执行各自白名单，不能认为 Langfuse 过滤器会替它们脱敏。

排队内容仅为净化后的 protobuf，每项最多 16 KiB；队列默认 256、最多 4096 项，另有一个在途请求。满队列、新请求发生在关闭后、超出大小或净化失败时丢弃遥测，不阻塞业务。导出线程使用一次 HTTP 请求，不重试、不跟随跳转、不继承代理环境；响应限制 64 KiB，非 2xx、无效 protobuf 或 OTLP partial rejection 都计为导出失败。

请求 I/O timeout 默认 2 秒，关闭最多等待 3 秒后丢弃剩余队列；在途 daemon 线程可能继续至 I/O 结束。该 timeout 不是网络请求的绝对墙钟终止保证。正常 CLI 退出、API lifespan 和 Worker shutdown 关闭 provider；SIGKILL 可能丢失未导出数据，禁止重跑业务来补 trace。

Prometheus 指标 `agent_langfuse_spans_total{result=...}` 包含 `queued`、`exported`、`filtered`、`dropped`、`sanitization_failed`、`export_failed`、`correlation_failed`。`exported` 只表示接收端返回成功 OTLP 应答，不证明平台页面已经完整可见。没有把任务/租户 ID 放入新增指标标签。

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

本地测试通过真实 Harness、读取重试策略和 VerificationRunner，使用 MockTransport 与替代沙盒验证阶段语义和净化结果；没有执行真实企业服务请求或 Docker 回归。写动作的提议/审批/派发/UNKNOWN/确认观测仍待实现，不能把只读工具 span 当作完整动作生命周期。

## 验证与待办

```bash
.venv/bin/pytest tests/test_langfuse.py tests/test_trace_context.py tests/test_stage_observations.py -q
env AGENT_TEST_TEMPORAL=1 .venv/bin/pytest tests/test_trace_context.py -m integration -q
env AGENT_TEST_POSTGRES=1 .venv/bin/pytest tests/test_postgres.py -q
```

测试使用实际锁定 SDK 的属性编码、实际 OTel span/protobuf 和 MockTransport，不需要外部凭证。覆盖线程上下文、跨租户过滤、多 exporter 数据边界、复用不重复计费、未知 usage、容量丢弃、超时关闭、平台失败/跳转/partial rejection 及 CLI 清理。Python CI 安装 langfuse extra 后执行这些测试；缺少可选 SDK 的常规环境会显式跳过此测试模块。

LF-01 仍待：真实 Langfuse OTLP 联调和三类真实模型轨迹、写动作生命周期 observation、采样、完整项目权限与保留/删除策略、自托管服务/镜像摘要锁定、部署/断网/吞吐与 P95 性能验收。当前不能用这份基础代码宣称完成完整 LF-01，更不能宣称策略质量已经提高。

官方依据：[SDK 与 OTel](https://langfuse.com/docs/observability/sdk/overview)、[现有 OTel 集成](https://langfuse.com/faq/all/existing-otel-setup)、[Python API 参考](https://python.reference.langfuse.com/langfuse)。实际编码以锁定 4.15.4 源码及 wire-format 测试为准。
