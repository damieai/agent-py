# Agent Engineering Workbench

研发交付与故障处置 Agent 的工程实现。当前版本包含**双链路仿真、持久化只读调查、有界候选修复、审批工作台、动作对账、Temporal Worker、上下文编译与回放**。候选修复已连接模型网关和容器验证编排，实际模型与容器验收仍待完成。

这是迭代中的工程项目，**尚未完成 B00—B12 的全部计划**。真实企业写入默认关闭，仿真不调用模型、不修改企业系统，仿真通过率不代表模型质量或生产可靠率。[实施状态与剩余任务](docs/implementation-plan.md)列明差距。

已确定接入 **Langfuse**，将调查/修复轨迹、提示词版本、人工与自动评分、数据集实验连接成效果优化流程。目标是用真实任务比较模型、检索与修复策略的质量、费用和耗时，再把通过门禁的配置固定到发布包。当前为 **LF-01 partially implemented**：可选 SDK/OTLP 元数据导出、因果关联与合成回读诊断已实现；[独立自托管配置](docs/langfuse-selfhost.md)已通过静态校验，真实平台与模型验收尚未完成；接入步骤、职责和验收见 [Langfuse 实施方案](docs/langfuse.md)。

## 本地运行

要求 Python 3.12、uv；前端需要 Node 24。SQLite 用于本地开发，生产配置要求 PostgreSQL 与非特权数据库角色。

```bash
uv sync --extra dev --frozen
cp .env.example .env
```

在 `.env` 设置至少 32 字符的随机 `AGENT_AUTH_SECRET`，不要使用示例或测试密钥。然后：

```bash
make init
make demo-repair
make demo-incident
make api
```

`demo-*` 中的自动批准**只在显式仿真模式**可用。正常 API 任务在审批前暂停。

另一个终端启动前端：

```bash
npm --prefix web ci
npm --prefix web run dev
```

打开 Vite 输出的本地地址。用 `agent-py token --subject developer` 生成开发凭证并输入工作台；审批时换成 `agent-py token --subject reviewer` 的凭证。凭证只存在页面内存，刷新后清除。

API 创建的任务需要 Worker 推进。没有 Temporal 时，可显式运行 `agent-py tick TASK_ID` 演示一个仿真步骤；这不是生产恢复方案。

## 持久化运行

运行本地 Temporal CLI：

```bash
temporal server start-dev --db-filename .runtime/temporal.db
make worker
# 再开一个终端
make dispatcher
```

也提供 Compose 开发配置。先设置 `AGENT_DB_PASSWORD` 与 `AGENT_AUTH_SECRET`：

```bash
docker compose up -d postgres temporal
docker compose run --rm migrate
docker compose run --rm api agent-py init
docker compose up -d api worker dispatcher
```

Compose 的 PostgreSQL 管理角色与 Temporal dev server **仅用于开发**。生产部署需要独立迁移角色、受限运行角色、TLS、持久化 Temporal 服务、备份与镜像 digest 锁定。镜像标签目前是开发配置，尚未完成容器验收。

## 测试和评测

```bash
make check
make test
agent-py evaluate --split development --output .runtime/evaluation.json
npm --prefix web run build
```

执行真实基础设施集成测试：

```bash
uv sync --extra dev --extra postgres-test --frozen
AGENT_TEST_POSTGRES=1 .venv/bin/pytest tests/test_postgres.py -q
AGENT_TEST_TEMPORAL=1 .venv/bin/pytest tests/test_runtime.py -q
```

PostgreSQL 测试使用独立临时原生数据库；Temporal 测试首次下载本地测试服务。这些进程需要本地 socket 权限。仿真评测有 60/20/40 条配置，但复用场景模板，**只检验执行协议，不用于报告模型泛化能力**。

## 真实模型与企业接入

企业读取实现位于 `adapters/enterprise.py`，覆盖 Bitbucket Cloud、Jira Cloud、Jenkins、Kubernetes、Prometheus 和 Loki。服务 URL 与 Token 必须由可信配置传入；适配器拒绝跨站跳转和过大响应。还没有完成企业端到端联调与自动写入认证。

设置模型 ID、API Key 后，可以对已经导入的授权证据执行单次只读分析：

```bash
agent-py ingest TASK_ID ./selected-runbook.txt
agent-py analyze TASK_ID INPUT_MICRO_USD_PER_TOKEN OUTPUT_MICRO_USD_PER_TOKEN --allow-api
```

费用参数按实际使用的模型填写。该命令产生分析产物，不执行模型提议的动作；候选修复使用下面单独启用的工作流。不要导入未经允许出站的企业材料。

