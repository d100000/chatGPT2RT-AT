/** Real app.js grouped submission flow with synthetic records and no network. */
import assert from 'node:assert/strict';
import test from 'node:test';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';
import * as requestLimits from '../rt_extractor/static/request-limits.mjs';
import * as browserResults from '../rt_extractor/static/browser-results.mjs';
import { createBatchQueue } from '../rt_extractor/static/batch-queue.mjs';

const NOW = Date.parse('2026-10-23T00:00:00Z');
const response = (status, payload, retryAfter) => ({
  ok: status >= 200 && status < 300, status,
  headers: { get: (name) => name.toLowerCase() === 'retry-after' ? retryAfter ?? null : null },
  json: async () => structuredClone(payload),
});
const settle = () => new Promise((resolve) => setImmediate(resolve));

function clock() {
  let time = NOW, next = 0;
  const timers = new Map();
  return {
    Date: class extends Date { static now() { return time; } },
    now: () => time,
    elapse(ms) { time += ms; },
    setTimeout(fn, delay = 0) { const id = ++next; timers.set(id, { fn, at: time + delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
    async advance(ms) {
      time += ms;
      for (let iterations = 0; iterations < 100; iterations += 1) {
        const pending = [...timers.entries()].find(([, timer]) => timer.at <= time);
        if (!pending) return;
        timers.delete(pending[0]);
        await pending[1].fn();
        await settle();
      }
      throw new Error('Unexpected timer retry loop');
    },
  };
}

class Element {
  constructor(tagName = 'div') {
    this.tagName = tagName.toUpperCase(); this.value = ''; this.disabled = false; this.hidden = false;
    this.open = false; this.returnValue = ''; this.dataset = {}; this.children = []; this.listeners = new Map();
    this.attributes = new Map(); this.ownText = '';
    this.classList = { add() {}, remove() {}, toggle() {} };
  }
  set textContent(value) { this.ownText = String(value ?? ''); this.children = []; }
  get textContent() { return this.ownText + this.children.map((child) => child.textContent ?? child).join(''); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; this.ownText = ''; }
  setAttribute(name, value) { this.attributes.set(name, value); }
  removeAttribute(name) { this.attributes.delete(name); }
  addEventListener(type, listener, options = {}) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push({ listener, once: Boolean(options.once) });
  }
  removeEventListener(type, listener) {
    this.listeners.set(type, (this.listeners.get(type) || []).filter((entry) => entry.listener !== listener));
  }
  async dispatch(type) {
    if (type === 'click' && this.disabled) return;
    for (const entry of [...(this.listeners.get(type) || [])]) {
      if (entry.once) this.removeEventListener(type, entry.listener);
      await entry.listener({ type, target: this, preventDefault() {} });
    }
  }
  click() { return this.dispatch('click'); }
  showModal() { this.open = true; }
  close(value) {
    if (!this.open) return Promise.resolve();
    if (value !== undefined) this.returnValue = value;
    this.open = false;
    return this.dispatch('close');
  }
  remove() {}
  focus() {}
}

async function uiPage(file, fetchImpl, options = {}) {
  const time = clock(), ids = new Map(), created = [], listeners = new Map(), downloads = [];
  const byId = (id) => { if (!ids.has(id)) ids.set(id, new Element()); return ids.get(id); };
  const body = new Element('body'); body.dataset.page = file === 'app.js' ? 'extractor' : 'converter';
  const document = {
    body, hidden: false, getElementById: byId,
    querySelector(selector) {
      if (selector === 'meta[name="rt-csrf"]') return { content: 'synthetic-csrf' };
      if (selector === 'dialog[open]') return [...ids.entries()].find(([id, element]) => id.endsWith('-dialog') && element.open)?.[1] || null;
      return null;
    },
    createElement(tag) { const element = new Element(tag); created.push(element); return element; },
    createDocumentFragment: () => new Element(),
    addEventListener(type, listener) { if (!listeners.has(type)) listeners.set(type, []); listeners.get(type).push(listener); },
    dispatchEvent(event) { for (const listener of listeners.get(event.type) || []) listener(event); },
  };
  byId('concurrency').value = '2';
  const map = new Map();
  const storage = options.storage || { get length() { return map.size; }, key: (index) => [...map.keys()][index], getItem: (key) => map.get(key) ?? null, setItem: (key, value) => map.set(key, value), removeItem: (key) => map.delete(key) };
  const store = browserResults.createBrowserResultStore({ owner: 'synthetic-owner-123456', storage, now: time.now });
  if (options.cached) { store.save(options.cached.batch, options.cached.records); if (options.cached.collected) store.markCollected(options.cached.batch.id); }
  const collector = browserResults.createResultCollector({ store, now: time.now, requireKnown: true });
  const context = vm.createContext({
    document, window: { addEventListener() {} }, Date: time.Date,
    CustomEvent: class { constructor(type, init = {}) { this.type = type; this.detail = init.detail; } },
    setTimeout: time.setTimeout, clearTimeout: time.clearTimeout,
    fetch: fetchImpl, AbortController, navigator: {}, URL, Blob, TextEncoder,
    ...requestLimits, ...browserResults,
    createBatchQueue: (text, settings) => createBatchQueue(text, settings, { now: time.now }),
    resultDownload(state, kind, jobId) { const result = browserResults.resultDownload(state, kind, jobId); downloads.push(result); return result; },
    getBrowserResults: () => store,
    collectBatchResults: (batch, request) => collector.collect(batch, request),
    isBatchCollected: (id) => collector.settled(id), resetResultCollection: () => collector.reset(), withBrowserResultLock: options.lock || ((operation) => operation()),
  });
  const source = readFileSync(new URL(`../rt_extractor/static/${file}`, import.meta.url), 'utf8').replace(/^import[^\n]*\n/gm, '');
  vm.runInContext(source, context, { filename: file }); await settle();
  return { byId, created, document, time, listeners, store, storage, downloads };
}

const input = (count) => Array.from({ length: count }, (_, index) => `synthetic-${index}@example.com----synthetic-password----JBSWY3DPEHPK3PXP`).join('\n');
function resultFor(body, number, { running = false, missing = false } = {}) {
  const lines = body.accounts_text.split('\n');
  const rows = lines.map((line, index) => ({ id: (number * 10000 + index).toString(16).padStart(32, '0'), line: index + 1,
    email: line.split('----')[0], status: running ? index ? 'queued' : 'running' : index ? 'error' : 'success', events: [] }));
  const batch = { id: `synthetic-batch-${number}`, status: running ? 'running' : 'completed', concurrency: body.concurrency, skip_email_verification: body.skip_email_verification,
    rows, active_jobs: rows.filter((row) => row.status === 'running'),
    counts: { total: rows.length, success: running ? 0 : 1, error: running ? 0 : rows.length - 1,
      queued: running ? rows.length - 1 : 0, running: running ? 1 : 0, awaiting_code: 0, cancelled: 0, skipped: 0 } };
  const record = { id: rows[0].id, email: rows[0].email, line: 1,
    raw: { email: rows[0].email, access_token: `synthetic-at-${number}`, refresh_token: `synthetic-rt-${number}`, id_token: `synthetic-id-${number}` },
    cpa: { type: 'codex', email: rows[0].email, access_token: `synthetic-at-${number}`, refresh_token: `synthetic-rt-${number}` },
    sub2api: { accounts: [{ credentials: { refresh_token: `synthetic-rt-${number}` } }] } };
  return { batch, records: running || missing ? [] : [record] };
}

async function pageWithServer({ onSubmit, onAck, groupOptions = () => ({}), lock } = {}) {
  const groups = new Map(), posts = [], requests = []; let accepted = 0;
  const page = await uiPage('app.js', async (path, options = {}) => {
    requests.push({ path, method: options.method || 'GET' });
    if (path === '/api/batches/current') return response(200, { batch: null });
    if (path === '/api/batches') {
      const body = JSON.parse(options.body); posts.push(body);
      const custom = await onSubmit?.(body, posts.length, accepted);
      if (custom) return custom;
      const result = resultFor(body, ++accepted, groupOptions(accepted)); groups.set(result.batch.id, result);
      return response(202, result.batch);
    }
    if (path === '/api/workspace') return response(200, {});
    const identifier = path.split('/')[3], result = groups.get(identifier);
    assert.ok(result, `unexpected path ${path}`);
    if (path.endsWith('/results')) return response(200, result);
    if (path.endsWith('/ack')) return await onAck?.(identifier) || response(200, {});
    if (path.endsWith('/cancel')) {
      result.batch = { ...result.batch, status: 'cancelled', active_jobs: [] };
      return response(200, result.batch);
    }
    return response(200, result.batch);
  }, { lock });
  async function start(count) {
    page.byId('accounts-text').value = input(count); page.byId('concurrency').value = '10';
    const pending = page.byId('batch-form').dispatch('submit');
    await page.byId('proxy-confirm-dialog').close('start'); await pending; await settle();
  }
  function clearFromOtherTab({ notify = true } = {}) {
    browserResults.createBrowserResultStore({ owner: page.store.owner, storage: page.storage, now: page.time.now }).clear();
    if (notify && page.store.receiveClear()) page.document.dispatchEvent({ type: 'rt:browser-results-cleared' });
  }
  return { ...page, groups, posts, requests, start, clearFromOtherTab };
}

test('2001 accounts run as 1000/1000/1 in order and earlier group downloads survive the next group', async () => {
  const page = await pageWithServer(); await page.start(2001);
  assert.equal(page.posts.length, 1);
  assert.equal(page.posts[0].concurrency, 10);
  assert.equal(page.byId('accounts-text').value, '');
  await page.time.advance(1000);
  assert.equal(page.posts.length, 2);
  const firstAck = page.requests.findIndex((request) => request.path.endsWith('/synthetic-batch-1/ack'));
  const secondPost = page.requests.map((request, index) => ({ request, index })).filter(({ request }) => request.path === '/api/batches')[1].index;
  assert.ok(firstAck < secondPost);
  assert.match(page.byId('queue-archives').textContent, /第 1 组/);
  await page.time.advance(1000); await page.time.advance(1000);
  assert.deepEqual(page.posts.map((body) => body.accounts_text.split('\n').length), [1000, 1000, 1]);
  assert.match(page.byId('queue-progress-label').textContent, /2,001 \/ 2,001/);
  assert.equal(page.byId('queue-status').textContent, '全部分组结束');
  assert.equal(page.store.read().batch.id, 'synthetic-batch-3');
  const rt = page.created.find((element) => element.attributes.get('aria-label') === '下载第 1 组 RT 文本');
  await rt.click();
  assert.equal(await page.downloads.at(-1).blob.text(), 'synthetic-rt-1\n');
  assert.doesNotMatch(JSON.stringify(Array.from({ length: page.storage.length }, (_, index) => page.storage.getItem(page.storage.key(index)))), /synthetic-password|JBSWY3/);
});

test('1001-account progression waits until the preceding group acknowledgement succeeds', async () => {
  let release; const gate = new Promise((resolve) => { release = resolve; });
  const page = await pageWithServer({ onAck: (id) => id.endsWith('-1') ? gate : undefined });
  await page.start(1001);
  const pending = page.time.advance(1000); await settle();
  assert.equal(page.posts.length, 1);
  assert.equal(page.byId('queue-archives').children.length, 0);
  release(response(200, {})); await pending;
  assert.equal(page.posts.length, 2);
  assert.equal(page.byId('queue-archives').children.length, 1);
});

test('a quota-limited next group waits for Retry-After and retains the earlier archive', async () => {
  const page = await pageWithServer({ onSubmit: (_body, attempt) => attempt === 2 ? response(429, { retry_after: 60, error: '每会话账号额度已用完' }) : undefined });
  await page.start(1001); await page.time.advance(1000);
  assert.equal(page.posts.length, 2);
  assert.equal(page.byId('queue-status').textContent, '等待服务额度');
  assert.match(page.byId('queue-message').textContent, /60 秒后自动提交第 2 组/);
  assert.equal(page.byId('cancel-button').hidden, false);
  assert.equal(page.byId('cancel-button').disabled, false);
  await page.time.advance(59_999); assert.equal(page.posts.length, 2);
  await page.time.advance(1); assert.equal(page.posts.length, 3);
  assert.equal(page.posts[2].accounts_text, page.posts[1].accounts_text);
  assert.match(page.byId('queue-archives').textContent, /第 1 组/);
});

test('stopping during quota wait prevents any queued credential resubmission', async () => {
  const page = await pageWithServer({ onSubmit: (_body, attempt) => attempt === 2 ? response(429, { retry_after: 60 }) : undefined });
  await page.start(1001); await page.time.advance(1000);
  await page.byId('cancel-button').click();
  assert.equal(page.byId('queue-status').textContent, '分组已停止');
  await page.time.advance(60_000);
  assert.equal(page.posts.length, 2);
  assert.equal(page.requests.filter((request) => request.path.endsWith('/cancel')).length, 0, 'a waiting plan has no live group to cancel');
  assert.match(page.byId('queue-archives').textContent, /第 1 组/);
});

test('an uncertain group submission never retries automatically or overwrites earlier results', async () => {
  const page = await pageWithServer({ onSubmit: (_body, attempt) => { if (attempt === 2) throw new Error('synthetic connection lost'); } });
  await page.start(1001); await page.time.advance(1000);
  assert.equal(page.byId('queue-status').textContent, '分组已停止');
  assert.match(page.byId('queue-message').textContent, /提交结果未确认/);
  await page.time.advance(60_000);
  assert.equal(page.posts.length, 2);
  assert.equal(page.store.read().batch.id, 'synthetic-batch-1');
  assert.match(page.byId('queue-archives').textContent, /第 1 组/);
});

test('a terminal group whose successful Tokens expired before handoff blocks later groups', async () => {
  const page = await pageWithServer({ groupOptions: () => ({ missing: true }) });
  await page.start(1001); await page.time.advance(1000);
  assert.equal(page.posts.length, 1);
  assert.equal(page.byId('queue-status').textContent, '分组已停止');
  assert.match(page.byId('queue-message').textContent, /未完整接收/);
  assert.equal(page.store.read().batch.id, 'synthetic-batch-1');
});

test('only a page-owned group plan polls in the background every 30 seconds', async () => {
  const page = await pageWithServer({ groupOptions: () => ({ running: true }) });
  await page.start(1001);
  page.document.hidden = true; page.document.dispatchEvent({ type: 'visibilitychange' });
  const before = page.requests.length;
  await page.time.advance(29_999); assert.equal(page.requests.length, before);
  await page.time.advance(1); assert.equal(page.requests.length, before + 1);
  assert.equal(page.requests.at(-1).path, '/api/batches/synthetic-batch-1');
  assert.equal(page.byId('queue-status').textContent, '分组处理中');
});

test('clear releases all archives and stops future groups, while duplicate emails never submit', async () => {
  const page = await pageWithServer({ onSubmit: (_body, attempt) => attempt === 2 ? response(429, { retry_after: 60 }) : undefined });
  await page.start(1001); await page.time.advance(1000);
  await page.byId('clear-browser-results').click();
  assert.equal(page.byId('batch-queue-panel').hidden, true);
  assert.equal(page.store.read(), null);
  await page.time.advance(60_000); assert.equal(page.posts.length, 2);
  page.byId('accounts-text').value = input(1000) + '\nSYNTHETIC-0@example.com----different-password----JBSWY3DPEHPK3PXP';
  const pending = page.byId('batch-form').dispatch('submit');
  await page.byId('proxy-confirm-dialog').close('start'); await pending;
  assert.equal(page.posts.length, 2);
  assert.match(page.byId('form-error-message').textContent, /第 1001 行.*重复/);
});

test('the actual form offers concurrency 1–10 and keeps 2 as default', () => {
  const html = readFileSync(new URL('../rt_extractor/templates/index.html', import.meta.url), 'utf8');
  const select = html.match(/<select id="concurrency"[\s\S]*?<\/select>/)[0];
  for (let number = 1; number <= 10; number++) assert.match(select, new RegExp(`value="${number}"`));
  assert.match(select, /value="2" selected/);
});

test('an hour of quota waiting does not expire completed group archives or stop the next group', async () => {
  const page = await pageWithServer({ onSubmit: (_body, attempt) => attempt === 2 ? response(429, { retry_after: 3600 }) : undefined });
  await page.start(1001); await page.time.advance(1000);
  await page.time.advance(1_800_001);
  assert.equal(page.store.read(), null, 'the existing localStorage cache keeps its original expiry');
  assert.equal(page.byId('queue-status').textContent, '等待服务额度');
  const archivedRt = page.created.find((element) => element.attributes.get('aria-label') === '下载第 1 组 RT 文本');
  await archivedRt.click();
  assert.equal(await page.downloads.at(-1).blob.text(), 'synthetic-rt-1\n');
  await page.time.advance(1_799_999);
  assert.equal(page.posts.length, 3);
  assert.equal(page.posts[2].accounts_text.split('\n').length, 1);
});

test('losing the active group cache stops later groups without discarding earlier archives', async () => {
  const page = await pageWithServer({ groupOptions: (number) => ({ running: number === 2 }) });
  await page.start(2001); await page.time.advance(1000);
  assert.equal(page.posts.length, 2);
  page.storage.removeItem(browserResults.RESULT_CACHE_PREFIX + page.store.owner);
  page.document.dispatchEvent({ type: 'rt:browser-results' });
  assert.equal(page.byId('queue-status').textContent, '分组已停止');
  assert.match(page.byId('queue-message').textContent, /结果缓存已丢失/);
  assert.equal(page.byId('active-panel').hidden, false);
  const archivedRt = page.created.find((element) => element.attributes.get('aria-label') === '下载第 1 组 RT 文本');
  await archivedRt.click(); assert.equal(await page.downloads.at(-1).blob.text(), 'synthetic-rt-1\n');
  await page.time.advance(60_000);
  assert.equal(page.posts.length, 2);
});

test('completed plan archives visibly expire after 30 minutes and cannot be downloaded through old buttons', async () => {
  const page = await pageWithServer(); await page.start(1001);
  await page.time.advance(1000); await page.time.advance(1000);
  assert.equal(page.byId('queue-status').textContent, '全部分组结束');
  const oldButton = page.created.find((element) => element.attributes.get('aria-label') === '下载第 1 组 RT 文本');
  await page.time.advance(1_800_001);
  assert.match(page.byId('queue-archives').textContent, /归档已过期/);
  assert.match(page.byId('queue-message').textContent, /已到期并从本页内存清除/);
  await oldButton.click();
  assert.equal(page.downloads.length, 0);
  assert.match(page.byId('form-error-message').textContent, /归档已过期/);
});

test('another tab explicitly clearing during quota wait stops the plan and erases its archives', async () => {
  const page = await pageWithServer({ onSubmit: (_body, attempt) => attempt === 2 ? response(429, { retry_after: 60 }) : undefined });
  await page.start(1001); await page.time.advance(1000);
  const oldDownload = page.created.find((element) => element.attributes.get('aria-label') === '下载第 1 组 RT 文本');
  page.clearFromOtherTab();
  assert.equal(page.byId('batch-queue-panel').hidden, true);
  assert.match(page.byId('form-error-message').textContent, /另一个标签页已清空/);
  assert.equal(page.byId('cancel-button').hidden, true);
  await oldDownload.click(); assert.equal(page.downloads.length, 0);
  await page.time.advance(60_000);
  assert.equal(page.posts.length, 2);
  assert.equal(page.store.read(), null);
});

test('a delayed cross-tab clear event still blocks the next group inside its submission lock', async () => {
  const page = await pageWithServer({ onSubmit: (_body, attempt) => attempt === 2 ? response(429, { retry_after: 60 }) : undefined });
  await page.start(1001); await page.time.advance(1000);
  page.clearFromOtherTab({ notify: false });
  assert.equal(page.byId('queue-status').textContent, '等待服务额度');
  await page.time.advance(60_000);
  assert.equal(page.posts.length, 2);
  assert.equal(page.byId('batch-queue-panel').hidden, true);
  assert.match(page.byId('form-error-message').textContent, /另一个标签页已清空/);
});

test('cross-tab clearing while a group POST is in flight prevents its late response from resurrecting results', async () => {
  let release; const gate = new Promise((resolve) => { release = resolve; });
  const page = await pageWithServer({ onSubmit: () => gate });
  const pending = page.start(1001); await settle();
  assert.equal(page.posts.length, 1);
  page.clearFromOtherTab();
  release(); await pending;
  assert.equal(page.store.read(), null);
  assert.equal(page.byId('batch-queue-panel').hidden, true);
  assert.equal(page.byId('start-button').disabled, false);
  assert.match(page.byId('form-error-message').textContent, /另一个标签页已清空/);
  await page.time.advance(60_000); assert.equal(page.posts.length, 1);
  assert.equal(page.requests.filter((entry) => entry.path.endsWith('/results')).length, 0);
});

test('a clear completed before a queued submit lock prevents even the first group POST', async () => {
  let release; const gate = new Promise((resolve) => { release = resolve; });
  const page = await pageWithServer({ lock: async (operation) => { await gate; return operation(); } });
  const pending = page.start(1001); await settle();
  page.clearFromOtherTab({ notify: false });
  release(); await pending;
  assert.equal(page.posts.length, 0);
  assert.equal(page.byId('accounts-text').value, '');
  assert.equal(page.byId('batch-queue-panel').hidden, true);
  assert.equal(page.byId('start-button').disabled, false);
});

test('an external batch replacement stops the old plan and keeps already archived downloads', async () => {
  const page = await pageWithServer({ groupOptions: (number) => ({ running: number === 2 }) });
  await page.start(2001); await page.time.advance(1000);
  const replacement = resultFor({ accounts_text: input(1), concurrency: 2 }, 99, { running: true });
  page.groups.set(replacement.batch.id, replacement);
  browserResults.createBrowserResultStore({ owner: page.store.owner, storage: page.storage, now: page.time.now }).save(replacement.batch);
  page.document.dispatchEvent({ type: 'rt:browser-results' });
  assert.equal(page.byId('queue-status').textContent, '分组已停止');
  assert.match(page.byId('queue-message').textContent, /另一个标签页已切换/);
  const archivedRt = page.created.find((element) => element.attributes.get('aria-label') === '下载第 1 组 RT 文本');
  await archivedRt.click(); assert.equal(await page.downloads.at(-1).blob.text(), 'synthetic-rt-1\n');
  await page.time.advance(60_000); assert.equal(page.posts.length, 2);
});

test('an external batch arriving while the next group awaits its lock prevents that group POST', async () => {
  let release, locks = 0; const gate = new Promise((resolve) => { release = resolve; });
  const page = await pageWithServer({ lock: async (operation) => { if (++locks === 2) await gate; return operation(); } });
  await page.start(1001);
  const pending = page.time.advance(1000); await settle();
  const replacement = resultFor({ accounts_text: input(1), concurrency: 2 }, 99, { running: true });
  browserResults.createBrowserResultStore({ owner: page.store.owner, storage: page.storage, now: page.time.now }).save(replacement.batch);
  release(); await pending;
  assert.equal(page.posts.length, 1);
  assert.equal(page.byId('queue-status').textContent, '分组已停止');
  assert.match(page.byId('queue-message').textContent, /另一个标签页已切换/);
  assert.match(page.byId('queue-archives').textContent, /第 1 组/);
});

test('a delayed already-handled clear notification cannot erase a newly started plan', async () => {
  const page = await pageWithServer({ onSubmit: (_body, attempt) => attempt === 2 ? response(429, { retry_after: 60 }) : undefined });
  await page.start(1001); await page.time.advance(1000);
  page.clearFromOtherTab({ notify: false });
  const revision = page.store.clearRevision();
  await page.time.advance(60_000);
  assert.equal(page.byId('batch-queue-panel').hidden, true);
  await page.start(1001);
  assert.equal(page.posts.length, 3);
  assert.equal(page.byId('batch-queue-panel').hidden, false);
  assert.equal(page.store.receiveClear(revision), false, 'the earlier lock-time observation already consumed this notification');
  page.document.dispatchEvent({ type: 'rt:browser-results-cleared' });
  assert.equal(page.byId('batch-queue-panel').hidden, false);
  assert.equal(page.byId('queue-status').textContent, '分组处理中');
  await page.time.advance(1000);
  assert.equal(page.posts.length, 4, 'the newly authorized plan continues normally');
});
