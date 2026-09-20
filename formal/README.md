# 可执行的有限安全模型

`make formal-check` 运行两个正向配置和四个故意关闭保护的负向配置。所有配置达到预期才返回 0；负向检查要求 TLC 退出码 12、指定不变量违例、至少两个状态的反例和完整结束标记。语法错误、超时、其他不变量失败、负向配置意外通过均不能算成功。

## 工具与复现

需要 Python 项目环境和 Java 11+。TLC 固定使用上游 [v1.7.4 发布](https://github.com/tlaplus/tlaplus/releases/tag/v1.7.4)的 jar（工具自身版本为 2.19）；`toolchain.json` 固定来源和 SHA-256。首次下载时已核对发布页 SHA-1 `bee4a54f3ee3d4afc347c3240ec2d9e93b075104`。更新版本须重新核对上游来源并运行全部正负配置，不能仅替换摘要。

```bash
mkdir -p .runtime/formal-tools
curl --fail --location --retry 3 --max-time 120 \
  https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar \
  --output .runtime/formal-tools/tla2tools.jar
TLC_JAR=.runtime/formal-tools/tla2tools.jar make formal-check
# 非标准 Java 安装可另设 JAVA=/absolute/path/to/java
```

脚本执行 jar 前校验 SHA-256，不自行下载或安装工具。固定单 Worker、seed=1、指纹多项式 0、512 MB 堆和每配置 60 秒超时，使用独立临时目录。TLC 会初始化本地 RMI 监听，严格沙箱可能需要允许本地监听。运行期间 Python 不将完整日志载入内存；解析上限 2 MB，超限失败。日志文件本身不设磁盘配额。

`.runtime/formal/report.json` 保存工具/JVM 版本、模型与配置摘要、生成/独立状态数、预期违例和反例动作；各配置的 `.log` 保存完整状态轨迹。开始运行即将旧报告标记为未完成失败，避免工具缺失或摘要错误留下旧 PASS。`verification.json` 是 2026-09-20 本机实测快照，不是签名证明；默认测试检查其输入摘要是否仍与当前模型匹配。模型变更后实际重跑 TLC，再用新报告替换快照。Bitbucket 独立步骤重新检查全部模型并保留报告和日志；远端流水线尚未实测。

## 模型和实现映射

`Action.tla` 覆盖一个稳定动作身份的批准、提交、响应丢失、远端原子去重及取消。`RemoteCommit` 的重复执行抽象传输重放；它不表示应用层主动重复派发。`IdempotentRemote` 是远端协议假设，本地锁不能提供该保证。

`WorkerLease.tla` 覆盖两个 Worker、一个任务、最多两次租约获取。数值 epoch 抽象不可复用的 owner token；实现使用 UUID，不把这个数值传给远端。Expire 抽象时间推进，Acquire/Release/Dispatch/Complete 各自视为原子转换。Dispatch 表示本地执行资格检查，Complete 仅表示带租约校验的 Worker 错误状态更新，不泛指所有外部回执。pending 保存派发时 token，因此同一 Worker 重新获租也不能接受旧回调。

| 配置 / 实测独立状态 | 预期结果与最短反例动作（省略 Init） | 实现回归 |
| --- | --- | --- |
| Action / 14 | 安全不变量通过 | `tests/test_state_machine.py::test_remote_idempotency_under_arbitrary_retry_sequences` |
| UnsafeRemote / 9 | AtMostOneEffect：Approve → Dispatch → RemoteCommit → RemoteCommit | 同上；`tests/test_execution.py::test_response_loss_and_lag_do_not_duplicate` |
| WorkerLease / 37 | 三个租约安全不变量通过 | `tests/test_scheduling.py` |
| UnsafeDispatch / 9 | NoStaleDispatch：Acquire → Expire → Dispatch | `test_formal_expired_dispatch_trace_without_replacement` |
| UnsafeRelease / 17 | NoWrongRelease：Acquire(w1) → Expire → Acquire(w2) → Release(w1) | `test_stale_worker_cannot_dispatch_or_release_new_owner` |
| UnsafeCompletion / 19 | NoStaleCompletion：Acquire → Dispatch → Expire → Complete | `test_formal_expired_completion_trace_without_replacement`；替代 Worker 情形见 `test_late_worker_error_does_not_overwrite_replacement_state` |

后三行测试均在 `tests/test_scheduling.py`。负向模型描述保护被删除后的行为，回归测试在实际 SQLite Service/Activities 上执行对应事件序列并断言被拒绝；不为生产代码添加关闭保护的开关。映射是人工维护的抽象对应关系，没有自动 refinement proof，也没有自动把任意 TLC 轨迹翻译为实现调用。

## 证明边界

这是有限状态空间安全检查，没有 fairness、活性或无限 epoch 证明。每个转换的原子性是模型假设，不证明数据库隔离或“本地检查到远端执行”的整个区间原子。过期租约不能终止在途请求，远端副作用仍依赖远端幂等或 fencing；独立 reconciler 必须继续接受有效的迟到外部证据，不能套用 Worker 错误回调的丢弃规则。

尚未覆盖审批撤销、跨任务业务重复、多租户容量、数据库故障、跨进程时钟偏差和整个系统的精化关系。两个模型独立检查，没有证明它们组合后的全部性质。直接 CLI/library 调用没有 Worker ContextVar，属于租约模型范围之外。TLC 的指纹状态存储也不是数学证明证书。
