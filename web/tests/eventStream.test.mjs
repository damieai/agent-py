import assert from 'node:assert/strict';
import test from 'node:test';
import { EventParser, acceptEvent, consumeStream } from '../src/eventStream.ts';

test('SSE handles every split position, CRLF, comments and multiple frames', () => {
  const text = ': heartbeat\r\nid: 1\r\nevent: task.created\r\ndata: {"message":"中文"}\r\n\r\nid: 2\ndata: {}\n\n';
  for (let split = 0; split < text.length; split++) {
    const parser = new EventParser();
    const events = [...parser.push(text.slice(0, split)), ...parser.push(text.slice(split))];
    assert.equal(events.length, 2);
    assert.equal(events[0].type, 'task.created');
    assert.equal(JSON.parse(events[0].data).message, '中文');
    assert.equal(events[1].id, '2');
  }
});

test('multiline data and incomplete frames never advance the cursor', () => {
  const parser = new EventParser();
  assert.deepEqual(parser.push('id: 1\ndata: {\ndata: "ok": true}\n'), []);
  const [event] = parser.push('\n');
  assert.equal(acceptEvent(event, 0).payload.ok, true);
  assert.equal(acceptEvent(event, 1), null);
  assert.throws(() => acceptEvent({...event, id: '3'}, 1), /不连续/);
  assert.throws(() => acceptEvent({...event, id: '9007199254740992'}, 1), /无效/);
  assert.throws(() => acceptEvent({...event, data: '[]'}, 0), /格式/);
});

test('bounded frame memory rejects oversized input', () => {
  assert.throws(() => new EventParser().push('x'.repeat(1_000_001)), /长度/);
  const parser = new EventParser();
  parser.push('data: ' + 'x'.repeat(600_000) + '\n');
  assert.throws(() => parser.push('data: ' + 'x'.repeat(600_000) + '\n'), /长度/);
});

test('UTF-8 may be split at every byte without losing Chinese data', async () => {
  const bytes = new TextEncoder().encode('id: 1\ndata: {"text":"取消后续执行"}\n\n');
  const response = new Response(new ReadableStream({start(controller) {
    for (const byte of bytes) controller.enqueue(new Uint8Array([byte]));
    controller.close();
  }}), {headers: {'content-type': 'text/event-stream'}});
  const events = [];
  await consumeStream(response, new AbortController().signal, event => events.push(event));
  assert.equal(JSON.parse(events[0].data).text, '取消后续执行');
});

test('aborting a pending read closes the stream without late callbacks', async () => {
  let cancelled = false;
  const response = new Response(new ReadableStream({cancel() {cancelled = true;}}), {headers: {'content-type': 'text/event-stream'}});
  const controller = new AbortController();
  const pending = consumeStream(response, controller.signal, () => assert.fail('late event'));
  controller.abort();
  await pending;
  assert.equal(cancelled, true);
});

test('broken connection retains only complete events for replay on resume', async () => {
  const response = new Response('id: 1\ndata: {}\n\nid: 2\ndata: {', {headers: {'content-type': 'text/event-stream'}});
  let cursor = 0;
  await consumeStream(response, new AbortController().signal, event => {cursor = acceptEvent(event, cursor).sequence;});
  assert.equal(cursor, 1);
  assert.equal(acceptEvent({id: '2', type: 'task.cancelled', data: '{}'}, cursor).sequence, 2);
});

test('response content type and malformed UTF-8 fail closed', async () => {
  await assert.rejects(() => consumeStream(new Response('<html>'), new AbortController().signal, () => {}), /格式/);
  const response = new Response(new Uint8Array([255]), {headers: {'content-type': 'text/event-stream'}});
  await assert.rejects(() => consumeStream(response, new AbortController().signal, () => {}));
});
