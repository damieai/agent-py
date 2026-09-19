# 候选补丁验证

`agent-py verify-patch TASK_ID SOURCE_DIRECTORY PATCH_JSON` 将选定源码复制到一次性工作目录，验证源摘要并应用补丁，在容器中分别运行基线和候选，最后保存 `candidate-verification` 产物。源目录不被修改，候选代码不会在宿主 Python 中执行。目前只支持 `src/` 内 Python 文件，不安装候选依赖，不读取仓库测试配置。

前置条件：Docker Engine 可用；本地已存在包含 Python 和 pytest 的受控镜像；`AGENT_SANDBOX_IMAGE` 使用完整 `name@sha256:...`，禁止运行时拉取。`AGENT_SANDBOX_ORACLE` 指向运维管理的验收目录，必须位于候选源码目录之外。工作目录由 `AGENT_SANDBOX_ROOT` 指定，应仅允许运行账户写入，不能与不可信进程共享或并发修改输入。

示例：创建一个 repair 任务，配置镜像与独立 oracle 后执行：

```bash
export AGENT_SANDBOX_ORACLE="$PWD/examples/oracle"
agent-py verify-patch TASK_ID examples/demo_service examples/queue-fix.patch.json
```

补丁 JSON 是 FileEdit 数组，包含 `path`、原文件的 `original_sha256` 和替换内容 `content`。最多 20 个 Python 源文件、1 MB 替换内容。快照每份最多 200 个 Python 文件、2 MB，拒绝符号链接和特殊文件。

容器使用无网络、只读根目录、独立只读源码及 oracle 挂载、非 root 用户、移除全部 capabilities、禁止新增权限、CPU/内存/PID 限制。可写 `/tmp` 为 128 MB tmpfs，`/dev/shm` 为 16 MB。关闭 Docker 日志驱动，将附加输出直接读入受限内存；输出超过 128 KB 或运行超过 600 秒时请求终止容器。运行器不会把任意体积输出写入宿主临时文件。容器终止失败会传播错误，需按容器名检查守护进程。

pytest 在隔离 Python 启动模式下预先导入，再添加候选源码路径；禁止插件自动加载、仓库配置及 conftest，只执行 `/oracle` 下的用例。**候选模块与 Python oracle 仍处于同一进程**，恶意模块可能干扰验收器。因此这是一套回归验证机制，不是对抗性正确性证明；其产物不能直接满足业务成功条件，也不授权合并或部署。后续需要独立进程黑盒 oracle 和真实容器红队验收。

判定：基线退出码为 1、候选为 0 且两次都未触及限制时为 `REGRESSION_FIXED`；基线为 0 则 `BASELINE_NOT_REPRODUCED`；候选失败为 `CANDIDATE_FAILED`；环境异常、超时、输出超限或其他退出码为 `INCONCLUSIVE`。基础设施无法启动时直接报错，不生成成功结论。

容器验收命令：

```bash
AGENT_TEST_SANDBOX_IMAGE="$AGENT_SANDBOX_IMAGE" .venv/bin/pytest tests/test_verification.py -m sandbox -q
```

当前环境缺少可用 Docker 引擎，该测试尚未执行。现有通过记录来自快照/边界单元测试、固定可信子进程的超时/输出测试及模拟容器结果，不能替代真实隔离验收。
