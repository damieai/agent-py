import { randomBytes } from 'node:crypto';
import { defineConfig, devices } from '@playwright/test';

// A fresh control credential per run; only the isolated fixture process receives it.
process.env.E2E_CONTROL_KEY ??= randomBytes(32).toString('hex');
export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 30000,
  globalTimeout: 120000,
  expect: { timeout: 10000 },
  outputDir: '../.runtime/browser/results',
  reporter: [['list'], ['html', { outputFolder: '../.runtime/browser/report', open: 'never' }]],
  use: {
    baseURL: 'http://127.0.0.1:18765',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: '.venv/bin/python scripts/serve_e2e.py',
    cwd: '..',
    wait: { stdout: /E2E_SERVER_READY/ },
    timeout: 20000,
    reuseExistingServer: false,
    env: { E2E_FIXTURE: '1', E2E_CONTROL_KEY: process.env.E2E_CONTROL_KEY },
    gracefulShutdown: { signal: 'SIGTERM', timeout: 5000 },
  },
});
