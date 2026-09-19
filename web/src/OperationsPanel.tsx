import { useEffect, useState } from 'react';

type Snapshot = {observed_at: string; tasks: Record<string, number>; operations: Record<string, number>; admission: {active: number; waiting: number; limit: number; oldest_wait_seconds: number}; oldest_unconfirmed_seconds: number; pending_outbox: number; overdue_tasks: number; today_spent_micro_usd: number; today_reserved_micro_usd: number};

export function OperationsPanel({token}: {token: string}) {
  const [data, setData] = useState<Snapshot | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let alive = true;
    let timeout: ReturnType<typeof setTimeout> | undefined;
    const controller = new AbortController();
    async function refresh() {
      try {
        const response = await fetch('/api/v1/ops/summary', {headers: {Authorization: `Bearer ${token}`}, signal: controller.signal});
        const body = await response.json();
        if (!response.ok) throw new Error(body.error?.message || '运行概览不可访问');
        if (alive) { setData(body); setError(''); }
      } catch (e) { if (alive) { setData(null); setError(String(e)); } }
      finally { if (alive) timeout = setTimeout(() => void refresh(), 5000); }
    }
    void refresh();
    return () => { alive = false; controller.abort(); clearTimeout(timeout); };
  }, [token]);
  return <section className="panel ops-panel"><h2>运行概览</h2>
    <p className="hint">需 operator 权限。只显示当前授权项目与环境；租户并发上限为共享策略。</p>
    {error && <p role="alert" className="error">{error}</p>}
    {data && <>
      <div className="ops-cards">
        <div><strong>{data.admission.active} / {data.admission.limit}</strong><span>授权范围内占用 / 租户上限</span></div>
        <div><strong>{data.admission.waiting}</strong><span>等待名额 · 最久 {Math.round(data.admission.oldest_wait_seconds)} 秒</span></div>
        <div><strong>{data.pending_outbox}</strong><span>待确认的 Workflow 启动</span></div>
        <div><strong>${(data.today_spent_micro_usd / 1e6).toFixed(4)}</strong><span>今日费用 · 预占 ${(data.today_reserved_micro_usd / 1e6).toFixed(4)}</span></div>
      </div>
      <p className={data.oldest_unconfirmed_seconds >= 900 ? 'error-text' : 'hint'}>最久未确认动作：{Math.round(data.oldest_unconfirmed_seconds)} 秒 · 超过截止时间的任务：{data.overdue_tasks}</p>
      <div className="ops-states">{Object.entries(data.tasks).map(([state, count]) => <span className="badge" key={state}>{state}: {count}</span>)}</div>
      <small>更新于 {new Date(data.observed_at).toLocaleTimeString()}。本页快照不代表 SLO 已达标。</small>
    </>}
  </section>;
}
