import { expect, test } from '@playwright/test'

/**
 * README screenshots. Not a test of behaviour: it walks the same fixture
 * store the e2e suite seeds and saves what a reviewer sees. Opt in with
 * SCREENSHOTS=1, which playwright.config.ts uses to stop ignoring this file:
 *
 *   SCREENSHOTS=1 npx playwright test e2e/screenshots.spec.ts
 */

const API = 'http://127.0.0.1:8031'
const OUT = '../docs/screenshots'

test.use({ viewport: { width: 1440, height: 960 } })

test.beforeAll(async ({ request }) => {
  const reset = await request.post(`${API}/api/fixtures`, {
    headers: {
      Authorization: `Bearer ${process.env.ADMIN_TOKEN ?? ''}`,
      'Content-Type': 'application/json',
    },
    data: { mode: 'reset' },
  })
  expect(reset.ok()).toBeTruthy()
})

async function verifyAll(page: import('@playwright/test').Page) {
  await page.goto('/inbox')
  await page.getByRole('button', { name: 'Run AI verification on all' }).click()
  await page.getByRole('button', { name: 'Run verification' }).click()
  await expect(page.getByText(/have not been checked/)).toBeHidden({ timeout: 120_000 })
}

async function openFirstUndecidedFailure(page: import('@playwright/test').Page) {
  await page.goto('/inbox?filter=fail')
  const row = page.locator('.queue-item').filter({ hasNotText: /Accepted|Returned/ }).first()
  await row.locator('.queue-main').click()
  await row.getByRole('link', { name: 'Review', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Application versus label' })).toBeVisible()
}

test('inbox with mixed verdicts', async ({ page }) => {
  await page.goto('/inbox')
  await expect(page.getByRole('heading', { name: 'Review inbox' })).toBeVisible()
  await page.waitForLoadState('networkidle')
  await page.screenshot({ path: `${OUT}/inbox.png` })
})

test('single-label result, field by field', async ({ page }) => {
  await verifyAll(page)
  await openFirstUndecidedFailure(page)
  await page.waitForLoadState('networkidle')
  await page.screenshot({ path: `${OUT}/record-detail.png` })
})

test('override with the audit entry', async ({ page }) => {
  await openFirstUndecidedFailure(page)
  await page.getByRole('button', { name: 'Accept', exact: true }).click()
  await expect(page.getByText('This record did not pass.')).toBeVisible()
  await page.screenshot({ path: `${OUT}/override-confirm.png` })
  await page.getByRole('button', { name: 'Confirm acceptance' }).click()
  await expect(page.getByText(/^Accepted by/)).toBeVisible()
  await page.waitForLoadState('networkidle')
  await page.screenshot({ path: `${OUT}/override-recorded.png` })
})

test('batch CSV import', async ({ page }) => {
  await page.goto('/batch')
  await page.getByRole('button', { name: 'Load bundled sample batch' }).click()
  await expect(page.locator('.queue-item, tbody tr').first()).toBeVisible({ timeout: 60_000 })
  await page.waitForLoadState('networkidle')
  await page.screenshot({ path: `${OUT}/batch-import.png` })
})
