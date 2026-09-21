# 冻结数据集与可恢复的检索实验

状态：LF-02 的**离线检索子实验**已实现。使用实际 ContextCompiler，对固定案例运行 none / lexical / bm25_rrf 三组对照；另有[调查 Harness 对照 runner](investigation-experiments.md)，已通过合成响应的预算和恢复验证；人工/judge 评分、真实模型调查/修复对照及 Langfuse dataset/experiment 同步仍未验收。LF-01 的真实平台验收继续单独保留，不因本地实验可运行而视为完成。

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

report.json 不是信任根。消费结果前使用下述 `experiment-audit` 独立核验；需要继续未完成任务时才使用原计划的 `--resume`。单独读取旧 COMPLETE 不能证明现存证据完整，也不能证明实验质量达标。

## 历史证据独立审计

```bash
.venv/bin/agent-py experiment-audit .runtime/experiments/development .runtime/experiments/development-audit.json
# 已在独立位置保存摘要时，额外固定整份证据，防止数据和内部摘要一起被替换
.venv/bin/agent-py experiment-audit .runtime/experiments/development .runtime/experiments/development-pinned-audit.json --expected-evidence-digest YOUR_SAVED_SHA256
```

审计读取归档中的实现身份，不要求它等于当前源码，因此升级代码后仍可复核旧实验。它不创建数据库、不执行检索、不调用模型/平台，也不修改源目录；必须读取既有 `.lock` 并取得共享锁，正在写入的实验会被拒绝。仅适用于本地 POSIX 文件系统；锁防护遵守协议的写者，不能抵御有权限绕过锁修改文件的操作者。

审计重新校验数据集、计划、实现身份摘要、全部 job 身份/摘要/文档范围/预算，然后从原始记录重算指标。它不信任旧 report 的 COMPLETE 和分数：

- 原始证据损坏、存在未知记录、证据文件为符号链接或独立摘要 pin 不匹配：拒绝，不产出有效审计报告。
- 旧汇总丢失、无法读取或与重算不一致（包括原先的 INVALID）：`audit_status=REPORT_MISMATCH`，输出重算结果供检查，CLI 非零；不自动修复旧汇总。
- 汇总一致但存在失败/未完成：`audit_status=INCOMPLETE`，CLI 非零。
- 汇总一致且全部任务完成：`audit_status=COMPLETE`。这仍不代表模型质量或发布门槛通过。

`evidence_digest` 覆盖冻结数据集、完整计划及按 job 摘要索引的记录清单；缺失记录以 null 入摘要。它不包含可重建的 report，也不依赖绝对路径。可把首次审计的摘要另存到独立可信位置，再通过 `--expected-evidence-digest` 固定。没有独立 pin 或签名时，内部哈希只能检查一致性，不能证明执行真实发生，也不能抵御所有内容及哈希同时重写。归档未保存 context 正文，所以这里只校验 context digest 的格式及 none 基线约束，不能重建正文验证该摘要。评分算法采用当前审计器的 v1 实现；未来变更应显式升级协议。

输出必须位于源归档目录之外、不可覆盖已有文件，权限 0600。审计报告不复制 query/document 正文，包含独立重算指标、证据摘要及 review_queue。待审核项只包括 lexical/BM25-RRF 的显式执行失败或 Recall 小于 1，保留具体 job/策略/repeat/原因；none 的预期低分不作为失败来源。队列只是调查线索，失败也可能来自错误参考标签，不能直接作为训练集。

## 人工确认后导入开发用例

此入口支持已有**离线检索 development 案例**的失败子集整理。暂不导入真实任务轨迹，不生成模型答案评分，也不允许将 calibration/holdout 失败复制到开发集。

先查看审计队列，在授权范围内检查源案例的文本、标签和分组，再手工创建独立审核文件，例如：

