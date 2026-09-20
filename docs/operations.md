# 调度与运行观测

本阶段提供数据库共享准入、运维概览、Prometheus 指标和本地 OTel JSONL 导出。生产容量、远端执行隔离和告警送达需要单独验收。

## 启动与配置

1. 使用迁移身份执行 `.venv/bin/alembic upgrade head`，当前版本为 `0010_dependency_bulkhead`。PostgreSQL 应用角色须拥有新表的读写权限，且不能是表所有者或绕过 RLS 的角色。
2. 按 `.env.example` 配置 API、Dispatcher 和 Worker。API 实例的 `AGENT_MAX_QUEUE_PER_TENANT` 必须一致；创建任务会在租户锁内检查队列容量和幂等键。
3. 启动 `agent-py api`、`agent-py dispatcher --tenant demo`、`agent-py worker`。首次 Worker 准入将 `AGENT_MAX_ACTIVE_PER_TENANT` 写入共享策略，后续以数据库值为准。
4. 运维可执行 `agent-py admission-limit demo 4` 修改租户上限。降低上限保留已运行租约，只阻止后续超额领取。此命令是持有数据库配置的本地管理入口，应限制主机访问权限。
5. 工作台“查看运行概览”或 `GET /api/v1/ops/summary` 要求 operator 身份，并按当前项目、环境和未撤销 Grant 过滤。`agent-py ops-status` 使用本地演示 operator 范围。

## 租约与故障语义

租户 gate 行锁串行化领取；排队票据按入队时间和 ID 排序。同一任务只能有一个有效执行 token，等待票据 90 秒过期，活跃轮询会续期。公平性限于仍然轮询的票据，不是跨租户加权公平调度。租户内行锁也是潜在吞吐瓶颈，需要容量测试。

默认执行租约 360 秒，配置范围 330—900 秒，高于当前 Activity 的 300 秒超时。Worker 单进程 Activity 上限默认为 8；跨进程租户上限由数据库维护。时钟需同步。完成或等待人工输入时释放容量；Activity 取消后后台线程可能仍运行，因此保留租约直至过期。停止和接管任务可进入控制处理，不受新执行容量阻塞。

执行入口核验 token 和有效期，旧 Worker 不能释放新 token，也不能用迟到错误覆盖新任务状态。租约不能撤销已发出的外部请求，过期后也不保证旧线程立即终止；远端执行仍需幂等身份、权威查询及供应方 fencing 协议。直接 CLI/library 调用不经过 Worker 准入，不应作为生产流量入口。

Dispatcher 按租户和阶段隔离异常；一个租户启动失败不会跳过其他租户及对账阶段。对账每批 100 条并轮转游标，避免前 100 条长期 UNKNOWN 阻塞后续记录。游标在进程内，重启后从头扫描。

## 指标和 trace

配置至少 32 字符的独立 `AGENT_METRICS_SECRET`；API `/metrics` 使用 Bearer 凭证，普通用户 JWT 不适用。`AGENT_MONITORING_TENANTS='["demo"]'` 明确指定允许导出的完整租户，是运维级权限。默认空列表不导出租户数据。工作台查询仍受用户授权过滤，两者统计范围不同。

Worker 另设 `AGENT_WORKER_METRICS_ENABLED=true`，默认监听 `127.0.0.1:9465`，使用同一独立监控凭证，仅提供该进程计数。多 Worker 应分别抓取不同实例端点。监听公网地址前应配置网络访问控制及 TLS 代理；内置端点不负责 TLS。不要将监控凭证放进前端或提交仓库。

API 的任务、操作、准入和预算 gauge 来源于共享数据库，多 API 副本请按租户/状态取 `max`，不能相加。请求和 Worker counter/histogram 是进程级，可跨实例汇总 rate。当前没有 Python multiprocess registry，共用一个监听端口的多 API 子进程不能代表全部请求计数；部署为每端点一个进程并分别抓取。

请求标签仅记录方法、路由模板和状态码；不会把任意 URL、任务 ID 或提示词作为指标标签。HTTP 时延测量到响应创建，**不是 SSE 完整连接时长**。今天费用按 UTC 预算日统计，单位为 micro USD；UNKNOWN 费用保留为预留，不冒充已结算。

