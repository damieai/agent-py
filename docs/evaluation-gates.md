# 本地评测门禁

门禁将已有仿真执行协议测试和检索消融报告组合为确定性判定：`PASS`、`FAIL` 或 `INSUFFICIENT_EVIDENCE`。它不调用模型、访问企业系统或授权发布；报告始终包含 `production_ready=false`。Bitbucket CI 已配置运行，实际远端流水线执行仍需在仓库环境验收。

## 一次运行

```bash
make evaluation-gate
```

入口 `scripts/check_evaluation_gate.py` 在临时目录创建仿真数据库与独立仿真远端，不读取生产数据库配置或本地 `.env`，不调用模型；结束后清理临时状态。保留 `.runtime/gate/simulation.json`、`retrieval.json`、`gate.json` 三份证据。脚本显式固定 test/simulation 模式和存储路径，其他 Settings 环境变量仍可能影响测试，应在干净 CI 环境运行。

默认例子使用 60 条 development 仿真配置和 12 条手写检索查询，对比 lexical 与 bm25_rrf。当前本地结果通过，不是历史版本回归对照：CI 内的两个策略均来自当前 checkout。要比较两个代码版本，应在各自干净 checkout 生成报告，再以相同固定 fixture 和策略要求运行离线门禁。

## 独立比较已有报告

```bash
.venv/bin/agent-py evaluation-gate \
  examples/evaluation-gate-policy.json \
  .runtime/gate/simulation.json \
  /path/to/baseline-retrieval.json \
  /path/to/candidate-retrieval.json \
  examples/retrieval-development.json \
  --output .runtime/comparison.json
```

参数依次为策略、仿真报告、基线检索报告、候选检索报告和原始 fixture。基线与候选可以来自同一消融报告，但必须分别包含策略声明的算法。命令不构建 Service，不读取凭证或连接数据库，也不从报告发现 URL 或附加文件。

| 退出码 | 判定 | 含义 |
|---|---|---|
| 0 | PASS | 输入完整且满足本次策略的全部门槛 |
| 1 | FAIL | 证据可比较，但存在协议失败、质量退化或上下文预算超限 |
| 2 | INSUFFICIENT_EVIDENCE / 文件错误 | 输入缺失、畸形、不一致、数据集不符或样本不足；不可按通过处理 |

文件不存在、重复 JSON 键或超长输入会生成新的 INSUFFICIENT_EVIDENCE 报告，覆盖旧 PASS，避免复用陈旧结果。报告以仅所有者可读写的临时文件原子替换。若输出路径不可写或与输入同路径（含符号链接解析）则退出 2，此时旧报告可能保留；CI 必须以当前命令退出码为准。脚本的上游评测若提前异常也会非零退出，不得消费旧报告。

## 预先固定的策略

`examples/evaluation-gate-policy.json` 绑定仿真 split/数据集摘要、检索 fixture 摘要、基线和候选算法，以及样本数、recall、MRR、平均降幅、退化查询数和平均上下文字节上限。

阈值和数据集应先评审再运行候选实验。CI 读取仓库策略，因此策略文件和相关代码需要仓库权限、分支保护与独立审阅；门禁不能阻止有权修改流水线的人同时降低阈值。更改 fixture 后要显式更新摘要并重新评审，不能把摘要不匹配自动当作新基线接受。

本轮仿真报告升级为 `simulation-conformance-v2`，记录完整配置集摘要和每个案例的最大派发次数。旧 v1 报告缺少检查所需字段，必须重新执行，不能直接改 suite 字符串。其他导出/回放格式不受影响。

## 判定规则

- 仿真必须精确覆盖所选 split 的所有案例，case ID 不重复、task ID 不复用，family 和预期结果不能被改写。根据实际结果、已确认操作数、远端副作用数及最大派发次数重算 passed 和汇总；任何案例不符合协议都会阻止通过，包括预期拒绝或取消的场景。
- 检索必须与 fixture 摘要、名称、查询集合和每条预算一致。未知/重复文档、缺失/重复查询、超预算、矛盾的 chunk/字节记录均不能成为通过证据。
- 根据 fixture 的相关文档标签和有序召回列表重算逐条 recall、precision、reciprocal rank，再校验报告汇总。浮点一致性绝对容差为 `1e-12`，禁止非有限数字与布尔计数。
- 按 query ID 配对，recall 或 reciprocal rank 任一降低即计为退化查询；即使平均分相同，也受退化查询数上限控制。报告列出逐条差值和每项门槛的观测值。
- 仿真报告上限 10 MB、每份检索报告上限 10 MB、策略及 fixture 各 2 MB；案例、查询和文档有类型及数量限制。无效输入只返回固定诊断，不输出源文档正文。

## 证据边界

摘要绑定输入内容，但不会认证报告来源或证明这些结果由指定代码执行。仿真回执和检索顺序是评测器记录的证据，恶意评测器仍能伪造。签名执行证据、完整 AgentRelease 版本绑定、运行时固定发布包、灰度、回滚和真实发布授权尚未接入。

仿真配置复用六类模板，不能测泛化；12 条手写查询不能支持统计显著性或真实模型质量结论。上下文字节不是 token、模型费用或端到端时延。当前没有成本/时延实测门禁，也没有用样本量不足的指标冒充生产 SLO。
