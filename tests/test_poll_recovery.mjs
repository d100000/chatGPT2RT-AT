/** Regression coverage for stale cached progress after a server task disappears. */
import assert from 'node:assert/strict';
import test from 'node:test';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';
import * as requestLimits from '../rt_extractor/static/request-limits.mjs';
import * as browserResults from '../rt_extractor/static/browser-results.mjs';

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
  const time = clock(), ids = new Map(), created = [], listeners = new Map();
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
    getBrowserResults: () => store,
    collectBatchResults: (batch, request) => collector.collect(batch, request),
    isBatchCollected: (id) => collector.settled(id), resetResultCollection: () => collector.reset(), withBrowserResultLock: (operation) => operation(),
  });
  const source = readFileSync(new URL(`../rt_extractor/static/${file}`, import.meta.url), 'utf8').replace(/^import[^\n]*\n/gm, '');
  vm.runInContext(source, context, { filename: file }); await settle();
  return { byId, created, document, time, listeners, store, storage };
}

const id = (number) => number.toString(16).padStart(32, '0');
function fixture({ success = 1, error = 0, running = 1, queued = 0 } = {}) {
  const rows = [], records = [];
  for (const [status, count] of Object.entries({ success, error, running, queued })) {
    for (let index = 0; index < count; index++) {
      const row = { id: id(rows.length + 1), line: rows.length + 1, email: `synthetic-${rows.length + 1}@example.com`, status, events: [] };
      rows.push(row);
      if (status === 'success') records.push({ id: row.id, line: row.line, email: row.email,
        raw: { email: row.email, access_token: 'synthetic-at', refresh_token: 'synthetic-rt', id_token: 'synthetic-id' },
        cpa: { type: 'codex', access_token: 'synthetic-at', refresh_token: 'synthetic-rt' },
        sub2api: { accounts: [{ credentials: { refresh_token: 'synthetic-rt' } }] } });
    }
  }
  return { batch: { id: 'synthetic-batch', status: running || queued ? 'running' : 'completed', concurrency: 3,
    counts: { total: rows.length, success, error, running, queued, awaiting_code: 0, cancelled: 0, skipped: 0 },
    rows, active_jobs: rows.filter((row) => row.status === 'running') }, records };
}

const missing = () => response(404, { error: '批次不存在' });

test('a lost 904-account task releases 630 pending accounts and preserves 91 downloadable successes', async () => {
  const cached = fixture({ success: 91, error: 183, running: 3, queued: 627 });
  const calls = [];
  const page = await uiPage('app.js', async (path) => {
    calls.push(path);
    return path === '/api/batches/current' ? response(200, { batch: cached.batch }) : missing();
  }, { cached });
  assert.equal(page.byId('active-panel').hidden, false);
  assert.equal(page.byId('count-progress').textContent, '274 / 904');
  await page.time.advance(1000);
  const recovered = page.store.read();
  assert.equal(recovered.batch.status, 'cancelled');
  assert.equal(recovered.batch.counts.cancelled, 630);
  assert.equal(recovered.batch.counts.success, 91);
  assert.equal(recovered.batch.counts.error, 183);
  assert.equal(recovered.records.length, 91);
  assert.equal(recovered.collected, true);
  assert.equal(page.byId('active-panel').hidden, true);
  assert.equal(page.byId('count-progress').textContent, '904 / 904');
  assert.equal(page.byId('count-pending').textContent, '0');
  assert.equal(page.byId('credentials-fields').disabled, false);
  assert.equal(page.byId('start-button').disabled, false);
  assert.equal(page.byId('downloads').hidden, false);
  assert.match(page.byId('form-error-message').textContent, /已接收的成功结果仍可下载.*重新导入未完成/);
  assert.equal((await browserResults.resultDownload(recovered, 'rt').blob.text()).trim().split('\n').length, 91);
  const stopped = calls.length;
  await page.time.advance(10_000);
  assert.equal(calls.length, stopped, 'a confirmed missing task must not remain in the polling loop');
});

test('a missing task without a browser cache clears the active form and gives an actionable message', async () => {
  const { batch } = fixture({ success: 0, running: 1 });
  const page = await uiPage('app.js', async (path) => path === '/api/batches/current'
    ? response(200, { batch }) : missing());
  assert.equal(page.store.read(), null, 'an unrecognized page must not take results from the server');
  await page.time.advance(1000);
  assert.equal(page.byId('start-button').disabled, false);
  assert.equal(page.byId('cancel-button').hidden, true);
  assert.equal(page.byId('active-panel').hidden, true);
  assert.equal(page.byId('count-pending').textContent, '0');
  assert.match(page.byId('form-error-message').textContent, /没有可恢复的结果.*重新导入/);
});

