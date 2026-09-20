import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

type Session = { id: string; owner: string; reviewer: string; outsider: string };
const control = () => ({ 'X-E2E-Key': process.env.E2E_CONTROL_KEY! });
async function session(request: APIRequestContext): Promise<Session> {
  const response = await request.post('/__test/session', { headers: control() });
  expect(response.ok()).toBeTruthy();
  return response.json();
}
async function login(page: Page, token: string) {
  await page.getByLabel('访问凭证').fill(token);
}
async function create(page: Page, goal: string) {
  await page.getByLabel('目标', { exact: true }).fill(goal);
  const response = page.waitForResponse(r => r.url().endsWith('/api/v1/tasks') && r.request().method() === 'POST');
  await page.getByRole('button', { name: '提交任务', exact: true }).click();
  const result = await response;
  expect(result.status()).toBe(202);
  const task = await result.json();
  await expect(page.locator('article h2')).toHaveText(goal);
  return task.id as string;
}
async function tick(request: APIRequestContext, identity: Session, id: string) {
  const response = await request.post(`/__test/${identity.id}/tick/${id}`, { headers: control() });
  expect(response.ok()).toBeTruthy();
  return response.json();
}
async function untilApproval(request: APIRequestContext, identity: Session, id: string) {
  for (let i = 0; i < 8; i++) {
    const result = await tick(request, identity, id);
    if (result.wait === 'APPROVAL') return;
    expect(result.done).not.toBe(true);
  }
  throw new Error('No approval reached within bounded simulation steps');
}

test('real API approval permissions, two approvals, completion and audit download', async ({ page, request }) => {
  test.setTimeout(60000);
  const identity = await session(request);
  await page.goto('/');
  await login(page, identity.owner);
  const id = await create(page, 'Browser queue repair approval flow');
  await untilApproval(request, identity, id);
  await expect(page.locator('.approval')).toContainText('merge_pr');
  const denied = page.waitForResponse(r => r.url().includes('/decisions'));
  await page.getByRole('button', { name: '批准此动作' }).click();
  expect((await denied).status()).toBe(403);
  await expect(page.getByRole('alert')).toBeVisible();
  await login(page, identity.reviewer);
  await page.locator('button.task', { hasText: 'Browser queue repair approval flow' }).click();
  for (const tool of ['merge_pr', 'deploy']) {
    await expect(page.locator('.approval')).toContainText(tool);
    const approved = page.waitForResponse(r => r.url().includes('/decisions'));
    await page.getByRole('button', { name: '批准此动作' }).click();
    expect((await approved).status()).toBe(200);
    await tick(request, identity, id);
    if (tool === 'merge_pr') await untilApproval(request, identity, id);
  }
  expect((await tick(request, identity, id)).done).toBe(true);
  await expect(page.locator('.title .badge')).toHaveText('SUCCESS');
  await expect(page.locator('.event-list')).toContainText('task.terminated');
  const download = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出审计包', exact: true }).click();
  const stream = await (await download).createReadStream();
  const chunks: Buffer[] = [];
  for await (const chunk of stream!) chunks.push(Buffer.from(chunk));
  const recording = JSON.parse(Buffer.concat(chunks).toString());
  expect(recording.body.schema).toBe('recording-v2');
  expect(recording.body.task.id).toBe(id);
  expect(recording.body.operations).toHaveLength(4);
  expect(recording.body.operations.every((op: {status: string}) => op.status === 'SUCCEEDED')).toBeTruthy();
});

test('takeover, version-bound resume and cancellation through the UI', async ({ page, request }) => {
  const identity = await session(request);
  await page.goto('/');
  await login(page, identity.owner);
  const id = await create(page, 'Browser takeover and cancellation');
  await page.getByRole('button', { name: '人工接管' }).click();
  await expect(page.getByRole('button', { name: '恢复 Agent 执行' })).toBeVisible();
  expect((await tick(request, identity, id)).wait).toBe('HUMAN_TAKEOVER');
  const resumed = page.waitForResponse(r => r.url().endsWith('/resume'));
  await page.getByRole('button', { name: '恢复 Agent 执行' }).click();
  expect((await resumed).status()).toBe(200);
  await expect(page.getByRole('button', { name: '恢复 Agent 执行' })).toHaveCount(0);
  const cancelled = page.waitForResponse(r => r.url().endsWith('/cancel'));
  await page.getByRole('button', { name: '取消后续执行' }).click();
  expect((await cancelled).status()).toBe(200);
  expect((await tick(request, identity, id)).result).toBe('CANCELLED');
  await expect(page.locator('.title .badge')).toHaveText('CANCELLED');
  await expect(page.locator('.operation')).toHaveCount(0);
});