```json
{
  "format": "agent-experiment-curation/v1",
  "evidence_digest": "替换为本次审计的64位摘要",
  "dataset_name": "reviewed-retrieval-failures-v1",
  "decisions": [
    {
      "case_id": "替换为队列中的development案例ID",
      "reviewer": "reviewer-01",
      "rubric_version": "relevance-review-v1",
      "reference_verified": true,
      "redaction_verified": true
    }
  ]
}
```

```bash
.venv/bin/agent-py experiment-curate .runtime/experiments/development .runtime/experiments/review.json .runtime/experiments/curated-v1
.venv/bin/agent-py experiment-run .runtime/experiments/curated-v1/dataset.json .runtime/experiments/curated-run-v1
```

审核文件使用严格 schema；两项确认必须为 JSON true，审核人和规则版本使用非敏感标识。执行时重新审计整个源归档，要求摘要未变化、旧汇总与重算一致、源 split 为 development、选中案例确有非 none 失败。明确 EXECUTION_FAILED 可进入审核；缺失 job 本身不算失败，缺失导致旧汇总不一致时必须先处理源实验。没有审核或摘要过期就拒绝，绝不自动将队列全部入库。

新目录包含冻结 `dataset.json`、`lineage.json` 和最后写入的 `complete.json`。lineage 记录源数据集/实现/整份证据摘要、审核内容与摘要、对应失败 job 和目标数据集摘要；complete 固定目标数据集与 lineage 摘要。每个文件私有、原子、不可覆盖；中途失败可能留下未完成目录，缺少 complete 不应作为完成的审核包交付，应保留现场并使用新的输出路径。

选中案例的输入、参考标签、entity_group、template_family 和 ID 全部保持原样，冻结时再次检查精确重复与分组约束。它是带出处的开发失败子集，不增加独立样本量；重新评测这个已观察子集不能当作无偏 holdout 改善证据。此入口不负责合并其他数据集或自动修正标签。审核人身份及去敏/参考核验是操作者声明，尚无认证、双人裁决、语义去重或真实轨迹权限证明；不应将它描述为已完成 LF-02 人工评分系统。

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

不生成模型成功率、人工接管率、judge 分数或模型费用，不用检索 Recall 冒充最终答案质量，也不把新报告塞进旧 evaluation gate。LF-02 后续仍需预算约束的调查/修复实验、人工标注校准、评分来源、真实轨迹失败入库和平台关联；LF-04 再定义可验证的新发布门禁。

## 验证

```bash
.venv/bin/pytest tests/test_retrieval_experiments.py tests/test_experiment_review.py -q
```

测试覆盖真实检索对照、CLI 的冻结/运行/恢复、宿主设置隔离、标签不进入检索上下文、分组泄漏、快照/结果篡改、无效引用、失败保留、中断恢复、单写者锁、实现变化及按案例而非重复次数统计。Bitbucket 自定义 `retrieval-experiment` 只使用仓库合成样例，并保存输入快照与逐项报告。

2026-09-21 本地执行冻结 → 开发集两次重复 → 恢复复核，12/12 job 完成，0 失败。报告为 `.runtime/experiments/development-final/report.json`。两个合成案例上 none Recall=0，lexical 与 BM25/RRF 均为 1，候选相对 lexical 的 Recall 差值为 0；仅两个实体组，不输出置信区间，不能据此声称候选提升或生产质量达标。

2026-09-21 新增历史审计与失败整理验收：当前审计器直接读取上一版 `.runtime/experiments/development-final`，12 项旧记录重算一致，报告为 `.runtime/experiments/development-final-audit-v2.json`；没有调用旧实现或恢复执行。另将合成开发案例的 context_budget 设为 1，使用实际 ContextCompiler 运行 6 项任务，得到两个待审核案例、四项非 none 的 RECALL_SHORTFALL，见 `.runtime/experiments/review-shortfall-audit.json`。此故障样例仅用于验证检测流程，不作为策略效果证据；未冒充真实人工审核。新增 28 项测试覆盖独立 pin、归档篡改、报告滞后、只读与锁、审核及分组约束、不可覆盖和来源留存；全量回归 687 passed、10 skipped（另有两项第三方弃用警告）。
