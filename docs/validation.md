# 本地验收记录

日期：2026-09-19。环境：Linux / Python 3.12，本地 SQLite、独立原生 PostgreSQL 与 Temporal 测试服务。以下结果只覆盖当前实现，不代表 B00—B12 或生产验收完成。

| 检查 | 命令 | 结果 |
|---|---|---|
| 静态检查及格式 | `make check` | 通过，包含迁移文件 |
| 默认测试 | `.venv/bin/pytest -q` | 121 通过；4 个基础设施测试默认跳过 |
| PostgreSQL | `AGENT_TEST_POSTGRES=1 .venv/bin/pytest tests/test_postgres.py -q` | 1 通过：迁移、非特权角色 RLS、并发预算、共享准入及运维聚合 |
| Temporal | `AGENT_TEST_TEMPORAL=1 .venv/bin/pytest tests/test_runtime.py -q` | 2 通过：Outbox 与本地服务工作流 |
| Temporal 候选修复 | `AGENT_TEST_TEMPORAL=1 .venv/bin/pytest tests/test_repair_workflow.py -m integration -q` | 1 通过：冻结输入到人工审阅，再取消；模型/容器为测试适配器 |
| 前端 | `npm --prefix web run build` | TypeScript 检查与 Vite 构建通过 |
| 开发集执行协议 | `agent-py evaluate --split development --output .runtime/evaluation.json` | 60/60；重复场景模板，不是模型质量评测 |

默认测试包括响应丢失后对账、重复派发、取消、审批过期及撤销、跨租户访问、并发审批、重复预算结算、未知回执、严格回放与文件路径边界。确认成功的动作不接受迟到失败覆盖；缺少确认标记的回执保留为 UNKNOWN。

只读调查新增 6 项测试：推理后产物写入失败恢复且仅请求一次、响应丢失不重复付费请求、上下文变化拒绝重用推理身份、显式开启及证据门槛、推理期间撤销证据后阻止发布、Worker 路由到只读调查。模型响应使用 HTTP Mock，没有付费模型质量结论。SQLite 已升级至 `0004_inference_result`，Alembic schema 检查无差异；PostgreSQL 和 Temporal 集成测试在本轮修改后再次通过。

企业采集继续新增 8 项测试：摘要去重及构建字段裁剪、四类范围拒绝、读取期间撤权、Deployment 字段裁剪、采集到推理的恢复路径。包含证据不得进入其他任务上下文的断言。SQLite 已进一步升级至 `0005_evidence_task_scope`，schema 检查无差异；原生 PostgreSQL 完整迁移/RLS 测试再次通过。采集和模型仍使用模拟 HTTP，真实企业端到端验收未完成。

生命周期新增 9 项测试：取消不可逆、恢复版本及角色、期限/紧急停用约束、审批过期结束、未确认动作升级去重、恢复 HTTP 接口、阻塞工作流保持活跃、PENDING 首次对账保留计时。真实本地 Temporal 测试增加接管暂停后恢复同一工作流的验证，2 项通过（55.56 秒）；前端恢复按钮构建通过。

沙盒验证新增 11 项已执行测试及 1 项 Docker 选择性测试。已验证五类结果门槛、源快照隔离及清理、符号链接/大小限制、独立 oracle 命令、固定可信进程的输出和超时终止、缺少 Docker 时拒绝降级、补丁路径别名去重。真实容器用例因当前 WSL Docker 不可用而未运行；没有执行模型生成的候选代码。`verify-patch --help` 已通过，镜像和 oracle 仍需运维配置。

有界候选修复里程碑新增 25 项默认测试及 1 项 Temporal 集成测试。覆盖完整候选审阅链路、失败反馈再生成、次数耗尽、推理响应丢失、补丁产物写入失败恢复、验证回执恢复、显式验证重试上限、并发推理与验证、原始输入变更永久失效、费用门槛、权限撤销、跨租户 API、非发起人的源码访问限制及 CRLF 源码摘要。SQLite 升级至 `0006_repair_runs`，Alembic 检查无差异；真实 PostgreSQL 再次验证两张新表的 RLS（1.36 秒）。新增 Temporal 流程通过（22.26 秒），原接管恢复流程再次通过（55.10 秒）。工作台补丁面板的 TypeScript/Vite 构建通过，CLI 帮助和 60 条仿真流程回归通过。

共享调度和运行观测新增 17 项默认测试，覆盖跨 Worker 并发上限、FIFO、重复创建满队列竞态、取消后保留租约、过期 token 防护及迟到错误隔离、共享策略、运维 ACL、独立监控认证、指标标签边界、事件/trace 关联、私密文件轮转、Worker HTTP 抓取、Dispatcher 阶段隔离和超过 100 条 UNKNOWN 的轮转。完整默认套件 121 通过（9.24 秒）。原生 PostgreSQL 验证新表 RLS、跨租户并发准入和聚合查询（1 项，1.50 秒）；两条真实 Temporal 集成流程再次通过（76.94 秒）。SQLite 升级至 `0007_worker_admission`，schema 检查无差异。前端构建通过。Prometheus/Grafana 配置仅提供模板，尚未做服务联调或告警送达验收。

测试执行需要本地线程和 socket 权限。当前环境中受限执行会阻塞 TestClient，因此完整套件及基础设施测试在获得执行权限后运行。测试还有两条第三方 Starlette/AnyIO 弃用警告，不影响断言结果。

尚未验证：真实付费模型、企业账号与外部写入、Docker 沙盒、Compose 容器组合、浏览器端到端、TLC 模型检查、负载/SLO、灾备恢复与 GPU 训练。当前运行环境没有可用的 Docker 引擎，代码执行器保持关闭，不降级到宿主机执行。

完整范围及剩余工作见[实施清单](implementation-plan.md)。
