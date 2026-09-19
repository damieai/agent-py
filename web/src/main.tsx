import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './style.css';
import { RepairPanel } from './RepairPanel';
import { OperationsPanel } from './OperationsPanel';
import { ContextPanel } from './ContextPanel';
import { EventPanel } from './EventPanel';

type Operation = {id: string; tool: string; resource: string; status: string; recovery_status: string | null; parameters: unknown; error: string | null};
type Approval = {id: string; operation_id: string; status: string; payload_digest: string; expires_at: string};
type Task = {id: string; version: number; taken_over: boolean; cancelled: boolean; status: string; result: string | null; waiting_reason: string | null; contract: {kind: string; workflow?: string; goal: string; resource: string}; operations?: Operation[]; approvals?: Approval[]; artifacts?: {id: string; kind: string; digest: string}[]; spent_micro_usd: number};

function App() {
  const [token, setToken] = useState('');
  const [tasks, setTasks] = useState<Task[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [task, setTask] = useState<Task | null>(null);
  const [goal, setGoal] = useState('调查 demo-service 的失败并准备可验证的处置方案');
  const [kind, setKind] = useState('repair');
  const [workflow, setWorkflow] = useState('investigate');
  const [resource, setResource] = useState('demo-service');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [showOps, setShowOps] = useState(false);
  const [mode, setMode] = useState('unknown');
  const current = useRef({token, selected});
  current.current = {token, selected};
  const refreshSequence = useRef(0);

  async function request(path: string, method = 'GET', body?: unknown, key?: string) {
    const response = await fetch(path, {method, headers: {'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json', ...(key ? {'Idempotency-Key': key} : {})}, body: body === undefined ? undefined : JSON.stringify(body)});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error?.message || JSON.stringify(data.detail || data));
    return data;
  }
  async function refresh() {
    if (!token) return;
    const sequence = ++refreshSequence.current;
    const isCurrent = () => sequence === refreshSequence.current && token === current.current.token && selected === current.current.selected;
    try {
      const list = await request('/api/v1/tasks');
      const detail = selected ? await request(`/api/v1/tasks/${selected}`) : null;
      if (!isCurrent()) return;
      setTasks(list.items);
      setTask(detail);
      setError('');
    } catch (e) { if (isCurrent()) { setTask(null); setTasks([]); setError(String(e)); } }
  }
  useEffect(() => { fetch('/health/live').then(r => r.json()).then(r => setMode(r.mode)).catch(() => setMode('offline')); }, []);
  useEffect(() => { void refresh(); const id = setInterval(() => void refresh(), 3000); return () => clearInterval(id); }, [token, selected]);

  async function mutate(action: () => Promise<void>) {
    setBusy(true);
    try { await action(); await refresh(); } catch (e) { setError(String(e)); } finally { setBusy(false); }
  }
  async function create() {
    await mutate(async () => {
      const created = await request('/api/v1/tasks', 'POST', {kind, workflow: kind === 'repair' ? workflow : 'investigate', goal, project: 'demo', environment: 'lab', resource: mode === 'simulation' ? `demo-${crypto.randomUUID()}` : resource}, crypto.randomUUID());
      setSelected(created.id);
    });
  }
  async function artifact(id: string) {
    try {
      const response = await fetch(`/api/v1/artifacts/${id}`, {headers: {'Authorization': `Bearer ${token}`}});
      if (!response.ok) throw new Error('产物不可访问');
      const url = URL.createObjectURL(await response.blob());
      const a = document.createElement('a'); a.href = url; a.download = `${id}.json`; a.click(); URL.revokeObjectURL(url);
    } catch (e) { setError(String(e)); }
  }

  async function exportRecording(id: string) {
    try {
      const response = await fetch(`/api/v1/tasks/${id}/recording`, {headers: {Authorization: `Bearer ${token}`}});
      if (!response.ok) throw new Error('审计包不可导出，请检查权限及任务记录');
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement('a'); link.href = url; link.download = `audit-${id}.json`; link.click(); URL.revokeObjectURL(url);
    } catch (e) { setError(String(e)); }
  }

  return <main>
    <header><div><span className="eyebrow">ENGINEERING / OPERATIONS</span><h1>Agent 工作台</h1><p>从调查证据到受控执行，每一步都有记录。</p></div><span className="mode">{mode === 'simulation' ? '仿真环境 · 无真实变更' : mode}</span></header>
    <section className="access"><label htmlFor="token">访问凭证</label><input id="token" type="password" autoComplete="off" value={token} onChange={e => {setTask(null); setToken(e.target.value);}} placeholder="输入短期 Bearer Token；仅保存在当前页面内存"/><button onClick={() => {setToken(''); setTask(null); setTasks([]);}}>清除</button></section>
    {error && <div role="alert" className="error">{error}</div>}
    <button disabled={!token} onClick={() => setShowOps(v => !v)}>{showOps ? '收起运行概览' : '查看运行概览'}</button>{showOps && token && <OperationsPanel key={token} token={token}/>}
    <div className="grid"><aside><section className="panel"><h2>新建任务</h2><label htmlFor="kind">任务路径</label><select id="kind" value={kind} onChange={e => setKind(e.target.value)}><option value="repair">研发 · 修复与交付</option><option value="incident">运维 · 调查与恢复</option></select><label htmlFor="workflow">执行范围</label><select id="workflow" value={workflow} disabled={kind !== 'repair' || mode !== 'live'} onChange={e => setWorkflow(e.target.value)}><option value="investigate">调查与人工审阅</option><option value="repair_candidate">生成候选补丁并验证</option></select>{mode === 'live' && <><label htmlFor="resource">授权资源</label><input id="resource" value={resource} onChange={e => setResource(e.target.value)} placeholder="与运维配置的资源名一致"/></>}<label htmlFor="goal">目标</label><textarea id="goal" rows={4} value={goal} onChange={e => setGoal(e.target.value)}/><button className="primary" disabled={!token || busy || goal.length < 5} onClick={() => void create()}>提交任务</button><p className="hint">任务由持久化 Worker 推进。高影响动作等待批准。</p></section>
      <section className="panel"><h2>任务记录 <small>{tasks.length}</small></h2>{tasks.length === 0 && <p className="empty">尚无可访问任务</p>}{tasks.map(t => <button className={`task ${selected === t.id ? 'active' : ''}`} key={t.id} onClick={() => setSelected(t.id)}><span>{t.contract.goal}</span><small>{t.contract.kind} · {t.status}</small></button>)}</section></aside>
      <article className="panel details">{!task ? <div className="empty"><h2>选择一个任务</h2><p>查看执行记录、审批对象与验证产物。</p></div> : <>
        <div className="title"><h2>{task.contract.goal}</h2><span className="badge">{task.result || task.status}</span></div><p className="mono">{task.id}</p><p>等待原因：{task.waiting_reason || '无'} · 已计费用：${(task.spent_micro_usd / 1_000_000).toFixed(4)}</p>
        <div className="actions"><button disabled={busy || task.status === 'TERMINATED'} onClick={() => void mutate(async () => {await request(`/api/v1/tasks/${task.id}/cancel`, 'POST');})}>取消后续执行</button><button disabled={busy || task.status === 'TERMINATED'} onClick={() => void mutate(async () => {await request(`/api/v1/tasks/${task.id}/takeover`, 'POST');})}>人工接管</button>{task.taken_over && <button disabled={busy || task.cancelled} onClick={() => void mutate(async () => {await request(`/api/v1/tasks/${task.id}/resume`, 'POST', {expected_version: task.version});})}>恢复 Agent 执行</button>}</div>
        {task.contract.workflow === 'repair_candidate' && <RepairPanel key={`${task.id}:${token}`} taskId={task.id} token={token}/> }
        <h3>待审批动作</h3>{!task.approvals?.some(a => a.status === 'PENDING') && <p className="hint">当前没有待审批动作。批准需要 reviewer 权限。</p>}{task.approvals?.filter(a => a.status === 'PENDING').map(a => {const op = task.operations?.find(o => o.id === a.operation_id); return <section className="approval" key={a.id}><strong>{op?.tool} · {op?.resource}</strong><pre>{JSON.stringify(op?.parameters, null, 2)}</pre><small>批准仅适用于这些参数与版本；有效期至 {a.expires_at}</small><div className="actions">{(['approve', 'reject'] as const).map(d => <button disabled={busy} key={d} onClick={() => void mutate(async () => {await request(`/api/v1/approvals/${a.id}/decisions`, 'POST', {decision: d, expected_digest: a.payload_digest});})}>{d === 'approve' ? '批准此动作' : '拒绝'}</button>)}</div></section>;})}
        <h3>动作与确认结果</h3><div className="timeline">{task.operations?.map(o => <div className="operation" key={o.id}><div><strong>{o.tool}</strong><span className="badge">{o.status}</span></div><small className="mono">{o.id}</small>{o.recovery_status && <p>恢复：{o.recovery_status}</p>}{o.error && <p className="error-text">{o.error}</p>}<details><summary>查看参数</summary><pre>{JSON.stringify(o.parameters, null, 2)}</pre></details></div>)}</div>
        <EventPanel key={`events:${task.id}:${token}`} taskId={task.id} token={token}/>
        <button onClick={() => void exportRecording(task.id)}>导出审计包</button><p className="hint">包含动作参数及业务记录，向外分享前请检查敏感内容。</p>
        <h3>证据与产物</h3><ContextPanel key={`${task.id}:${token}`} taskId={task.id} token={token}/>{task.artifacts?.map(a => <button className="artifact" key={a.id} onClick={() => void artifact(a.id)}>{a.kind} <small>SHA256 {a.digest.slice(0, 12)}…</small></button>)}
      </>}</article></div>
    <footer>仿真结果不等于真实生产验证。UNKNOWN 表示结果尚未确认，不能视为失败后重做。</footer>
  </main>;
}

createRoot(document.getElementById('root')!).render(<React.StrictMode><App/></React.StrictMode>);
