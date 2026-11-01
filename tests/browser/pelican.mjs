// Optional end-to-end QA: PELICAN_PYTHON=/path/python PLAYWRIGHT_MODULE=/path/playwright/index.mjs node tests/browser/pelican.mjs
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { mkdtemp, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const server = spawn(process.env.PELICAN_PYTHON || 'python3', ['-m', 'tests.pelican_browser_server'], { cwd: fileURLToPath(new URL('../..', import.meta.url)), stdio: ['ignore', 'pipe', 'pipe'] });
let serverErrors = '';
server.stderr.on('data', value => { serverErrors += value; });
const lines = createInterface({ input: server.stdout });
const config = await new Promise((resolve, reject) => { lines.once('line', line => resolve(JSON.parse(line))); server.once('exit', code => reject(new Error(`Test server exited ${code}: ${serverErrors}`))); });
const artifacts = await mkdtemp(join(tmpdir(), 'pelican-ui-'));
let browser;
try { browser = await chromium.launch({ headless: true, ...(process.env.PELICAN_CHROME ? { executablePath: process.env.PELICAN_CHROME } : {}) }); }
catch (error) { server.kill('SIGTERM'); throw error; }
const context = await browser.newContext({ acceptDownloads: true, viewport: { width: 1440, height: 1050 } });
// Existing sidebar network integration is unrelated to this test and never goes online.
await context.route('**/*', route => { const host = new URL(route.request().url()).hostname; return host === '127.0.0.1' ? route.continue() : route.abort(); });
const page = await context.newPage();
const errors = [];
page.on('pageerror', error => errors.push(error.message));
async function refresh() { await page.locator('#refresh-eval').click(); }
async function fill(model = 'mock-good') { await page.locator('#eval-url').fill(config.provider); await page.locator('#eval-key').fill('browser-test-key-only'); await page.locator('#eval-models').fill(model); }
async function untilSample(count) { await page.waitForFunction(n => Number(document.querySelector('#eval-sample-count').textContent.split(' ')[0]) >= n, count, { timeout: 15000 }); }
async function saveDownload(selector, name) { const pending = page.waitForEvent('download'); await page.locator(selector).click(); const item = await pending; assert.equal(item.suggestedFilename(), name); const path = join(artifacts, name); await item.saveAs(path); return path; }
try {
  await page.goto(config.url + '/pelican');
  if (await page.locator('#site-notice-dialog').isVisible()) await page.locator('#site-notice-dialog').evaluate(dialog => dialog.close());
  await page.locator('#eval-question option').waitFor({ state: 'attached' });
  assert.equal(await page.locator('#eval-models').inputValue(), 'gpt-6.1-sol');
  await fill('mock-animated');
  await page.locator('#eval-load-models').click();
  await page.locator('#eval-model-catalog').waitFor({ state: 'visible' });
  assert.equal(await page.locator('#eval-models').inputValue(), 'mock-animated');
  await page.locator('#eval-once').click();
  await untilSample(1);
  await page.getByRole('button', { name: '看图评分', exact: true }).first().waitFor({ state: 'visible' });
  await page.waitForFunction(() => ![...document.querySelectorAll('#eval-samples button')].find(b => b.textContent === '看图评分').disabled);
  assert.equal(await page.locator('#eval-key').inputValue(), '');
  assert.equal(await page.locator('#eval-samples img').first().evaluate(img => img.naturalWidth), 600);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await page.evaluate(() => scrollTo(0, 0));
  await page.screenshot({ path: join(artifacts, 'desktop.png'), fullPage: true });
  await page.getByRole('button', { name: '看图评分', exact: true }).first().click();
  const reviewImage = await page.locator('#review-image img').getAttribute('src');
  assert.ok(reviewImage.startsWith('data:image/png;base64,'));
  await page.locator('#review-reviewer').fill('reviewer-one');
  for (const d of 'ABCD') { await page.locator(`#score-${d}`).selectOption('3'); await page.locator(`#reason-${d}`).fill('本地测试：检查可见结构'); await page.locator(`#location-${d}`).fill('图中骑乘接触位置'); }
  await page.getByRole('button', { name: '保存人工评分', exact: true }).click();
  await page.locator('#review-dialog').waitFor({ state: 'hidden' });
  await page.locator('[data-eval-view="blind"]').click();
  await page.locator('#blind-reviewer').fill('reviewer-two');
  await page.locator('#blind-start').click();
  await page.getByRole('button', { name: '独立评分', exact: true }).click();
  assert.equal(await page.locator('#ai-review-settings').isVisible(), false);
  assert.equal((await page.locator('#review-dialog').innerText()).includes('mock-animated'), false);
  assert.equal(await page.locator('#review-image img').getAttribute('src'), reviewImage);
  for (const d of 'ABCD') { await page.locator(`#score-${d}`).selectOption('5'); await page.locator(`#reason-${d}`).fill('第二份独立测试评分'); await page.locator(`#location-${d}`).fill('主体及车架'); }
  await page.getByRole('button', { name: '保存人工评分', exact: true }).click();
  await page.locator('#review-dialog').waitFor({ state: 'hidden' });
  await page.locator('[data-eval-view="report"]').click();
  await page.locator('.eval-report-group').waitFor();
  assert.match(await page.locator('#eval-report').innerText(), /16/);
  const jsonPath = await saveDownload('#eval-view-report [data-export="json"]', 'pelican-report.json');
  const json = JSON.parse(await readFile(jsonPath, 'utf8'));
  assert.equal(json.samples[0].technical.browser_rendered, true);
  assert.equal(json.samples[0].reviews.length, 2);
  assert.ok(json.samples[0].render_evidence.data_url.startsWith('data:image/png;base64,'));
  assert.equal(json.samples[0].render_evidence.data_url, reviewImage);
  assert.equal(json.samples[0].render_evidence.capture_policy, 'first-loaded-frame-v1');
  assert.equal(JSON.stringify(json).includes('browser-test-key-only'), false);
  assert.equal(json.groups[0].total_mean, 16);
  await saveDownload('#eval-view-report [data-export="csv"]', 'pelican-report.csv');
  const htmlPath = await saveDownload('#eval-view-report [data-export="html"]', 'pelican-report.html');
  assert.ok((await readFile(htmlPath, 'utf8')).includes('data:image/png;base64,'));
  const offlineContext = await browser.newContext({ offline: true });
  const offlinePage = await offlineContext.newPage();
  await offlinePage.goto(pathToFileURL(htmlPath).href);
  assert.equal(await offlinePage.locator('img').first().evaluate(img => img.complete && img.naturalWidth > 0), true);
  assert.equal(await offlinePage.locator('body').evaluate(body => getComputedStyle(body).fontSize), '15px');
  await offlineContext.close();
  await page.locator('[data-eval-view="run"]').click();
  const svgDownload = page.waitForEvent('download'); await page.getByRole('button', { name: 'SVG ↓', exact: true }).first().click();
  const svg = await svgDownload; await svg.saveAs(join(artifacts, 'original.svg'));
  const originalSvg = await readFile(join(artifacts, 'original.svg'), 'utf8');
  assert.equal(originalSvg, json.samples[0].raw_output);
  assert.match(originalSvg, /@keyframes spin/);
  assert.match(originalSvg, /<animate attributeName="d"/);
  // Both AI protocols are opt-in; this request still uses the local mock provider.
  await page.getByRole('button', { name: '看图评分', exact: true }).first().click();
  await page.locator('#ai-review-settings summary').click();
  await page.locator('#ai-enabled').check(); await page.locator('#ai-vision').check();
  await page.locator('#ai-url').fill(config.provider); await page.locator('#ai-key').fill('browser-review-key'); await page.locator('#ai-model').fill('mock-judge');
  await page.getByRole('button', { name: '发送图片并请求 AI 评审', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('#review-status').textContent.includes('AI 评分已保存'));
  const calls = (await (await page.request.get(config.url + '/__test__/requests')).json()).requests;
  const judge = calls.find(call => call.body.model === 'mock-judge');
  assert.equal(judge.body.input[0].content[1].image_url, reviewImage);
  await page.locator('[data-close="review-dialog"]').click();
  // A loop persists across navigation and refresh, and cancellation keeps its partial.
  await fill('mock-slow');
  await page.locator('#eval-continuous').click();
  await page.waitForFunction(() => document.querySelector('#eval-fields').disabled);
  await page.goto(config.url + '/converter');
  await page.locator('#pelican-task-badge').waitFor({ state: 'visible' });
  await page.goto(config.url + '/pelican');
  await page.waitForFunction(() => document.querySelector('#eval-fields').disabled);
  await page.getByRole('button', { name: '停止任务', exact: true }).click();
  await page.waitForFunction(() => !document.querySelector('#eval-fields').disabled);
  const before = (await (await page.request.get(config.url + '/__test__/requests')).json()).requests.length;
  await page.reload();
  await page.locator('#eval-question option').waitFor({ state: 'attached' });
  const after = (await (await page.request.get(config.url + '/__test__/requests')).json()).requests.length;
  assert.equal(after, before);
  // Mobile wrapping and running controls are real browser checks.
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => scrollTo(0, 0));
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await page.screenshot({ path: join(artifacts, 'mobile.png'), fullPage: true });
  assert.equal(await page.locator('a[href="/pelican"][aria-current="page"]').count(), 1);
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ passed: true, artifacts, assertions: 'CSS/SMIL animated SVG render + unchanged download; identical PNG for human/blind/AI reviews; two independent reviews; JSON/CSV/HTML/SVG downloads; cross-page loop; stop; reload; desktop/mobile; no runtime errors' }));
} catch (error) {
  await page.screenshot({ path: join(artifacts, 'failure.png'), fullPage: true });
  console.error(JSON.stringify({ artifacts, uiError: await page.locator('#eval-error').textContent(), reviewStatus: await page.locator('#review-status').textContent(), errors, serverErrors: serverErrors.slice(-6000) }));
  throw error;
} finally {
  await browser.close();
  server.kill('SIGTERM');
}