也可让 Worker 执行持久化只读分析：先运行 `make migrate`，配置 `AGENT_EXECUTION_MODE=live`、`AGENT_ALLOW_MODEL_API=true`，并将 `AGENT_MODEL_INPUT_MICRO_PER_TOKEN` 和 `AGENT_MODEL_OUTPUT_MICRO_PER_TOKEN` 设置为实际正数费用。未显式开启时不会发起模型请求。创建任务并用 `ingest` 导入证据后，由 Worker 或 `agent-py tick TASK_ID` 推进。

默认使用导入证据；配置采集清单后，Worker 在首次推理前读取清单内的企业资源。没有匹配证据时等待 `EVIDENCE_REQUIRED`；分析后保存产物并停在 `HUMAN_REVIEW`，不宣称修复成功。已校验的模型决策与费用同事务保存，产物写入失败后可恢复而不再次推理；响应丢失则保守记账并停止自动重试。相同推理身份下证据或请求发生变化会拒绝重用，需要新任务重新分析。

采集配置参考 [collection-manifest.json](examples/collection-manifest.json)。将实际配置保存到运维控制的文件，通过 `AGENT_COLLECTION_MANIFEST` 指定路径，并在进程环境中设置 `token_env` / `username_env` 引用的凭证变量。凭证变量需要由 shell 或 Secret 管理器注入，任意连接器变量不会自动从 `.env` 加载。可先运行 `agent-py collect TASK_ID` 单独采集，无需开启模型付费请求。

每条来源必须匹配租户、项目、环境、资源及主体。支持 `bitbucket_pr(workspace, repo, pr)`、`jira_issue(key)`、`jenkins_build(job, build)`、`kubernetes_deployment(namespace, name)`；`parameters` 填写对应参数。地址沿用企业读取适配器的 HTTPS、固定前缀及禁止重定向限制。清单只能由运维配置，不接受模型修改。

新导入与采集的证据绑定任务；未变化的采集内容按摘要去重。旧版本未绑定任务的文档仍按原项目 ACL 共享，迁移不会自动猜测其归属。采集省略构建参数及 Pod 配置，但工单与 PR 正文仍可能包含业务敏感信息，需要按企业出站规则选择来源。真实账号联调尚未完成。

需要多轮只读调查时，在 live 工作台选择“多轮只读调查”，或通过 API/CLI 创建 `workflow=investigation_loop` 任务：最多三轮，冻结输入并恢复已结算决策，重复查询、无证据或无增量时停止。工作台展示每轮假设、引用与费用；状态与报告下载复核读取者的冻结证据权限。配置、请求示例和恢复边界见[有界调查说明](docs/investigation-loop.md)。

## 有界候选修复

`workflow=repair_candidate` 将已授权的本地源码、任务证据、模型补丁、容器验证和人工审阅连接起来。失败候选可按清单上限再次生成（最多 3 次），每次回到同一冻结基线。补丁和验证结果持久化，工作台可查看每轮状态和源码差异。通过回归后停在 `CANDIDATE_READY_FOR_REVIEW`，没有自动推送、合并或部署。

该路径需 `live` 模式、付费模型及候选执行两个显式开关、允许源码出站的仓库绑定、digest 镜像和独立 oracle。完整配置及故障恢复见[候选修复操作说明](docs/repair-workflow.md)，示例见[repair-manifest.json](examples/repair-manifest.json)。

## 执行边界

候选补丁的基线/修复对照验证可通过 `agent-py verify-patch` 执行，配置与验收限制见[沙盒验证说明](docs/sandbox-verification.md)。需要可用 Docker 和预置的 digest 镜像，当前环境尚未完成容器验收。

- HTTP 请求、模型推理和业务动作使用不同的身份及幂等记录。
- UNKNOWN 保留原动作身份，通过独立 Reconciler 查询外部权威状态。
- 未确认动作超过 15 分钟标记 `MANUAL_REVIEW`，继续对账，不自行判定失败。当前只记录事件和工作台状态，未接入外部值班通知。
- 人工接管可通过工作台或 `POST /api/v1/tasks/{id}/resume` 恢复，请求带 `expected_version`；恢复不延长截止时间、不重置预算，取消任务不能恢复。
- 审批绑定参数摘要，执行时复核批准人和发起人的当前资源授权。
- 数据库 RLS 在 PostgreSQL 中强制启用；生产启动拒绝超级用户、BYPASSRLS 和表所有者。
- 13 条租户复合外键在提交时拒绝跨租户或孤立引用；升级至 `0009_tenant_references` 前阅读[维护与回滚步骤](docs/database-integrity.md)。
- 沙盒必须使用 digest 固定的容器镜像，没有宿主机执行不可信代码的降级路径。
- Webhook 仅支持已配对的签名 Connector 通知；通知触发权威查询，不能自行宣布业务成功。
- 回放没有生产客户端或网络补齐路径；导出包仍需检查业务敏感内容。

