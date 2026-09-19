# 事件时间线与离线审计

任务详情现在显示实时事件时间线，并可下载 `recording-v2` 审计包。包内保存一个一致性快照的任务状态、动作请求及回执、审批摘要、连续事件和产物清单，供离线排查和确定性动作回执回放使用。

## 工作台与 SSE

前端使用带 Bearer Header 的 fetch 流，避免把凭证放进 URL。按 `Last-Event-ID` 续传，重复序号去重，发现跳号停止；连接中断后从最后完整接收的事件重试，间隔从 1 秒退避到 15 秒。更换任务或凭证会取消旧流。页面仅保留最近 200 条事件，完整记录通过审计包查看。

后端每批最多 200 条，并在新的数据库快照内检查 JWT 有效期、项目/环境范围和当前 Grant。批量历史直接追赶，空闲时发送 heartbeat；每个连接最多处理 120 批，然后允许客户端续传。数据库查询在线程中执行，避免长连接阻塞 API 事件循环。

协议约定：

| 信号 | 客户端行为 |
|---|---|
| 带 `id` 的任务事件 | 按连续序号接收，展示类型及 JSON payload |
| `stream.closed` | 当前任务已终止且事件追赶完成，停止重连 |
| `access_revoked` 或 HTTP 401/403/404 | 清空本面板事件并停止，需用户更新凭证或授权 |
| `history_unavailable` 或 HTTP 409 | 历史缺口或游标超前，停止并提示重新打开任务 |
| 断线、普通 EOF 或暂时性服务错误 | 保留最后完整事件游标并退避重连 |

历史缺口不会被补造成成功事件。重新打开任务仍报缺口时需要运维检查数据库和备份，不能通过删除审计行“修复”。每批已经读取的数据无法在发送后撤回；长连接授权复核不是瞬时撤回已发内容的保证。HTTP 响应头时延也不等于 SSE 生命周期。

前端解析器支持跨字节 UTF-8、CRLF/LF、注释心跳和多行 data，未完成的帧不推进游标，帧缓存上限约一百万字符。当前消费的是本项目事件协议；没有宣称实现所有第三方 SSE 协议扩展。Node 24 的 7 项解析/流读取测试已接入 CI，浏览器页面级 E2E 仍待完成。

## 导出与验证

```bash
.venv/bin/agent-py export TASK_ID audit.json
.venv/bin/agent-py audit-check audit.json
```

HTTP 下载入口为 `GET /api/v1/tasks/{id}/recording`，要求当前任务授权；候选修复额外要求任务发起人或 operator。API 使用 `no-store` 和附件下载响应，不接收上传包或执行包内内容。

PostgreSQL 使用只读 REPEATABLE READ 事务，SQLite 显式 BEGIN，确保任务、事件与相关记录来自同一数据库快照。读取结束再次复核当前任务授权，捕获期间已提交的撤权。快照不会锁住 PostgreSQL 任务写入；已用并发取消验证 MVCC 一致性和 RLS。记录中的 `mode` 是导出进程的执行配置，不是签名的历史部署证明。

导出上限为 100 个动作、100 个审批、10000 条事件、1000 个产物清单项和 10 MB JSON，超限拒绝，不静默截断。产物仅导出 ID、种类和摘要，不包含正文、文件存储路径、模型提示词快照或容器日志。动作参数、回执、任务目标及事件仍可能包含业务敏感内容，向租户外分享前须检查。

`audit-check` 只读取指定本地文件，不构建 Service、不读取生产数据库或请求供应商。它检查 JSON 类型/上限、包摘要、连续事件序号、重复身份、动作提议与派发、尝试次数、最终动作状态、审批摘要及派发前批准事件、已成功回执的 `confirmed=true`、任务终止和成功验证清单。UNKNOWN/PENDING 会明确列为 unresolved，不作为文件损坏，也不会被判定为成功。

摘要提供完整性检查，**不是签名或真实性证明**。攻击者可同时重写数据和摘要；当前还没有外部签名、可信时间戳或不可改写审计存储。审批的 payload digest 包含原始证据引用，当前动作表没有保存完整引用，校验器只能核对审批和动作已存摘要的一致性，不能从导出的请求独立重建原批准对象。检查也不证明审批当时的权限和有效期、产物正文正确性或真实远端状态。

报告固定返回 `remote_state_verified=false` 和 `artifact_contents_verified=false`。`fully_confirmed_dispatches` 只表示所有已派发动作有成功回执记录；空派发集合也为 true，不代表任务完成。

## 严格动作回执回放

```python
import json
from agent_py.audit import check_recording
from agent_py.replay import ReplayExecutor

recording = json.load(open("audit.json"))
report = check_recording(recording)
replay = ReplayExecutor(recording)
by_id = {op["id"]: op for op in recording["body"]["operations"]}
for operation_id in report["dispatch_order"]:
    op = by_id[operation_id]
    result = replay.execute(report["tenant"], operation_id, **op["request"])
replay.assert_consumed()
```

v2 回放要求租户、下一个派发身份及规范化请求摘要完全一致；乱序、重复或未消费完都失败。JSON 的整数和布尔值不视为同一参数。UNKNOWN、PENDING、FAILED 没有可回放的成功回执，执行会停止且不推进游标；不查询生产系统补齐，也不推断失败动作是否产生副作用。

保留原 `export_recording` 库函数和 v1 按身份查询兼容性；v1 没有租户绑定或严格顺序完成检查，新 CLI/API 默认使用 v2。当前不是对模型输出、工具请求时间序列、Temporal 调度或整个 Agent 轨迹的重新执行；这些需要更完整的录制契约。审计包也不构成业务成功的独立验收器。