test('SSE grant revocation clears task, evidence and credentials', async ({ page, request }) => {
  const identity = await session(request);
  await page.goto('/');
  await login(page, identity.owner);
  await create(page, 'Browser queue evidence revocation');
  await expect(page.locator('.event-list')).toContainText('task.created');
  await page.getByText('查看当前可用证据', { exact: true }).click();
  await page.getByRole('button', { name: '刷新证据预览' }).click();
  await page.locator('.evidence summary').click();
  await expect(page.locator('.evidence pre')).toContainText('BROWSER_EVIDENCE');
  expect((await request.post(`/__test/${identity.id}/revoke`, { headers: control() })).ok()).toBeTruthy();
  await expect(page.getByLabel('访问凭证')).toHaveValue('');
  await expect(page.locator('button.task')).toHaveCount(0);
  await expect(page.locator('.evidence')).toHaveCount(0);
  await expect(page.locator('.event-list')).toHaveCount(0);
});

test('credential clear removes the list immediately and stores no credentials', async ({ page, request }) => {
  const identity = await session(request);
  await page.goto('/');
  await login(page, identity.owner);
  await create(page, 'Browser private task label');
  await expect(page.locator('button.task')).toHaveCount(1);
  // Editing the input to empty must behave the same as pressing the clear button.
  await login(page, '');
  await expect(page.locator('button.task')).toHaveCount(0);
  await expect(page.locator('article h2')).toHaveText('选择一个任务');
  expect(await page.evaluate(() => [localStorage.length, sessionStorage.length])).toEqual([0, 0]);
  await page.reload();
  await expect(page.getByLabel('访问凭证')).toHaveValue('');
});

test('late create response cannot select a task in a different credential session', async ({ page, request }) => {
  const first = await session(request);
  const second = await session(request);
  await page.goto('/');
  await login(page, first.owner);
  let release!: () => void;
  let captured!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  const ready = new Promise<void>(resolve => { captured = resolve; });
  await page.route('**/api/v1/tasks', async route => {
    if (route.request().method() !== 'POST') return route.continue();
    const response = await route.fetch(); // Real request/DB commit; delay only the browser response.
    captured();
    await gate;
    await route.fulfill({ response });
  });
  await page.getByRole('button', { name: '提交任务', exact: true }).click();
  await ready;
  const details: string[] = [];
  page.on('request', r => { if (/\/api\/v1\/tasks\/[^/]+$/.test(r.url())) details.push(r.url()); });
  await login(page, second.owner);
  const completed = page.waitForResponse(r => r.request().method() === 'POST' && r.url().endsWith('/api/v1/tasks'));
  release();
  await (await completed).finished();
  // Cover one normal refresh interval after delivering the stale mutation response.
  await page.waitForTimeout(3500);
  expect(details).toEqual([]);
  await expect(page.locator('article h2')).toHaveText('选择一个任务');
  await expect(page.getByRole('alert')).toHaveCount(0);
});

test('late audit download is discarded after clearing and re-entering the same credentials', async ({ page, request }) => {
  const identity = await session(request);
  await page.goto('/');
  await login(page, identity.owner);
  await create(page, 'Browser download session isolation');
  let release!: () => void;
  let captured!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  const ready = new Promise<void>(resolve => { captured = resolve; });
  await page.route('**/recording', async route => {
    const response = await route.fetch();
    captured();
    await gate;
    await route.fulfill({ response });
  });
  let downloads = 0;
  page.on('download', () => { downloads++; });
  await page.getByRole('button', { name: '导出审计包', exact: true }).click();
  await ready;
  await page.getByRole('button', { name: '清除', exact: true }).click();
  await login(page, identity.owner);
  const completed = page.waitForResponse(r => r.url().endsWith('/recording'));
  release();
  await (await completed).finished();
  // A negative assertion needs an observation window after the delayed body arrives.
  await page.waitForTimeout(500);
  expect(downloads).toBe(0);
});

test('an incomplete credential stays editable and a valid replacement works', async ({ page, request }) => {
  const identity = await session(request);
  await page.goto('/');
  await login(page, 'incomplete-token');
  await expect(page.getByRole('alert')).toBeVisible();
  await expect(page.getByLabel('访问凭证')).toHaveValue('incomplete-token');
  await login(page, identity.owner);
  await create(page, 'Browser valid credential replacement');
  await expect(page.getByRole('alert')).toHaveCount(0);
});
