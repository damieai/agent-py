# 单次与多轮调查的持久化对照实验

状态：LF-02 调查实验 runner 已实现，本地使用合成模型响应验证；真实模型、人工/judge 质量评分及 Langfuse experiment 同步尚未验收。runner 直接调用现有 `InvestigationHarness`，两组分别为 `investigate` 和 `investigation_loop`，复用 ContextCompiler、AnthropicGateway、预算预留、持久化推理账本和模型分析产物。它不使用仿真 Harness 代替调查逻辑。

## 默认离线执行

```bash
uv sync --extra dev --frozen
.venv/bin/agent-py experiment-freeze examples/investigation-experiment.json .runtime/investigation-experiments/dataset.json
.venv/bin/agent-py investigation-experiment .runtime/investigation-experiments/dataset.json examples/investigation-experiment-config.json .runtime/investigation-experiments/development
.venv/bin/agent-py investigation-experiment .runtime/investigation-experiments/dataset.json examples/investigation-experiment-config.json .runtime/investigation-experiments/development --resume
```

合成模式使用 `httpx.MockTransport`，所有“模型响应”和 token usage 均为固定测试数据；不会访问供应商。单次组返回待复核结论，多轮组首轮请求固定的 `followup` 查询，次轮停止；实际 Harness 仍可能因缺少新证据等原因提前停止。该脚本不是智能模型，也不是质量基准。

仓库样例包含两个 development 案例，每例有首轮及 followup 证据，用来测试两轮调查和恢复。所有案例沿用[冻结数据集](retrieval-experiments.md)契约；参考标签保留在快照中，不进入模型请求。当前 Harness 的固定上下文预算为 6000 字节，因此选中案例必须显式声明 `context_budget=6000`，query 至少五字符；不匹配时在启动前拒绝，不能悄悄忽略输入配置。

## 固定配置与费用约束

`examples/investigation-experiment-config.json` 展示全部字段。mode、model_id、split、repeats、检索策略、输入/输出每 token 的 micro-USD 价格、单任务与总预算、任务 deadline 均进入冻结计划。

- 固定两组策略，并发为 1，每例重复 1—10 次，总计最多 200 个 job；按 repeat 轮换两组顺序。
- 启动前要求 `案例数 × repeats × 2 × task_budget <= total_budget`。每个 job 获得相同的独立任务上限，不能借用其他案例的剩余额度；配置不足时不创建 run、不调用模型。
- 每次推理仍由真实 Gateway 做请求大小估算及输出 token 上限预留，由 Service 原子扣减任务和日预算。每个 job 独立数据库，因此日预算设为该 job 的固定上限；整体预算通过上述静态分配约束，而非跨库共享费用账户。
- 已结算费用与未结算预留分别保留。供应商超时或没有可用 usage 时，现有 Gateway 可能按最大预留保守结算；账本数字不是已对账的供应商账单。
- 供应商返回的实际费用可能超过估算。runner 在 job 完成后检测 `spent + reserved > task_budget`，将报告标为 INVALID 并停止后续 job；它无法阻止供应商已经产生的超额费用，因此不宣称绝对硬性账单上限。每轮发送前仍经过现有预算检查。
- 相同预算**上限**不等于相同实际算力。多轮可能花费更多，样本/模型/提示词不同会影响成本，不能只用轮数或调用次数宣称策略优劣。

计划还固定完整输入摘要、agent_py 源码/锁文件与依赖身份。恢复必须匹配原计划，不能通过修改价格、模式、模型、预算、数据或实现继续旧实验。运行中发现实现身份改变会标为 INVALID。新配置需要新 run，旧报告保留。

## 真实模型模式

真实模式已经有显式入口，尚未运行真实供应商验收。先复制配置到自己的文件，将 mode 改为 `live`，model_id 设为已开通的明确模型标识，并填写该模型当前适用的输入/输出价格及预算。仓库默认 1/2 的价格仅为合成测试数据，不是供应商报价。

独立环境变量 `AGENT_EXPERIMENT_MODEL_API_KEY` 提供凭证；不要把密钥写进配置或版本库。然后显式运行：

```bash
.venv/bin/agent-py investigation-experiment SNAPSHOT.json LIVE_CONFIG.json NEW_RUN_DIR --allow-model-api
```

`--allow-model-api` 表示允许付费推理，并允许将本次冻结数据中的选中任务目标、授权检索上下文及多轮历史发送给当前 Gateway 固定的 Anthropic Messages 端点。只有配置为 live、该开关启用且实验专用凭证存在时才允许开始。恢复 live 实验同样需要显式开关。使用者需先完成材料来源授权和出站审查；数据集中的 authorized_redacted 仅为操作者声明。

此入口不读取普通业务 `.env`、`AGENT_MODEL_API_KEY`、数据库/工具配置、发布配置或 Langfuse 凭证；内部 Settings 只接受 runner 明确传入的参数。HTTP transport 显式关闭环境代理继承和连接自动重试，沿用现有 Gateway 的固定端点、响应大小与超时限制。不会自动导出输入或响应到 Langfuse。

## 隔离、落盘与恢复

每个 job 独立 SQLite 数据库、任务、Grant、证据副本和 artifact 目录，仅创建本地 reader 身份。使用 `DisabledLiveExecutor`，没有企业读工具采集 manifest、修复沙盒、写动作派发器、Temporal Dispatcher 或企业工具凭证。调用的是同步 Harness，因此这里只验证实验内持久化恢复，不替代 Temporal Worker 的真实服务验收。

run 目录结构：

