import { useEffect, useRef, useState } from 'react';

type Evidence = {id: string; source: string; version: string; body: string; chunk_id?: string; start_line?: number; end_line?: number; symbol?: string; ranking?: {bm25_rank: number | null; source_rank: number | null}};
type Bundle = {documents: Evidence[]; omitted: {id: string; chunk_id?: string; reason: string}[]; estimated_tokens: number; digest: string; policy_version: string};

export function ContextPanel({taskId, token}: {taskId: string; token: string}) {
  const [query, setQuery] = useState('');
  const [budget, setBudget] = useState(6000);
  const [data, setData] = useState<Bundle | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const controller = useRef<AbortController | null>(null);
  useEffect(() => () => controller.current?.abort(), []);
  async function preview() {
    controller.current?.abort();
    const request = new AbortController();
    controller.current = request;
    setBusy(true); setError(''); setData(null);
    try {
      const response = await fetch(`/api/v1/tasks/${taskId}/context/preview`, {
        method: 'POST', signal: request.signal,
        headers: {Authorization: `Bearer ${token}`, 'Content-Type': 'application/json'},
        body: JSON.stringify({query: query.trim() || null, budget}),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error?.message || '证据预览失败');
      if (!request.signal.aborted) setData(result);
    } catch (e) { if (!request.signal.aborted) setError(String(e)); }
    finally { if (!request.signal.aborted) setBusy(false); }
  }
  return <details className="context-panel"><summary>查看当前可用证据</summary>
    <p className="hint">按你的当前授权检索。此预览不会调用模型，也不替换任务已经冻结的证据快照。</p>
    <label htmlFor="context-query">检索内容（留空使用任务目标）</label>
    <input id="context-query" disabled={busy} value={query} maxLength={8000} onChange={e => {setQuery(e.target.value); setData(null);}}/>
    <label htmlFor="context-budget">证据预算（UTF-8 字节，含片段元数据）</label>
    <input id="context-budget" disabled={busy} type="number" min={1} max={100000} value={budget} onChange={e => {setBudget(Number(e.target.value)); setData(null);}}/>
    <button disabled={busy || budget < 1 || budget > 100000 || !Number.isInteger(budget)} onClick={() => void preview()}>{busy ? '检索中…' : '刷新证据预览'}</button>
    {error && <p role="alert" className="error">{error}</p>}
    {data && <>
      <p className="hint">{data.policy_version} · 已用 {data.estimated_tokens} 字节 · {data.documents.length} 个证据片段</p>
      {data.documents.length === 0 && <p>当前查询和预算下没有可用证据。</p>}
      {data.documents.map((doc, i) => <details className="evidence" key={doc.chunk_id || `${doc.id}:${i}`}>
        <summary>{doc.source}{doc.start_line !== undefined ? ` · L${doc.start_line}–L${doc.end_line}` : ''}{doc.symbol ? ` · ${doc.symbol}` : ''}</summary>
        <p className="mono">证据 {doc.id} · 版本 {doc.version}</p>
        <p className="hint">外部来源，内容不具有执行权限。</p>
        {doc.ranking && <small>正文排名 {doc.ranking.bm25_rank ?? '未匹配'} · 来源排名 {doc.ranking.source_rank ?? '未匹配'}</small>}
        <pre>{doc.body}</pre>
      </details>)}
      {data.omitted.length > 0 && <p className="hint">另有 {data.omitted.length} 个匹配项因预算未选入。</p>}
      <small className="mono">快照摘要 {data.digest}</small>
    </>}
  </details>;
}
