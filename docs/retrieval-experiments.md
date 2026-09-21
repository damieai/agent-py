# 冻结数据集与可恢复的检索实验

状态：LF-02 的**离线检索子实验**已实现。使用实际 ContextCompiler，对固定案例运行 none / lexical / bm25_rrf 三组对照；模型 Harness、人工/judge 评分、真实调查/修复对照及 Langfuse dataset/experiment 同步仍未接入。LF-01 的真实平台验收继续单独保留，不因本地实验可运行而视为完成。

## 执行与恢复

从仓库根目录运行，不需要模型、Langfuse 或业务数据库凭证：

```bash
uv sync --extra dev --frozen
.venv/bin/agent-py experiment-freeze examples/retrieval-experiment.json .runtime/experiments/dataset.json
.venv/bin/agent-py experiment-run .runtime/experiments/dataset.json .runtime/experiments/development --split development --repeats 2
# 中断后，或重新核验既有证据并重建汇总：参数必须与原计划相同
.venv/bin/agent-py experiment-run .runtime/experiments/dataset.json .runtime/experiments/development --split development --repeats 2 --resume
```

冻结快照和新 run 目录均拒绝覆盖。选择其他 split 时使用新 run 目录，例如 `--split calibration` 或 `--split holdout`。默认只运行 development，不根据结果自动切换数据集。重复次数 1—10；每次最多 3000 个 job，单进程顺序执行，并按 repeat 轮换策略顺序。此 runner 不提供付费模型或企业写工具入口。

输入样例包含三个 split、六个合成案例，开发集只有两个案例。它用于演示完整工程流程，**不是生产检索质量或策略收益的证据**。

## 输入契约与分组

每例显式声明 case ID、split、entity_group、template_family、query、documents、relevant 文档 ID 和 context_budget。参考标签与 query/documents 分离；编译器只接收 query、授权文档及上下文预算，不接收 relevant。每个 job 使用新的临时 SQLite 库、固定本地 Grant 和 Principal，执行现有授权过滤、检索和结果校验；不创建业务 Service、不读取 .env，也不初始化模型或 Langfuse 客户端。宿主机配置的线上凭证不会启用这些能力。

冻结时检查：

- 案例/文档 ID 唯一；参考文档存在且不重复；查询非空。
- 同一实体组或模板族不得跨 development/calibration/holdout。
- 对查询和文档正文做大小写、空白归一化，忽略文档顺序及来源位置后拒绝重复输入，防止重命名案例制造额外样本。
- JSON 拒绝重复字段、非有限数值和额外 schema 字段；原始输入最多 2 MB，冻结快照最多 4 MB。

provenance 必须声明 synthetic 或 authorized_redacted；这只是操作者声明，不替代来源授权、去敏审查或参考答案核验。声明的分组和精确重复检查不能识别全部近义改写、错误分组或语义泄漏。所有 split 存在同一个本地快照中，逻辑隔离不替代文件权限，也不能防止有权读取文件的人接触 holdout 标签。

快照使用 `agent-experiment-dataset/v1` 格式，摘要覆盖规范化后的完整 schema（包含参考标签和预算）。freeze 不可覆盖已有文件；每次加载重新验证摘要。没有数字签名，摘要不能对抗有权同时重写数据和摘要的操作者。

## 固定计划与逐项账本

run 目录包含：

| 文件 | 含义 |
|---|---|
| dataset.json | 原始冻结输入的完整副本 |
| plan.json | 数据集摘要、split、三种固定策略、repeat 数及实现身份 |
| records/<job_digest>.json | 每个案例/策略/repeat 的不可覆盖结果与内容摘要 |
| report.json | 从逐项证据重算的派生汇总，可以重建 |
| .lock | POSIX flock 单写者锁 |