| 路径 | 内容 |
|---|---|
| dataset.json | 完整冻结输入副本 |
| plan.json | 固定配置与实现身份 |
| jobs/<job_digest>/started.json | 在任务启动前发布的 job 身份标记 |
| jobs/<job_digest>/state.db | 此任务的持久化状态和推理/费用账本，恢复时必须保留 |
| jobs/<job_digest>/artifacts/ | Harness 原始模型分析产物 |
| jobs/<job_digest>/analysis.json | 有效分析的不可覆盖副本，绑定逐轮 decision 摘要 |
| jobs/<job_digest>/result.json | 不可覆盖的任务状态、费用账本副本、引用 ID 和产物摘要 |
| report.json | 从已发布 result 重建的派生报告 |
| .lock | POSIX flock 单写者锁 |

run/job 目录 0700，发布文件和 state.db 0600；模型产物由既有 ArtifactStore 私有落盘。输出可能包含原始任务、证据和模型分析，应按输入相同的敏感级别保管。目录拒绝覆盖、符号链接 job/证据被拒绝；仅验证本地 POSIX 文件系统，不承诺 NFS/Windows 行为。

恢复时先检查所有旧结果、产物摘要、逐轮 decision 与账本绑定、引用是否属于该轮提供的文档，以及全部未完成 job 的任务身份、输入和账本可读性，再开始执行剩余任务：

1. 已有 result 的任务，包括 BLOCKED/FAILED/UNKNOWN，都不自动重跑。
2. 没有 result，但任务和 state.db 完整：使用同一请求幂等键和任务继续 Harness。已验证的推理 decision 被复用；多轮已持久化的 query/context 被复用，不重新花钱获取同一结果。
3. 某调用已经 dispatched，却没有可复用的有效 decision：现有 Gateway 拒绝再次发送。runner 将该 job 记为 UNKNOWN；已结算费用或未结算预留继续计入记录，不把“不知道”当成零成本。
4. started 已存在而 state.db 丢失/损坏、初始化未完成、任务身份或证据漂移：拒绝整个恢复，不重建任务。保留现场供人工分析。新 run 是新的实验和新的付费授权，不能用它掩盖旧调用的未知费用。
5. 任务 deadline 沿用原始值，不因为恢复而延长。已过期任务保持未完成。

文件间没有整体原子事务。崩溃可能发生在任务完成、产物写入或 result 发布之间；逐任务持久化账本用于安全恢复，report 可能滞后。摘要不带签名，只验证内部一致性，不对抗有权同时改写数据库、产物和全部摘要的人。上一批 `experiment-audit` / `experiment-curate` 仅接受检索实验格式，不能直接用于这个新协议；调查实验目前通过原配置 `--resume` 验证和重建汇总。

## 报告解释

每个结果包含实际任务 ID、轮次调用键、请求/decision 摘要、最大预留、结算值、dispatched 状态、分析摘要、引用 ID 以及固定错误类别。原始异常和密钥不进入报告。

- ANALYSIS_READY：生成 schema 有效且引用 ID 属于已提供证据的报告，等待人工复核。不是回答正确、语义引用正确或业务任务成功。
- BLOCKED：例如证据不足、预算不足、deadline。FAILED：其他执行错误。UNKNOWN：至少一个已发送调用没有持久化有效 decision；既可能是未知响应，也可能是已知无效响应，不能自动重试。
- 所有预期 job 都 ANALYSIS_READY 才为 COMPLETE；有缺失/阻塞/失败/未知为 INCOMPLETE。费用超限、出现 Operation 或实现变化为 INVALID。后两种 CLI 非零。
- 费用按组汇总；配对费用差只在某案例所有 repeats 的两组分析均完整时计算，保留 entity_group/template_family，不把缺失结果填零。不据两个示例输出置信区间或收益结论。
- `ledger_exposure_micro_usd` 仅合计已发布 result；`ledger_exposure_scope=published_job_records_only`、`cost_evidence_complete` 和 pending 数明确该范围。任务正在运行/崩溃未发布时，实时账本可能有额外支出或预留，不能只看旧 report 判断剩余额度。
- job P50/P95 包含建库、检索、模型响应和产物写入；恢复任务只记录当前执行片段，因缺少完整端到端时间而排除在分位数之外，并单列排除数量。

报告始终保留 answer_quality、citation_correctness、task_success、release_decision 为 NOT_ASSESSED，Langfuse 为 NOT_UPLOADED；合成模式 billing 为 NOT_INCURRED，live 为 NOT_RECONCILED。自动引用成员校验不是语义事实核验。人工/judge 评分、真实模型质量/成本对照、修复实验和平台同步仍属于后续 LF-02 工作。

## 验证

```bash
.venv/bin/pytest -q tests/test_investigation_experiments.py
```

测试使用实际 Harness、数据库和推理账本；外部供应商响应均为 MockTransport，包括 live 入口路径的测试。覆盖两轮执行、宿主配置隔离、密钥不落盘、显式出站与付费开关、总体预算准入、任务预算、超时/未结算调用、中断后结果复用、多轮恢复、账本丢失与漂移、原 deadline、产物/账本一致性、超支停跑和不完整证据。真实供应商与生产性能不能由这些测试替代。

2026-09-21 本地 CLI 验收：冻结样例 → 新 run → `--resume`，4/4 job 为 ANALYSIS_READY，单次组 2 个推理调用、多轮组 4 个推理调用；重建服务后复核未增加调用。报告为 `.runtime/investigation-experiments/development-final/report.json`，快照摘要为 `a6d482b86a95c1040ee2324039ec1ad1c61279e4c4b2046883ca294b66ea8013`。账本按合成 usage/价格记录两组分别 100/200 micro-USD，没有真实付款，Operation 为 0；两个案例不足以证明策略收益，答案质量和真实供应商费用均未验收。

最终验证：新增 29 项调查实验测试；全量回归 716 passed、10 skipped，Ruff 检查/格式校验通过。两项警告来自既有第三方 TestClient/AnyIO 弃用提示。
