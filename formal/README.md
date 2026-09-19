# 动作协议的有限模型

`Action.tla` 建模一个动作的批准、提交、响应丢失、外部去重与取消。`IdempotentRemote` 是外部保证，不能由本地锁代替。

安装 Java 和 TLC 后执行：

```bash
java -cp /path/to/tla2tools.jar tlc2.TLC -config formal/Action.cfg formal/Action.tla
java -cp /path/to/tla2tools.jar tlc2.TLC -config formal/UnsafeRemote.cfg formal/Action.tla
```

第二个配置预期产生重复副作用反例。模型只覆盖一个稳定动作身份和外部原子去重；尚未覆盖双 Worker、审批撤销、租约 fencing、跨任务业务重复或活性，因此不构成整个系统的证明。运行记录须另行保存，文件存在不表示已经通过 TLC。
