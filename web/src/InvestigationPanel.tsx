import { useEffect, useState } from 'react';

type Decision = {summary: string; hypotheses: string[]; evidence_ids: string[]; next_query: string | null; stop: boolean};
type Round = {ordinal: number; query: string; state: string; context_digest: string; decision: Decision | null; evidence: {id: string; source: string; version: string; chunk_id?: string; start_line?: number; end_line?: number}[]; spent_micro_usd: number; reserved_micro_usd: number};
type Investigation = {max_rounds: number; rounds: Round[]; stop_reason: string | null; waiting_reason: string | null};
const states: Record<string, string> = {READY: '输入已冻结，等待推理', IN_FLIGHT: '已派发，等待结果；中断时需人工核对', RESPONSE_UNKNOWN: '缺少有效决策，禁止自动重试付费请求', DECIDED: '已保存模型决策', NO_EVIDENCE: '没有匹配证据', NO_PROGRESS: '证据无增量'};
const reasons: Record<string, string> = {MODEL_STOP: '模型结束调查', REPEATED_QUERY: '检索查询重复', NO_PROGRESS: '证据无增量', NO_EVIDENCE: '没有匹配证据', ROUND_LIMIT: '已达调查轮数上限'};
const money = (value: number) => `$${(value / 1_000_000).toFixed(6)}`;

export function InvestigationPanel({taskId, token, onAccessLost}: {taskId: string; token: string; onAccessLost: () => void}) {
  const [run, setRun] = useState<Investigation | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    let request: AbortController;
    async function load() {
      request = new AbortController();
      const timeout = setTimeout(() => request.abort(), 10000);
      try {
        const response = await fetch(`/api/v1/tasks/${taskId}/investigation`, {headers: {Authorization: `Bearer ${token}`}, signal: request.signal, cache: 'no-store'});
        const data = await response.json();
        if (!alive) return;
        if (!response.ok) {
          setRun(null);
          if ([401, 403].includes(response.status)) { onAccessLost(); return; }
          throw new Error(data.error?.code === 'EVIDENCE_STALE' ? '证据已变更或访问已撤销，调查内容已清除。请核对权限或创建新任务。' : data.error?.message || '调查记录不可访问');
        }
        setRun(data.investigation); setError('');
      } catch (e) {
        if (alive) { setRun(null); setError(request.signal.aborted ? '调查状态请求超时，请等待重新读取。' : String(e)); }
      } finally {
        clearTimeout(timeout);
        if (alive) timer = setTimeout(() => void load(), 3000);
      }
    }
    setRun(null); setError('');
    void load();
    return () => { alive = false; request?.abort(); clearTimeout(timer); };
  }, [taskId, token, onAccessLost]);

  return <section className="investigation-panel" aria-label="只读调查过程">
    <h3>只读调查过程</h3>
    <p className="hint">最多 {run?.max_rounds ?? 3} 轮。以下是模型假设与证据引用，结论需要人工审阅。</p>
    {error && <p role="alert" className="error">{error}</p>}
    {run && run.rounds.length === 0 && <p>{run.waiting_reason === 'EVIDENCE_REQUIRED' ? '等待导入或采集匹配的授权证据。' : '等待 Worker 开始调查。'}</p>}
    {run?.stop_reason && <p className="investigation-outcome"><strong>{reasons[run.stop_reason] || run.stop_reason}</strong> · {run.waiting_reason === 'HUMAN_REVIEW' ? '等待人工审阅' : '等待报告落盘或处理任务阻塞'}</p>}
    {run?.rounds.map(round => <section className="investigation-round operation" key={round.ordinal}>
      <div><strong>调查第 {round.ordinal} 轮</strong><span className="badge">{states[round.state] || round.state}</span></div>
      <p>检索查询：{round.query}</p>
      <small>已计费用 {money(round.spent_micro_usd)} · 预占 {money(round.reserved_micro_usd)}</small>
      {round.decision && <>
        <p className="investigation-summary">{round.decision.summary}</p>
        <h4>待核实假设</h4>
        <ul>{round.decision.hypotheses.map((hypothesis, i) => <li key={i}>{hypothesis}</li>)}</ul>
        {round.decision.next_query && <p>建议下一查询：{round.decision.next_query}</p>}
      </>}
      <details><summary>冻结证据与引用（{round.evidence.length}）</summary>
        <ul>{round.evidence.map((evidence, i) => <li key={evidence.chunk_id || `${evidence.id}:${i}`}>
          <span>{evidence.source}</span> · 版本 {evidence.version}{evidence.start_line !== undefined ? ` · L${evidence.start_line}–L${evidence.end_line}` : ''}
          {round.decision?.evidence_ids.includes(evidence.id) && <strong> · 模型引用</strong>}
          <small className="mono"> · {evidence.id}</small>
        </li>)}</ul>
        <small className="mono">上下文摘要 {round.context_digest}</small>
      </details>
    </section>)}
  </section>;
}
