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

tenant、task session、推理身份使用独立密钥的 HMAC-SHA256 伪名，域包含环境和租户。相同密钥/绑定下可关联重启后的执行段；密钥轮换改变伪名。当前没有持久化 trace 索引、工作台跳转或反查 API，不把伪名误称为平台 ACL。

启用仅影响观测，不改变模型请求、授权、预算或 AgentRelease 的业务运行策略。配置与密钥须单独管理；观测配置尚未纳入发布清单。日常默认仍为关闭。

## 导出与数据边界

允许导出的 span 名仅有 `worker.tick`、`model.generation`、`model.result_reused`，且 instrumentation scope 必须是项目自己的 `agent-py`。应用端只添加选定字段；处理器在排队前重建干净 span，剔除其余属性、事件、links、status 文本和资源元数据。

不导出任务目标、查询原文、证据正文、源代码、补丁、模型输出、异常信息、headers、原始业务 ID 或凭证。正文摘要不等于内容授权，因此首期不支持 redacted/input-output 模式。其他 exporter（包括本地 JSONL）仍执行各自白名单，不能认为 Langfuse 过滤器会替它们脱敏。

排队内容仅为净化后的 protobuf，每项最多 16 KiB；队列默认 256、最多 4096 项，另有一个在途请求。满队列、新请求发生在关闭后、超出大小或净化失败时丢弃遥测，不阻塞业务。导出线程使用一次 HTTP 请求，不重试、不跟随跳转、不继承代理环境；响应限制 64 KiB，非 2xx、无效 protobuf 或 OTLP partial rejection 都计为导出失败。

请求 I/O timeout 默认 2 秒，关闭最多等待 3 秒后丢弃剩余队列；在途 daemon 线程可能继续至 I/O 结束。该 timeout 不是网络请求的绝对墙钟终止保证。正常 CLI 退出、API lifespan 和 Worker shutdown 关闭 provider；SIGKILL 可能丢失未导出数据，禁止重跑业务来补 trace。

Prometheus 指标 `agent_langfuse_spans_total{result=...}` 包含 `queued`、`exported`、`filtered`、`dropped`、`sanitization_failed`、`export_failed`。`exported` 只表示接收端返回成功 OTLP 应答，不证明平台页面已经完整可见。没有把任务/租户 ID 放入新增指标标签。

## 验证与待办

```bash
.venv/bin/pytest tests/test_langfuse.py -q
```

测试使用实际锁定 SDK 的属性编码、实际 OTel span/protobuf 和 MockTransport，不需要外部凭证。覆盖线程上下文、跨租户过滤、多 exporter 数据边界、复用不重复计费、未知 usage、容量丢弃、超时关闭、平台失败/跳转/partial rejection 及 CLI 清理。Python CI 安装 langfuse extra 后执行这些测试；缺少可选 SDK 的常规环境会显式跳过此测试模块。

LF-01 仍待：真实 Langfuse OTLP 联调和三类真实模型轨迹、API→Outbox→Temporal 跨进程上下文与持久化关联、检索/工具/沙盒阶段 observation、采样、完整项目权限与保留/删除策略、自托管服务/镜像摘要锁定、部署/断网/吞吐与 P95 性能验收。当前不能用这份基础代码宣称完成完整 LF-01，更不能宣称策略质量已经提高。

官方依据：[SDK 与 OTel](https://langfuse.com/docs/observability/sdk/overview)、[现有 OTel 集成](https://langfuse.com/faq/all/existing-otel-setup)、[Python API 参考](https://python.reference.langfuse.com/langfuse)。实际编码以锁定 4.15.4 源码及 wire-format 测试为准。
