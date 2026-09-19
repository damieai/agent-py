# 开发和试点运行手册

## 未决动作

通过任务详情或 `GET /api/v1/operations/{id}` 检查状态。UNKNOWN/PENDING 不能重新创建新动作。Dispatcher 的独立 Reconciler 即使任务已取消仍查询外部结果。当前未实现人工裁决 API，不能直接用 SQL 把 UNKNOWN 改成成功。

当外部无法确认时，保留动作和回执证据、负责人及沟通记录；不得删除再执行。超过 15 分钟自动标记 `MANUAL_REVIEW` 并记录升级事件；外部告警投递尚未接入，需要操作人员监控工作台。

## 紧急停用与撤权

`agent-py emergency-stop --tenant demo` 停止该租户新派发，保留对账。`--no-stopped` 恢复派发。操作命令属于受信管理入口，不对模型或普通用户暴露。

在途请求可能已经成功，撤权不能撤销这些事实。停止后先对账，再决定新的补偿动作。生产配置和 Grant 管理由部署管理员负责；当前无完整管理 UI。

## 数据迁移

在仓库根目录运行 `alembic upgrade head`。`agent-py init` 仅用于开发并走同一迁移链。不要在生产调用 `Base.metadata.create_all`。

生产使用独立迁移账号，运行时账号仅具有所需 DML 权限。`0009_tenant_references` 需要迁移角色具有 DDL 权限及全租户可见性（superuser 或所有者加 BYPASSRLS），以检查存量跨租户关联。RLS 不保护这些角色；运行时启动检查会拒绝它们。维护窗口、备份、坏数据处理和回滚步骤见[数据库完整性说明](database-integrity.md)。数据库变更应先加兼容字段，再迁移数据，最后在所有旧 Worker 排空后移除旧字段。

## 备份与恢复验收步骤

1. 停止新派发，记录活跃任务、未决动作和工作流版本。
2. 使用独立备份账号备份 PostgreSQL、产物和外部持久化工作流服务。
3. 在隔离网络恢复，先验证租户权限和产物摘要，再启用只读查询。
4. 用稳定动作身份与外部系统对账；禁止直接重放所有写队列。
5. 验证旧 Worker 版本可运行、未决动作有负责人，再恢复受限派发。

这些是待演练步骤；仓库当前没有 RPO/RTO 实测达标结论。不要把开发 Compose volume 的存在当作已完成灾备。

## 签名 Connector

为部署实例配置固定 `AGENT_WEBHOOK_TENANT` 与至少 32 字符的 `AGENT_WEBHOOK_SECRET`。请求体只有 `event_id` 与 `operation_id`，签名为 `HMAC-SHA256(secret, timestamp + '.' + raw_body)`，放在 `X-Agent-Signature`；`X-Agent-Timestamp` 为 Unix 秒，允许五分钟偏差。

这是本项目受信 Connector 的协议，不是 Jira/Bitbucket 原生 Webhook 验签协议。Connector 必须先完成供应商身份验证，再转换成此通知；当前没有实现那个桥接服务。通知只触发查询，不携带可信成功结果。

## 本地环境限制

本次环境 Docker Desktop 未接入 WSL，无法验收容器镜像、沙盒系统调用隔离与 kind 集群。PostgreSQL 和 Temporal 可通过原生测试服务验证。前端构建通过不代表浏览器 E2E 已通过。
## 接管与恢复

候选修复的专用配置、状态、验证 token 恢复与人工重试见[候选修复操作说明](repair-workflow.md)。新迁移增加 `repair_runs`、`repair_attempts` 两张 RLS 表；部署前需将它们纳入运行角色授权和备份范围。

当前版本的非终态阻塞、人工接管和人工审阅保持 Workflow 活跃，按等待间隔重新检查；相同阻塞原因不重复产生事件。操作员可在工作台恢复接管任务，或先 GET 任务取得 `version`，再 POST `/api/v1/tasks/{id}/resume`，传入 `{"expected_version": 2}`（使用实际版本）。409 表示状态已变化，需要重新检查；取消及终态任务不能恢复。授权、截止时间和紧急停用仍生效。

