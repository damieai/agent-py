import { useEffect, useState } from 'react';

type Attempt = {id: string; ordinal: number; state: string; summary: string | null; outcome: string | null; patch_artifact_id: string | null; verification_artifact_id: string | null; verification_count: number};
type Run = {state: string; source_digest: string; max_attempts: number; attempts: Attempt[]};

export function RepairPanel({taskId, token}: {taskId: string; token: string}) {
  const [run, setRun] = useState<Run | null>(null);
  const [diff, setDiff] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let alive = true;
    const controller = new AbortController();
    async function load() {
      try {
        const response = await fetch(`/api/v1/tasks/${taskId}/repair`, {headers: {Authorization: `Bearer ${token}`}, signal: controller.signal});
        const data = await response.json();
        if (!response.ok) throw new Error(data.error?.message || '修复记录不可访问');
        if (alive) { setRun(data.repair); setError(''); }
      } catch (e) { if (alive) { setRun(null); setDiff(''); setError(String(e)); } }
    }
    void load();
    const interval = setInterval(() => void load(), 3000);
    return () => { alive = false; controller.abort(); clearInterval(interval); };
  }, [taskId, token, revision]);

  async function preview(attempt: Attempt) {
    setBusy(true); setDiff('');
    try {
      const response = await fetch(`/api/v1/tasks/${taskId}/repair/attempts/${attempt.id}/patch`, {headers: {Authorization: `Bearer ${token}`}});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error?.message || '补丁不可访问');
      setDiff(data.diff); setError('');
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  }
  async function retry(attempt: Attempt) {
    setBusy(true);
    try {
      const response = await fetch(`/api/v1/tasks/${taskId}/repair/retry-verification`, {method: 'POST', headers: {Authorization: `Bearer ${token}`, 'Content-Type': 'application/json'}, body: JSON.stringify({attempt_id: attempt.id})});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error?.message || '验证重试被拒绝');
      setError(''); setRevision(v => v + 1);
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  }
  return <section className="repair-panel">
    <h3>候选修复与验证</h3>
    <p className="hint">最多生成 {run?.max_attempts ?? '配置限定次数的'} 个候选。通过回归验证后等待人工审阅，不会自动合并或部署。</p>
    {error && <p role="alert" className="error">{error}</p>}
    {!run ? <p>等待 Worker 冻结源码与证据。</p> : <>
      <p><strong>{run.state}</strong> · 源码摘要 <span className="mono">{run.source_digest.slice(0, 16)}…</span></p>
      {run.attempts.map(a => <section className="operation" key={a.id}>
        <div><strong>候选 {a.ordinal}</strong> <span className="badge">{a.outcome || a.state}</span></div>
        <p>{a.summary || '等待模型生成补丁'}</p>
        <small>容器验证启动次数：{a.verification_count} / 3</small>
        <div className="actions">
          {a.patch_artifact_id && <button disabled={busy} onClick={() => void preview(a)}>查看补丁差异</button>}
          {a.state === 'BLOCKED' && a.outcome === 'VERIFICATION_INTERRUPTED' && <button disabled={busy || a.verification_count >= 3} onClick={() => void retry(a)}>重试中断的验证（需 operator）</button>}
        </div>
      </section>)}
    </>}
    {diff && <details open><summary>候选源码差异</summary><pre className="patch-diff">{diff}</pre></details>}
  </section>;
}
