# 开发和试点运行手册

## 未决动作

通过任务详情或 `GET /api/v1/operations/{id}` 检查状态。UNKNOWN/PENDING 不能重新创建新动作。Dispatcher 的独立 Reconciler 即使任务已取消仍查询外部结果。当前未实现人工裁决 API，不能直接用 SQL 把 UNKNOWN 改成成功。

当外部无法确认时，保留动作和回执证据、负责人及沟通记录；不得删除再执行。超过 15 分钟自动标记 `MANUAL_REVIEW` 并记录升级事件；外部告警投递尚未接入，需要操作人员监控工作台。

## 紧急停用与撤权

`agent-py emergency-stop --tenant demo` 停止该租户新派发，保留对账。`--no-stopped` 恢复派发。操作命令属于受信管理入口，不对模型或普通用户暴露。

在途请求可能已经成功，撤权不能撤销这些事实。停止后先对账，再决定新的补偿动作。生产配置和 Grant 管理由部署管理员负责；当前无完整管理 UI。

## 数据迁移

在仓库根目录运行 `alembic upgrade head`。`agent-py init` 仅用于开发并走同一迁移链。不要在生产调用 `Base.metadata.create_all`。

生产使用表所有者作为迁移账号，运行时账号仅具有所需 DML 权限。RLS 不保护超级用户；运行时启动检查会拒绝这些角色。数据库变更应先加兼容字段，再迁移数据，最后在所有旧 Worker 排空后移除旧字段。

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