审批过期且动作尚未提交时，仿真执行路径将动作标为 `APPROVAL_EXPIRED` 并结束任务。没有自动续期，也不会复用旧批准重新派发。`MANUAL_REVIEW` 表示未确认时间超过 15 分钟；应查询外部权威并检查原 operation_id，不手工创建重复动作。独立对账继续运行，晚到的确认成功仍可收敛。

升级限制：旧版本已经关闭的 Workflow 不会因部署新代码自动重新打开。此版本的恢复针对仍活跃的 Workflow；历史关闭实例需要单独检查并制定迁移方案。这里尚未完成 Worker 版本迁移和旧历史回放演练。已进入外部 I/O 的动作不能靠接管撤回，必须继续对账。

## 检索策略与证据预览

默认 `AGENT_CONTEXT_STRATEGY=lexical`。准备切换 `bm25_rrf` 时先用离线开发集和 `context-preview` 核验预算内的证据；排空已有只读调查或为新策略新建任务。旧修复流程继续验证冻结策略，推理身份和预算账本不能删除后重发。`EVIDENCE_STALE` 要求检查原文版本、来源或当前授权，`CONTEXT_CAPACITY` 要求收窄证据范围。完整说明见[上下文编译](retrieval.md)。

## 事件流与审计导出

工作台时间线断线后会自动按序号续传；凭证过期或撤权时停止并清空面板。历史缺口需检查数据库，不能静默跳过。运维可下载 v2 审计包，在离线环境执行 `agent-py audit-check`，区分内部一致性、未确认动作与真实远端验证。配置边界及回放示例见[审计说明](audit-replay.md)。

## 企业证据采集依赖

升级 `0008_dependency_circuits` 后，用 `agent-py dependency-status TENANT` 查看共享熔断。`DEPENDENCY_OPEN` 表示冷却或探测期间等待；先检查供应商健康及 Retry-After，再决定是否执行 `dependency-reset TENANT HASH`。不要删除状态行，避免丢失旧许可隔离信息。完整策略和监控见[依赖故障治理](dependency-resilience.md)。

## 审计签名运维

签名默认关闭，启用、离线验证及轮换流程见[审计签名说明](audit-signing.md)。`AUDIT_SIGNING_UNAVAILABLE` 时检查 manifest 范围、密钥有效期及私钥权限；不要以无签名包冒充签名成功。`AUDIT_SIGNATURE_INVALID` 时检查独立信任清单、预期租户/用途、撤销状态和时钟；不要从待验证包导入可信公钥以绕过失败。私钥疑似泄露时撤销旧 ID 并通过独立渠道更新验证者策略。

## 评测门禁失败处置

运行 `make evaluation-gate` 或对已有报告使用 `agent-py evaluation-gate`；命令与格式见[评测门禁说明](evaluation-gates.md)。退出 1 时查看 gate.json 中失败的 checks 和 paired_queries，定位具体协议案例或检索退化；退出 2 时先修复缺失/无效证据、数据集摘要或覆盖范围。不要删除失败案例或临时降低阈值使门禁通过。输入策略和报告版本变更需独立审阅，当前门禁不直接授权发布。

## 企业读取容量等待

`DEPENDENCY_CAPACITY` 表示共享名额已满，先用 `dependency-status TENANT` 查看 active_reads/read_limit，再检查供应商延迟与挂起 Worker。按供应商配额评估后可用 `dependency-limit TENANT HASH LIMIT` 修改共享限额；调低不会驱逐在途读取。不要通过 reset 或删除租约绕开限流。`DEPENDENCY_LEASE_LOST` 表示超过 90 秒或已释放，返回内容不可继续发布。升级 0010 后需授予运行角色新租约表的 DML 权限，回滚再升级时重新授予；详见[依赖故障治理](dependency-resilience.md)。
