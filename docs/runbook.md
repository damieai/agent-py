# 开发和试点运行手册

## 未决动作

通过任务详情或 `GET /api/v1/operations/{id}` 检查状态。UNKNOWN/PENDING 不能重新创建新动作。Dispatcher 的独立 Reconciler 即使任务已取消仍查询外部结果。当前未实现人工裁决 API，不能直接用 SQL 把 UNKNOWN 改成成功。

当外部无法确认时，保留动作和回执证据、负责人及沟通记录；不得删除再执行。当前版本尚未实现 15 分钟自动升级告警，需要操作人员监控。

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