test('a handoff 404 preserves a task that still exists when its state is checked again', async () => {
  const cached = fixture({ success: 1, running: 1, queued: 1 });
  const later = fixture({ success: 2, running: 1 });
  let stateCalls = 0, handoffs = 0;
  const page = await uiPage('app.js', async (path) => {
    if (path === '/api/batches/current') return response(200, { batch: cached.batch });
    if (path.endsWith('/results')) { handoffs++; return missing(); }
    stateCalls++; return response(200, later.batch);
  }, { cached });
  await page.time.advance(1000);
  assert.equal(stateCalls, 2, 'a handoff failure must recheck task state before deciding it disappeared');
  assert.equal(handoffs, 1);
  assert.equal(page.byId('active-panel').hidden, false);
  assert.equal(page.byId('credentials-fields').disabled, true);
  assert.equal(page.byId('count-pending').textContent, '1');
  assert.equal(page.store.read().batch.status, 'running');
  assert.equal(page.store.read().records.length, 1);
  assert.match(page.byId('form-error-message').textContent, /结果暂未同步.*重试/);
});

test('a completed snapshot remains completed if its final handoff and state have already disappeared', async () => {
  const cached = fixture({ success: 1, running: 1 });
  const finished = fixture({ success: 1, error: 1, running: 0 });
  let stateCalls = 0;
  const page = await uiPage('app.js', async (path) => {
    if (path === '/api/batches/current') return response(200, { batch: cached.batch });
    if (path.endsWith('/results')) return missing();
    return ++stateCalls === 1 ? response(200, finished.batch) : missing();
  }, { cached });
  await page.time.advance(1000);
  assert.equal(page.store.read().batch.status, 'completed');
  assert.equal(page.store.read().batch.counts.cancelled, 0);
  assert.equal(page.store.read().records.length, 1);
  assert.equal(page.store.read().collected, true);
  assert.equal(page.byId('batch-badge').textContent, '处理完成');
  assert.equal(page.byId('start-button').disabled, false);
  assert.equal(page.byId('form-error-message').textContent, '');
});

test('clearing during an in-flight 404 prevents old cached progress or results from returning', async () => {
  const cached = fixture();
  let resolveState;
  const state = new Promise((resolve) => { resolveState = resolve; });
  const page = await uiPage('app.js', async (path) => {
    if (path === '/api/batches/current') return response(200, { batch: cached.batch });
    if (path === '/api/workspace') return response(200, { cleared: true });
    return state;
  }, { cached });
  const pending = page.time.advance(1000);
  await settle();
  await page.byId('clear-browser-results').click();
  resolveState(missing()); await pending;
  assert.equal(page.store.read(), null);
  assert.equal(page.byId('count-total').textContent, '0');
  assert.equal(page.byId('active-panel').hidden, true);
  assert.equal(page.byId('downloads').hidden, true);
  assert.equal(page.byId('form-error-message').textContent, '');
});

test('clearing while a failed handoff rechecks task state cannot resurrect the task', async () => {
  const cached = fixture({ success: 1, running: 1, queued: 1 });
  const later = fixture({ success: 2, running: 1 });
  let stateCalls = 0, resolveRecheck;
  const recheck = new Promise((resolve) => { resolveRecheck = resolve; });
  const page = await uiPage('app.js', async (path) => {
    if (path === '/api/batches/current') return response(200, { batch: cached.batch });
    if (path === '/api/workspace') return response(200, { cleared: true });
    if (path.endsWith('/results')) return missing();
    return ++stateCalls === 1 ? response(200, later.batch) : recheck;
  }, { cached });
  const pending = page.time.advance(1000); await settle();
  assert.equal(stateCalls, 2);
  await page.byId('clear-browser-results').click();
  resolveRecheck(response(200, later.batch)); await pending;
  assert.equal(page.store.read(), null);
  assert.equal(page.byId('count-total').textContent, '0');
  assert.equal(page.byId('active-panel').hidden, true);
  assert.equal(page.byId('form-error-message').textContent, '');
});