实现身份覆盖 agent_py 源码摘要、uv.lock、Python 版本及 Pydantic/SQLAlchemy 版本。升级实现、依赖或改变 split/repeats 时必须建新 run，不允许把结果混入旧对照。恢复前校验全部旧记录的摘要、计划身份、返回文档范围、上下文预算及 none 基线约束；发现损坏就拒绝执行新 job。运行结束再检查实现身份，期间变化则返回 INVALID。

每个 job 结果先写同目录临时文件并 fsync，再以不覆盖方式原子发布，随后 fsync 目录。文件 0600，run/records 目录 0700；锁和原子发布以本地 POSIX 文件系统为目标，尚未认证 Windows/NFS。崩溃产生的未发布临时文件不计为完成证据。

`--resume` 只运行没有已发布记录的 job；已完成或已失败记录都不自动重跑。这里允许重做的是无外部副作用的纯检索计算，不是未决付费模型调用；以后接模型时必须另接推理账本和预算预留。明确失败保存固定 EXECUTION_FAILED，不保存异常文本。修正失败需建新 run，保留旧结果供分析。断电/强杀时 report.json 可能滞后于逐项账本，恢复会重新计算。

report.json 不是信任根。消费结果前，应以原计划运行 `--resume` 完整核验记录；单独读取旧 COMPLETE 不能证明现存证据完整，也不能证明实验质量达标。

## 指标与解释边界

逐项证据保留检索文档 ID 顺序、context digest、字节数、chunk 数及 job 耗时。Recall、Precision、Reciprocal Rank 从这些 ID 和冻结参考标签重新计算，不信任记录中的外部评分。按策略汇总时明确列出 completed 与 missing_or_failed；缺失值不填零，也不默认为通过。

配对比较以 lexical 为基线，分别比较 none 和 bm25_rrf 的 Recall 差值。先在每个案例内汇总 repeats，再按实体组做固定种子的 cluster bootstrap（1000 次、95% 百分位区间），不把重复调用数当作独立样本数。少于五个实体组或存在不完整配对时不输出区间；同时输出逐案例差值和模板族分层。区间依赖声明实体组可作为重采样单位的假设，不证明真实业务独立性。

job P50/P95 包含一次临时数据库建库、文档装载、编译、校验和清理。none 不需要建库；这不是模型端到端时延，也不是公平的生产纯检索性能基准。主机负载、缓存和预热均未控制。

执行结果 COMPLETE 仅表示预期 job 全部有完整结果；任何失败/缺失为 INCOMPLETE，实现变化为 INVALID，后两者 CLI 返回非零。报告始终保留：

- quality_scope=retrieval_only
- model_quality=NOT_ASSESSED
- release_decision=NOT_ASSESSED
- langfuse=NOT_UPLOADED
- 配对结果 assessment=EXPLORATORY

不生成模型成功率、人工接管率、judge 分数或模型费用，不用检索 Recall 冒充最终答案质量，也不把新报告塞进旧 evaluation gate。LF-02 后续仍需预算约束的调查/修复实验、人工标注校准、评分来源、失败入库和平台关联；LF-04 再定义可验证的新发布门禁。

## 验证

```bash
.venv/bin/pytest tests/test_retrieval_experiments.py -q
```

测试覆盖真实检索对照、CLI 的冻结/运行/恢复、宿主设置隔离、标签不进入检索上下文、分组泄漏、快照/结果篡改、无效引用、失败保留、中断恢复、单写者锁、实现变化及按案例而非重复次数统计。Bitbucket 自定义 `retrieval-experiment` 只使用仓库合成样例，并保存输入快照与逐项报告。

2026-09-21 本地执行冻结 → 开发集两次重复 → 恢复复核，12/12 job 完成，0 失败。报告为 `.runtime/experiments/development-final/report.json`。两个合成案例上 none Recall=0，lexical 与 BM25/RRF 均为 1，候选相对 lexical 的 Recall 差值为 0；仅两个实体组，不输出置信区间，不能据此声称候选提升或生产质量达标。
