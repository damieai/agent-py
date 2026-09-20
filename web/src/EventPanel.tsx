import { useEffect, useState } from 'react';
import { acceptEvent, consumeStream, type TimelineEvent } from './eventStream';

export function EventPanel({taskId, token, onAccessLost}: {taskId: string; token: string; onAccessLost: () => void}) {
  const [items, setItems] = useState<TimelineEvent[]>([]);
  const [status, setStatus] = useState('连接中');
  const [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    let stopped = false, cursor = 0, delay = 1000;
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function connect() {
      if (controller.signal.aborted || stopped) return;
      setStatus(cursor ? '续传中' : '连接中');
      try {
        const response = await fetch(`/api/v1/tasks/${taskId}/events`, {
          headers: {Authorization: `Bearer ${token}`, 'Last-Event-ID': String(cursor)}, signal: controller.signal,
        });
        if ([401, 403, 404].includes(response.status)) {
          if (!controller.signal.aborted) onAccessLost();
          stopped = true; setItems([]); throw new Error('事件访问已失效，请检查凭证和授权');
        }
        if (response.status === 409) { stopped = true; throw new Error('事件历史不可续传，请重新打开任务'); }
        if (!response.ok) throw new Error('事件服务暂时不可用');
        if (controller.signal.aborted) return;
        setStatus('已连接'); setError('');
        await consumeStream(response, controller.signal, event => {
          if (event.type === 'access_revoked') { if (!controller.signal.aborted) onAccessLost(); stopped = true; setItems([]); setError('凭证过期或授权已撤销'); setStatus('已停止'); return; }
          if (event.type === 'history_unavailable') { stopped = true; setError('事件历史存在缺口，请重新打开任务'); setStatus('已停止'); return; }
          if (event.type === 'stream.closed') { stopped = true; setStatus('任务事件已结束'); return; }
          if (stopped) return;
          let accepted: TimelineEvent | null;
          try { accepted = acceptEvent(event, cursor); }
          catch (e) { stopped = true; throw e; }
          if (accepted) {
            cursor = accepted.sequence; delay = 1000;
            setItems(old => [...old, accepted].slice(-200));
          }
        });
      } catch (e) {
        if (!controller.signal.aborted) { setError(String(e)); setStatus(stopped ? '已停止' : '等待重连'); }
      }
      if (!controller.signal.aborted && !stopped) {
        timer = setTimeout(() => void connect(), delay);
        delay = Math.min(delay * 2, 15000);
      }
    }
    void connect();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [taskId, token, onAccessLost]);
  return <section className="event-panel"><h3>事件时间线 <small>{status}</small></h3>
    <p className="hint">显示最近 200 条；重连按事件序号续传。完整记录可导出为审计包。</p>
    {error && <p role="alert" className="error">{error}</p>}
    <div className="event-list">{items.map(event => <details key={event.sequence}>
      <summary><span className="mono">#{event.sequence}</span> {event.type}</summary>
      <pre>{JSON.stringify(event.payload, null, 2)}</pre>
    </details>)}</div>
  </section>;
}
