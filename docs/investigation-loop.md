# 有界只读调查

`workflow=investigation_loop` 为显式选择的 live 工作流。旧的 `investigate` 保持单次分析语义，候选修复仍使用 `repair_candidate`。此循环已通过模拟模型响应和数据库恢复测试，尚无真实企业账号或付费模型质量验收。

## 启用和执行

1. 运行 `make migrate`，升级至 `0011_investigation_rounds`。
2. 在 API 与 Worker 使用一致配置：`AGENT_EXECUTION_MODE=live`、`AGENT_ALLOW_MODEL_API=true`、模型 ID、API key、实际正数输入/输出单价。现有租户、Grant、任务和每日费用上限继续适用。
3. 可配置 `AGENT_COLLECTION_MANIFEST`，首次冻结上下文前采集匹配租户、项目、环境、资源和主体的固定企业来源。或者使用 `agent-py ingest` 导入任务证据。
4. 使用认证请求创建任务，例如向 `POST /api/v1/tasks` 提交以下 JSON，并设置唯一的 `Idempotency-Key`：

```json
{
  "kind": "incident",
  "workflow": "investigation_loop",
  "goal": "调查 demo-service 队列容量回归，区分代码变更与部署配置原因",
  "project": "demo",
  "environment": "lab",
  "resource": "demo-service",
  "budget_micro_usd": 1000000,
  "deadline_seconds": 1800
}
```

5. 使用 Temporal Worker 推进，或逐次执行 `agent-py tick TASK_ID`。每个 tick 至多发起一次新的付费推理；前面的轮次从持久化决策恢复。首次没有匹配证据则等待 `EVIDENCE_REQUIRED`，此时可导入材料后继续。
6. 通过现有任务产物列表下载 `model-analysis`。`investigation-loop/v1` 报告包含各轮冻结上下文、决策、停止原因和人工审阅标记；任务停在 `HUMAN_REVIEW`，不会标记业务成功。当前工作台的任务创建表单尚未提供此工作流选项，使用 API 创建。

## 调查规则

模型仅可返回有引用的总结、假设，以及“停止”或一个不超过 500 字符的后续检索查询。下一轮在同一授权证据库重新检索。模型不能提供 HTTP 地址、修改采集清单、凭证、ACL、预算或请求执行写工具；查询字符串只作为检索文本使用。

最多进行三轮付费决策。终止原因如下：

| 原因 | 行为 |
| --- | --- |
| `MODEL_STOP` | 模型选择结束分析 |
| `REPEATED_QUERY` | 后续查询与已有查询在大小写和空白归一化后重复 |
| `NO_PROGRESS` | 下一轮检索上下文摘要与任一已有轮次相同 |
| `NO_EVIDENCE` | 后续查询没有匹配的授权证据 |
| `ROUND_LIMIT` | 已完成三轮，忽略继续检索请求 |

企业采集在首次冻结前进行，后续轮次只查询已导入的本地证据库，不自主选择新的企业资源。`NO_PROGRESS` 使用上下文摘要判断，不能证明语义上的信息增益；这属于后续质量评测范围。

## 恢复与权限

每轮的查询及上下文先写入 `investigation_rounds`，唯一键为租户、任务和轮次。付费身份为 `investigation-loop:v1:N`，沿用预占、请求摘要绑定、派发 CAS、决策与费用同事务结算。竞争 Worker 采用相同冻结快照，只有抢占派发成功者调用模型。

模型完成而报告写入失败时，可以从已校验决策恢复，不重复推理。HTTP 响应丢失时保守结算且禁止自动再次付费；需要人工处理或新任务。无结果和无增量的后续检索也持久化，因此新增文档不会自动重启已停止的调查。首次无证据的等待不冻结空快照。

每次推理前后以及报告发布前，重新校验所有已使用轮次的证据，而不只是当前轮次。撤权、正文或版本变化、有效期失效会阻止继续；需要修复授权或创建新任务，不静默替换旧证据。每个执行边界继续检查 deadline、取消、接管、租户紧急停止、Worker 租约和 AgentRelease。SQL 表使用复合租户外键，PostgreSQL 同时启用 FORCE RLS。

本轮没有提供完整的通用 Plan DAG、语义级依赖失效或跨任务自动重规划，也没有新增外部写入能力。已下载的报告不能被远程撤回；现有产物下载按任务权限授权，不是逐文档的动态脱敏系统。