test('an active result cache expiry clears stale download claims and RT without cancelling the task', async () => {
  const cached = fixture({ success: 91, error: 183, running: 3, queued: 627 });
  const calls = [];
  const page = await uiPage('app.js', async (path, options = {}) => {
    calls.push({ path, method: options.method || 'GET' });
    return response(200, path.endsWith('/current') ? { batch: cached.batch } : cached.batch);
  }, { cached });
  await page.created.find((element) => element.textContent === '查看 RT').click();
  assert.equal(page.byId('rt-value').value, 'synthetic-rt');
  page.document.hidden = true;
  page.document.dispatchEvent({ type: 'visibilitychange' });
  await page.time.advance(browserResults.RESULT_CACHE_TTL + 1);
  assert.equal(page.store.read(), null);
  assert.equal(page.byId('downloads').hidden, true);
  assert.equal(page.byId('conversion-status').textContent, '');
  assert.doesNotMatch(page.byId('results-body').textContent, /查看 RT|原始 JSON ↓|CPA JSON ↓/);
  assert.equal(page.byId('rt-dialog').open, false);
  assert.equal(page.byId('rt-value').value, '');
  assert.equal(page.byId('rt-copy-button').disabled, true);
  assert.equal(page.byId('count-progress').textContent, '274 / 904');
  assert.equal(page.byId('active-panel').hidden, false);
  assert.equal(page.byId('credentials-fields').disabled, true);
  assert.match(page.byId('browser-cache-status').textContent, /成功数量是任务处理记录.*没有可下载/);
  page.document.hidden = false;
  page.document.dispatchEvent({ type: 'visibilitychange' });
  await page.time.advance(0);
  assert.ok(calls.some((call) => call.path === '/api/batches/synthetic-batch'));
  assert.ok(calls.every((call) => call.method === 'GET'), 'cache expiry must not send cancellation or acknowledgement');
});

test('a download click noticing a missing cache immediately removes stale download actions', async () => {
  const cached = fixture();
  const page = await uiPage('app.js', async () => response(200, { batch: cached.batch }), { cached });
  page.store.clear(); // No storage event is delivered to the writing page.
  await page.byId('rt-button').click();
  assert.equal(page.byId('downloads').hidden, true);
  assert.equal(page.byId('conversion-status').textContent, '');
  assert.doesNotMatch(page.byId('results-body').textContent, /查看 RT|JSON ↓/);
  assert.equal(page.byId('active-panel').hidden, false);
  assert.equal(page.byId('count-pending').textContent, '1');
  assert.match(page.byId('form-error-message').textContent, /缓存已过期或被清除/);
});

test('a completed result cache still clears its finished view when it expires', async () => {
  const cached = { ...fixture({ success: 1, running: 0 }), collected: true };
  const page = await uiPage('app.js', async () => response(200, { batch: null }), { cached });
  await page.time.advance(browserResults.RESULT_CACHE_TTL + 1);
  assert.equal(page.store.read(), null);
  assert.equal(page.byId('downloads').hidden, true);
  assert.equal(page.byId('conversion-status').textContent, '');
  assert.equal(page.byId('count-total').textContent, '0');
  assert.equal(page.byId('start-button').disabled, false);
});

test('returning from a suspended page checks cache expiry before timers or network responses', async () => {
  const cached = fixture();
  const page = await uiPage('app.js', async () => response(200, { batch: cached.batch }), { cached });
  page.document.hidden = true;
  page.document.dispatchEvent({ type: 'visibilitychange' });
  page.time.elapse(browserResults.RESULT_CACHE_TTL + 1);
  page.document.hidden = false;
  page.document.dispatchEvent({ type: 'visibilitychange' });
  assert.equal(page.store.read(), null);
  assert.equal(page.byId('downloads').hidden, true);
  assert.equal(page.byId('conversion-status').textContent, '');
  assert.equal(page.byId('active-panel').hidden, false);
});

test('cross-tab natural cache removal clears result affordances and keeps polling the known active task', async () => {
  const cached = fixture(); let stateCalls = 0;
  const page = await uiPage('app.js', async (path) => {
    if (path.endsWith('/current')) return response(200, { batch: cached.batch });
    stateCalls++; return response(200, cached.batch);
  }, { cached });
  page.storage.removeItem(browserResults.RESULT_CACHE_PREFIX + page.store.owner);
  page.document.dispatchEvent({ type: 'rt:browser-results' });
  assert.equal(page.byId('downloads').hidden, true);
  assert.equal(page.byId('conversion-status').textContent, '');
  assert.equal(page.byId('active-panel').hidden, false);
  await page.time.advance(0);
  assert.equal(stateCalls, 1);
  assert.equal(page.store.read(), null, 'a removed result marker must not be silently recreated');
  assert.equal(page.byId('count-pending').textContent, '1');
});

test('a cache renewed elsewhere is not erased by the old expiry timer and gets a new expiry check', async () => {
  const cached = fixture({ success: 1, running: 0 });
  cached.collected = true;
  const page = await uiPage('app.js', async () => response(200, { batch: null }), { cached });
  page.time.elapse(browserResults.RESULT_CACHE_TTL - 10);
  page.store.save(cached.batch, cached.records);
  await page.time.advance(11);
  assert.equal(page.store.read().records.length, 1);
  assert.equal(page.byId('downloads').hidden, false);
  await page.time.advance(browserResults.RESULT_CACHE_TTL);
  assert.equal(page.store.read(), null);
  assert.equal(page.byId('downloads').hidden, true);
});
