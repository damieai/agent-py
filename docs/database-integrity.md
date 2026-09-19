# 租户关联完整性与迁移

`0009_tenant_references` 在数据库层检查 `(tenant_id, parent_id)`，防止应用缺陷、脚本或旧 Worker 写入跨租户引用与孤立记录。它与 PostgreSQL FORCE RLS 共同生效：RLS 控制可见和可写的行，复合外键控制记录之间的关联。SQLite 开发连接同样开启外键检查。

## 约束范围

| 子记录 | 关联父记录 |
|---|---|
| operations、task_events、outbox、artifacts、reservations、work_leases | tasks |
| documents.task_id | tasks；NULL 表示项目级证据 |
| approvals.operation_id | operations |
| repair_runs.task_id / snapshot_id | tasks / artifacts |
| repair_attempts.run_id | repair_runs |
| repair_attempts.patch_artifact_id / verification_artifact_id | artifacts；未产生时可为 NULL |

共 13 条具名复合外键；tasks、operations、artifacts、repair_runs 增加 `(tenant_id, id)` 唯一键。约束使用 `DEFERRABLE INITIALLY DEFERRED`，允许一个事务中先写子记录再写父记录，在提交前必须全部有效。`flush()` 成功不等于提交成功；无效引用使整笔事务回滚。

删除和更新采用默认 NO ACTION，不级联删除审计记录。有子记录的父记录不能单独删除；这不是不可删除存储，特权用户仍能在一个事务中显式删除整组数据。授权、审批有效期、产物种类以及同租户内的任务语义关联仍由 Service 检查；本迁移不验证 JSON 中的 ID，也未实现留存策略。

## 升级步骤

1. 在维护窗口停止 API 写请求、Worker 和 Dispatcher；备份数据库，在隔离副本预演。该版本需要建唯一键并验证存量数据，不承诺无停机迁移或固定完成时间。
2. PostgreSQL 使用拥有 DDL 权限且能读取所有租户的专用迁移角色（superuser，或表所有者加 BYPASSRLS）。仅表所有者在 FORCE RLS 下仍不足以执行全量预检。运行账号必须继续保持非所有者、非超级用户、无 BYPASSRLS。
3. 配置管理连接的 `AGENT_DATABASE_URL`，运行 `.venv/bin/alembic upgrade head`，随后 `.venv/bin/alembic check`。不要将管理员连接配置用于 API/Worker。
4. 用运行账号检查租户隔离、正常任务创建及审批，再恢复服务。

升级首先锁定相关 PostgreSQL 表并进行全租户预检，锁等待最多 10 秒；约束建立可能取得更强锁并阻塞读取。锁超时或无效数据都会使事务失败。错误只显示表、列与数量，不记录租户 ID 或业务内容。出现 `Invalid tenant references` 时应由数据负责人在受控环境定位数据来源、核实正确归属；不得为了通过迁移直接删除记录或将其改绑其他租户。修正并留存处置证据后重新执行。

SQLite 的批量重建需要在专用迁移连接上暂时关闭外键即时检查。迁移环境使用 `BEGIN IMMEDIATE` 将 DDL、数据复制和版本号纳入一个事务，提交前执行 `PRAGMA foreign_key_check`，失败完整回滚，最后恢复连接检查。业务连接始终开启外键。新建空库、已有数据升级、中途失败、降级再升级均有自动测试。

本迁移必须在线读取数据库，拒绝 `--sql` 离线生成。生产大表的执行时间、锁竞争和容量尚未验收；不能据本地秒级测试推断生产耗时。

## 回滚与限制

`.venv/bin/alembic downgrade 0008_dependency_circuits` 会先移除外键，再移除配套唯一键，保留业务行和 PostgreSQL RLS 策略。回滚撤除了数据库关联保护，应只在受控维护期间执行；本轮已经在带数据的 SQLite 和原生 PostgreSQL 中验证回滚再升级，不代表完整业务灾备或跨版本 Worker 恢复演练。

直接写数据库遇到约束失败时，不得吞掉错误继续使用同一事务。事务必须回滚并调查调用方；正常 HTTP 用户仍应通过 Service 的权限和参数校验获得受控错误，不向客户端暴露数据库异常细节。
