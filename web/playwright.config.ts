import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { defineConfig, devices } from '@playwright/test'

// Independent data and credentials: never load local or production .env files.
process.env.LABELCHECK_E2E_DATA_DIR ??= mkdtempSync(join(tmpdir(), 'labelcheck-e2e-'))
process.env.ACCESS_TOKEN = 'e2e-access'
process.env.ADMIN_TOKEN = 'e2e-admin'
const serverEnv = {
  DATA_DIR: process.env.LABELCHECK_E2E_DATA_DIR,
  ACCESS_TOKEN: 'e2e-access', ADMIN_TOKEN: 'e2e-admin',
  VITE_ACCESS_TOKEN: 'e2e-access', VITE_ADMIN_TOKEN: 'e2e-admin',
  READER_PROVIDER: 'fake', READER_API_KEY: '', OPENAI_API_KEY: '',
  PUBLIC_BASE_PATH: '',
}

/**
 * End-to-end suite (PRD §10 M7). Runs against the fake reader, so it is
 * deterministic, free and offline; both servers start here, so `npm run e2e`
 * works from a cold checkout.
 */
export default defineConfig({
  testDir: './e2e',
  globalTeardown: './e2e-cleanup.ts',
  // README captures, not tests; see e2e/screenshots.spec.ts.
  testIgnore: process.env.SCREENSHOTS ? [] : ['**/screenshots.spec.ts'],
  fullyParallel: false, // one SQLite store, and the specs seed it
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI ? 'list' : [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL: 'http://localhost:15273',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
    { name: 'firefox', use: { ...devices['Desktop Firefox'] } },
    { name: 'webkit', use: { ...devices['Desktop Safari'] } },
  ],
  webServer: [
    {
      command:
        'cd ../api && .venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 18031',
      url: 'http://127.0.0.1:18031/api/health',
      reuseExistingServer: false,
      env: serverEnv,
      timeout: 60_000,
    },
    {
      // Points the dev proxy at the e2e API, not a dev server on 8000.
      command: 'DEV_API_URL=http://127.0.0.1:18031 npm run dev -- --port 15273 --strictPort',
      url: 'http://localhost:15273',
      reuseExistingServer: false,
      env: serverEnv,
      timeout: 60_000,
    },
  ],
})
