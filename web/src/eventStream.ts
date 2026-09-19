export type StreamEvent = {id: string; type: string; data: string};

export class EventParser {
  private buffer = '';
  private id = '';
  private type = '';
  private data: string[] = [];
  private size = 0;
  push(text: string): StreamEvent[] {
    this.buffer += text;
    const events: StreamEvent[] = [];
    while (true) {
      const end = this.buffer.search(/[\r\n]/);
      if (end < 0 || (this.buffer[end] === '\r' && end === this.buffer.length - 1)) break;
      const line = this.buffer.slice(0, end);
      const width = this.buffer[end] === '\r' && this.buffer[end + 1] === '\n' ? 2 : 1;
      this.buffer = this.buffer.slice(end + width);
      this.size += line.length;
      if (this.size > 1_000_000) throw new Error('事件超过长度限制');
      if (!line) {
        if (this.data.length) events.push({id: this.id, type: this.type || 'message', data: this.data.join('\n')});
        this.id = ''; this.type = ''; this.data = []; this.size = 0;
        continue;
      }
      if (line.startsWith(':')) continue;
      const colon = line.indexOf(':');
      const field = colon < 0 ? line : line.slice(0, colon);
      let value = colon < 0 ? '' : line.slice(colon + 1);
      if (value.startsWith(' ')) value = value.slice(1);
      if (field === 'data') this.data.push(value);
      if (field === 'event') this.type = value;
      if (field === 'id' && !value.includes('\0')) this.id = value;
    }
    if (this.buffer.length + this.size > 1_000_000) throw new Error('事件超过长度限制');
    return events;
  }
}

export type TimelineEvent = {sequence: number; type: string; payload: Record<string, unknown>};
export function acceptEvent(event: StreamEvent, cursor: number): TimelineEvent | null {
  if (!/^\d+$/.test(event.id)) throw new Error('事件缺少有效序号');
  const sequence = Number(event.id);
  if (!Number.isSafeInteger(sequence) || sequence < 1) throw new Error('事件序号无效');
  if (sequence <= cursor) return null;
  if (sequence !== cursor + 1) throw new Error('事件历史不连续，请重新打开任务');
  const payload: unknown = JSON.parse(event.data);
  if (payload === null || Array.isArray(payload) || typeof payload !== 'object') throw new Error('事件内容格式无效');
  return {sequence, type: event.type, payload: payload as Record<string, unknown>};
}

export async function consumeStream(response: Response, signal: AbortSignal, receive: (event: StreamEvent) => void) {
  if (!response.body || !response.headers.get('content-type')?.includes('text/event-stream')) throw new Error('事件流响应格式无效');
  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8', {fatal: true});
  const parser = new EventParser();
  const cancel = () => { void reader.cancel().catch(() => undefined); };
  signal.addEventListener('abort', cancel, {once: true});
  try {
    while (!signal.aborted) {
      const {value, done} = await reader.read();
      if (signal.aborted) return;
      if (done) { decoder.decode(); return; }
      for (const event of parser.push(decoder.decode(value, {stream: true}))) {
        if (signal.aborted) return;
        receive(event);
      }
    }
  } finally {
    signal.removeEventListener('abort', cancel);
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}
