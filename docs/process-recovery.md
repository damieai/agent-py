# Worker 进程故障演练

`make recovery-check` 在 Linux 上运行四个隔离恢复场景，使用真实 SIGKILL、临时 SQLite、本项目独立持久化仿真远端，以及原生 Temporal 本地服务。它验证进程死亡后的账本与恢复边界，不等于企业渠道、生产数据库或生产 SLO 验收。

## 运行与证据

```bash
uv sync --extra dev --frozen
make recovery-check
```

首次运行 Temporal Python SDK 可能下载其本地开发服务器；环境必须允许本地监听端口与子进程。无需 Docker、模型 key 或企业凭证。普通 `pytest` 默认跳过这四个环境依赖场景，不能据默认回归结果声称演练通过。

入口 `scripts/check_recovery.py` 清除继承的 AGENT_* 配置和 pytest 的附加筛选设置，创建唯一 `.runtime/recovery/run-*` 目录，并要求以下证据同时成立：

- pytest 正常退出，JUnit 恰好包含约定的四个用例，没有 skip、error 或 failure。
- 四份对应的场景报告都存在且通过。
- 原生 Temporal 历史文件存在且包含事件；历史实际回放由对应测试完成。
- 受测源码、测试驱动和 uv.lock 的 SHA256 在运行前后相同。

每个目录保存 `junit.xml`、三份提交边界结果、`temporal_restart.json`、`temporal-history.json` 与汇总 `report.json`。汇总含源码与证据文件摘要、总耗时、适用范围及 `production_acceptance=false`。报告不包含真实身份凭证，也不是独立签名或防篡改证明。失败时不复用旧目录或先前的通过结果；进程日志仍可在 pytest 临时目录中排查。

本地最终版本的一次实测：四场景全部通过，门禁总耗时 44.369 秒；审批等待处重启至任务完成 34.461 秒，77 条历史事件回放通过，四个动作各派发一次。这是单次开发环境测量，不是统计 SLO 或跨版本恢复证明。

总运行上限 180 秒；超时或中断会杀掉本次 pytest 所在的新进程组，包含其 Worker 和本地服务器。测试结束也会清理 Worker。注入代码只在 `tests/support/recovery_worker.py`，需要显式 fixture 标记，不会安装进生产 Worker。Bitbucket `custom: recovery-drill` 可单独触发并归档报告，远端流水线仍待实际运行。

## 场景与不变量

| 场景 | 硬杀位置 | 必须观察到的结果 |
| --- | --- | --- |
| `before_remote_commit` | 本地 PENDING 已提交，进入远端适配器但尚未执行 | 本地对账变为 UNKNOWN，远端 effect_count=0；attempts=1，不重派发；取消后仍等待对账 |
| `after_remote_commit` | 仿真远端资源与回执已原子提交，本地尚未记录成功 | 初始 PENDING；独立 Reconciler 读取回执后 SUCCEEDED；effect_count=1、attempts=1；取消能排空并结束 |
| `after_local_commit` | 本地已记录 SUCCEEDED，Activity 尚未返回 | 重开数据库连接后成功仍在；effect_count=1、attempts=1；取消能结束 |
| `temporal_restart` | 原生 Temporal 已记录审批等待 timer，Worker 没有活动租约 | 硬杀后以不同 PID 在同队列启动 Worker；原 Workflow 继续审批与执行，4 个原键动作各派发一次；最终 SUCCESS；完整历史由当前代码 Replayer 通过 |

前三个场景调用真实 `Activities.tick`，但不经过 Temporal 服务器调度 Activity 重试；第四个场景使用原生 Temporal 和独立 Worker 进程，但选择的是已持久化等待的边界。报告分别声明范围，不能组合成“已覆盖所有 Temporal 在途 Activity 崩溃”。

没有手动修改租约到期时间、工作流计时器或 task deadline 来加速演练。恢复端独立重开本地账本和远端文件，验证结果不能依赖父进程内存。重复调用 Reconciler 不会增加副作用；没有回执也不能伪造失败或成功。

## 默认配置的恢复限制

默认 Worker 名额租约为 360 秒，Activity start-to-close 为 300 秒，当前没有 Activity heartbeat 或租约续租。SIGKILL 不执行 finally，租约将保留至自然到期。替代 Worker 提前争抢会得到 `DUPLICATE_TICK`，即使远端回执已由 Reconciler 找回，也不能把这视为 Worker 已恢复继续推进。

本演练特意验证该阻挡，而不是删除租约。它说明默认路径不能保证“健康依赖下任意崩溃后 60 秒恢复”；报告里的耗时只针对相应场景。审批等待边界通常更快，因为 Activity 已退出并释放租约，不能用它的结果代替在途请求的恢复上界。

生产处理未决动作时，应保持 Reconciler 运行，查看 operation_id、回执与 UNKNOWN 的持续时间。无回执且第三方没有原键查询/恢复协议时，继续人工核对，不能重建新 operation_id 再执行。取消仅停止后续派发，不能抹掉已提交的副作用或未决责任。

## 剩余验收

- PostgreSQL 与真实第三方渠道上的同类硬杀边界、网络分区、ACK 丢失与远端原键恢复协议。
- 原生 Temporal 在途 Activity 的自然超时与租约到期全过程，以及更快检测/续租设计的安全性。
- 旧版本历史对新 Worker 的兼容性、版本化路由与发布排空。当前 Replayer 只证明同版本代码可回放本次历史。
- Temporal 服务本身重启、数据库备份恢复、生产负载下 P95/P99 与 60 秒恢复 SLO。