设置 `AGENT_TRACE_FILE` 可启用 OTel SDK 本地 JSONL 导出。每个实例使用 PID 和随机后缀文件名，每文件约 2 MB、3 份备份、权限 0600。多次重启会产生新文件组，需运维设置目录总量及保留期限。同步文件导出会增加请求开销。只导出允许的属性及异常类型，不导出异常正文、事件正文、模型提示词或源码；tenant/task ID 仍属于受控元数据。

API 返回 `X-Trace-ID`，同一 span 中写入的任务事件携带 trace ID。Worker tick 有独立 span；尚未实现 HTTP→Outbox→Temporal 的完整跨进程父子链和 OTLP Collector 导出。

## Langfuse 接入计划（planned）

已选定 Langfuse 扩展模型调用、逐轮上下文、候选验证、提示词版本与效果分析。当前仍只有上述本地导出，尚无 Langfuse 配置或远端数据上传；实现清单见 [LF-01—LF-04](langfuse.md)。

LF-01 需先处理现有独立 `TracerProvider` 与 SDK 的关联，以及 API→Outbox→Activity 的上下文传播；按任务 session 关联各执行段，等待期间不维持长 span。推理结果复用单独记录，避免虚增调用和费用。Prometheus 继续负责准入、熔断、队列及服务告警。

运维验收需提供独立观测服务部署、租户与环境项目隔离、默认元数据模式、去敏内容的显式出站策略、队列上限和关闭 flush 时限。分别测试断网、错误凭证、队列满、硬杀与恢复，记录遥测丢失和启用前后的开销；平台故障不重试业务动作，不要求重新付费推理来补轨迹。提示词使用随发布固定的本地快照，评测证据缺失则阻止实验通过。

## 监控示例与处置

`ops/prometheus.yml` 假设 Prometheus 与应用在同一宿主机，需替换地址并在 `/etc/prometheus/agent-metrics-token` 放置只含凭证的私密文件。容器内的 localhost 不指向宿主机。`ops/alerts.yml` 和 `ops/grafana-dashboard.json` 是待部署模板，未在真实 Prometheus/Grafana 上验收，也未配置 Alertmanager 接收人。可在安装后运行 `promtool check config ops/prometheus.yml` 和 `promtool check rules ops/alerts.yml`。

| 信号 | 排查与操作 |
|---|---|
| UNKNOWN/PENDING 超过 15 分钟 | 查工作台动作身份和人工升级事件，检查 Reconciler 与供应方权威状态；禁止更换身份重发 |
| Outbox 持续积压 | 检查 Temporal 可达性、Namespace、Dispatcher 租户配置及启动异常类型；恢复后幂等补发 |
| 等待准入持续积压 | 查看租户策略、Worker Activity 限额、长时间运行请求与遗留租约；先定位瓶颈，再调整上限 |
| API 错误率或时延上升 | 使用响应 trace ID 定位路由和异常类型，检查数据库连接、锁等待；不要以 SSE 头部时延判断流式质量 |
| 预算预留不释放 | 查询推理回执及 UNKNOWN 身份；没有权威证据时不能把未知费用清零 |

示例告警阈值是运维起点，尚未证明目标 SLO。生产上线仍需负载测试、告警送达演练、账单校准、迁移回滚和备份恢复。

企业证据读取增加共享熔断、活动读取名额/上限以及容量拒绝指标，已加入告警及 Grafana 模板；租户级数据库 gauge 继续按 max 聚合。具体恢复命令和界限见[依赖故障治理](dependency-resilience.md)。

启用发布固定后，Outbox 按发布摘要分流。积压时同时核对任务 release_id、对应 Dispatcher/Worker 的独立 pin 和专属队列；禁止修改任务发布 ID 强行迁移。启用前需要先升级或停止不识别版本的旧 Dispatcher，详见[发布操作步骤](releases.md)。

启用发布签名后，readiness 503 或 RELEASE_SIGNATURE_INVALID 应检查签名到期、公钥有效期、撤销状态、audience/运行环境和各副本信任文件分发。先续签/恢复可信策略，再恢复原 Outbox；不要换动作身份重发。轮换步骤与无法强制中断在途请求的边界见[发布签名说明](release-signing.md)。