更多说明：[实施计划](docs/implementation-plan.md)、[运行手册](docs/runbook.md)、[架构决策](docs/architecture.md)。

## 运行保障

Worker 已接入数据库共享的租户并发上限、FIFO 等待票据和过期租约防护。工作台提供按 operator 当前授权过滤的运行概览；API 和 Worker 提供独立凭证保护的 Prometheus 指标，支持受限字段的本地 OTel trace 导出。配置、监控模板和故障处置见[运行观测说明](docs/operations.md)。真实负载、监控告警送达和跨进程 OTLP 链路仍待验收。

## 证据检索与预览

可通过 `AGENT_CONTEXT_STRATEGY=bm25_rrf` 启用 Python 结构分块、BM25 与来源匹配融合排序，保留原文摘要及行号，调用前后复核权限与版本。默认策略仍为 lexical。工作台提供只读证据预览；`agent-py evaluate-retrieval examples/retrieval-development.json` 可在临时数据库比较三个策略。算法、兼容性、开发集结果与限制见[上下文编译说明](docs/retrieval.md)。

## 事件时间线与审计

工作台支持带凭证的 SSE 事件续传、流中授权复核及审计包下载。`agent-py export TASK_ID audit.json` 导出 v2 一致性快照，`agent-py audit-check audit.json` 离线检查事件、审批和动作回执；v2 回放额外约束租户与派发顺序。包摘要不等于真实性或远端状态证明，详见[审计与回放说明](docs/audit-replay.md)。

## 企业读取故障治理

企业证据 GET 已接入数据库共享熔断、有界重试、Retry-After 冷却和单 Worker 半开探测。每次重试重新校验任务授权与取消，旧许可无法覆盖恢复后的熔断状态。共享读取名额进一步限制健康依赖的并发请求，默认每租户/依赖 4 个，可通过 `dependency-limit` 调整。先升级数据库至 `0010_dependency_bulkhead`；查看、恢复和部署限制见[依赖故障治理](docs/dependency-resilience.md)。该策略不重发付费模型或外部写动作。

可选 [Ed25519 审计签名](docs/audit-signing.md) 支持 API/CLI/工作台导出，以及独立信任清单下的离线校验、租户与用途绑定、密钥轮换和撤销。默认关闭，签名不等于不可改写存储或远端状态证明。

`make evaluation-gate` 在临时仿真环境运行协议评测与检索对照，按固定策略输出通过、失败或证据不足，并以退出码阻止 CI 回归；详见[评测门禁](docs/evaluation-gates.md)。本地通过不代表生产发布获准。

认证支持受控本地 RSA JWKS 轮换和撤钥，无网络密钥发现或旧钥缓存回退；`agent-py auth-keys-check FILE` 可离线核对公钥指纹。配置、Token 兼容性与轮换步骤见[认证说明](docs/authentication.md)。

`make formal-check` 用固定 TLC 检查动作幂等及双 Worker 租约模型，四个负向控制必须产生指定反例；输入摘要、实现测试映射和证明边界见[形式化验证](formal/README.md)。需要本地 Java 和通过摘要校验的 TLC jar。

可选 `release-build` / `release-check` 将源码、运行配置和重算后的评测证据绑定为发布摘要；任务和 Temporal 队列固定到该版本，配置漂移拒绝新派发。启用顺序、旧任务恢复和证明边界见[发布版本说明](docs/releases.md)。

可选[发布签名与撤销](docs/release-signing.md)使用独立公钥策略和短期 Ed25519 证明；同版本续签不改变队列，撤钥或到期阻止新派发，保留取消和对账。

`make browser-test` 运行 Chromium 工作台验收，连接独立临时 FastAPI/SQLite 与仿真渠道，覆盖审批、接管恢复/取消、撤权清屏、下载和凭证切换竞态。安装步骤、报告位置及边界见[浏览器测试](docs/browser-testing.md)。

`make recovery-check` 运行 Linux 进程硬杀与原生 Temporal Worker 重启演练，输出四个场景、同版本历史回放及源码/证据摘要。跳过或缺失证据会判失败；默认 360 秒租约带来的恢复延迟没有被隐藏。运行方式和未覆盖范围见[恢复演练](docs/process-recovery.md)。
