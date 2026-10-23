import assert from 'node:assert/strict';
import test from 'node:test';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';
import * as requestLimits from '../rt_extractor/static/request-limits.mjs';
import * as browserResults from '../rt_extractor/static/browser-results.mjs';

const { retryAfterSeconds, rateLimitError, createRequestCooldowns, requestLimitAction } = requestLimits;
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

test('Retry-After honors the longest positive server delay, including an HTTP date', () => {
  assert.equal(retryAfterSeconds(response(429, {}, '15'), { retry_after: 30 }, NOW), 30);
  assert.equal(retryAfterSeconds(response(429, {}, '30'), { retry_after: 2 }, NOW), 30);
  assert.equal(retryAfterSeconds(response(429, {}, new Date(NOW + 120_000).toUTCString()), {}, NOW), 120);
  assert.equal(retryAfterSeconds(response(429, {}, '.5'), { retry_after: 1.5 }, NOW), 2);
});

test('missing or corrupt delays use a finite cooldown instead of hammering the service', () => {
  for (const value of [undefined, '', 'bad', '-1', 'Infinity', '1e1000']) {
    assert.equal(retryAfterSeconds(response(429, {}, value), { retry_after: value }, NOW), 60);
  }
  const error = rateLimitError(response(429, {}, '8'), { error: '暂缓提交', retry_after: 6 }, NOW);
  assert.equal(error.status, 429);
  assert.equal(error.retryAfter, 8);
  assert.match(error.message, /暂缓提交.*8 秒/);
});

test('cooldown reads do not extend a lock and status polling never blocks cancellation', () => {
  const time = clock();
  const cooldowns = createRequestCooldowns({ now: time.now });
  cooldowns.hold('poll', 60);
  assert.equal(cooldowns.remaining('poll'), 60);
  assert.equal(cooldowns.remaining('cancel'), 0);
  assert.equal(cooldowns.remaining('submit'), 0);
  cooldowns.hold('poll', 2);
  assert.equal(cooldowns.delay('poll'), 60_000);
  return time.advance(60_000).then(() => {
    assert.equal(cooldowns.remaining('poll'), 0);
    assert.equal(cooldowns.active(), false);
  });
});

test('related endpoints share action cooldowns even when resource IDs vary', () => {
  assert.equal(requestLimitAction('/api/jobs', 'POST'), 'submit');
  assert.equal(requestLimitAction('/api/batches', 'POST'), 'submit');
  for (const path of ['/api/jobs/one/rt', '/api/jobs/two/cpa-download', '/api/batches/three/conversion-source', '/api/batches/one/results']) {
    assert.equal(requestLimitAction(path), 'download');
  }
  assert.equal(requestLimitAction('/api/jobs/random/code', 'POST'), 'code');
  assert.equal(requestLimitAction('/api/jobs/random/cancel', 'POST'), 'cancel');
  assert.equal(requestLimitAction('/api/batches/current'), 'poll');
  assert.equal(requestLimitAction('/api/workspace', 'DELETE'), 'cancel');
  assert.equal(requestLimitAction('/api/batches/one/ack', 'POST'), 'handoff-ack');
});

class Element {
  constructor(tagName = 'div') {
    this.tagName = tagName.toUpperCase(); this.value = ''; this.disabled = false; this.hidden = false;
    this.open = false; this.dataset = {}; this.children = []; this.listeners = new Map();
    this.attributes = new Map(); this.ownText = '';
    this.classList = { add() {}, remove() {}, toggle() {} };
  }
  set textContent(value) { this.ownText = String(value ?? ''); this.children = []; }
  get textContent() { return this.ownText + this.children.map((child) => child.textContent ?? child).join(''); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; this.ownText = ''; }
  setAttribute(name, value) { this.attributes.set(name, value); }
  removeAttribute(name) { this.attributes.delete(name); }
  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }
  async dispatch(type) {
    if (type === 'click' && this.disabled) return;
    for (const listener of this.listeners.get(type) || []) await listener({ type, target: this, preventDefault() {} });
  }
  click() { return this.dispatch('click'); }
  showModal() { this.open = true; }
  close() { this.open = false; }
  remove() {}
  focus() {}
}

async function uiPage(file, fetchImpl, options = {}) {
  const time = clock(), ids = new Map(), created = [], listeners = new Map();
  const byId = (id) => { if (!ids.has(id)) ids.set(id, new Element()); return ids.get(id); };
  const body = new Element('body'); body.dataset.page = file === 'app.js' ? 'extractor' : 'converter';
  const document = {
    body, hidden: false, getElementById: byId,
    querySelector: () => ({ content: 'synthetic-csrf' }),
    createElement(tag) { const element = new Element(tag); created.push(element); return element; },
    createDocumentFragment: () => new Element(),
    addEventListener(type, listener) { if (!listeners.has(type)) listeners.set(type, []); listeners.get(type).push(listener); },
    dispatchEvent(event) { for (const listener of listeners.get(event.type) || []) listener(event); },
  };
  byId('concurrency').value = '2';
  const map = new Map();
  const storage = options.storage || { get length() { return map.size; }, key: (index) => [...map.keys()][index], getItem: (key) => map.get(key) ?? null, setItem: (key, value) => map.set(key, value), removeItem: (key) => map.delete(key) };
  const store = browserResults.createBrowserResultStore({ owner: 'synthetic-owner-123456', storage, now: time.now });
  if (options.cached) { store.save(options.cached.batch, options.cached.records); store.markCollected(options.cached.batch.id); }
  const collector = browserResults.createResultCollector({ store, now: time.now });
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

const activeBatch = (changes = {}) => ({
  id: 'synthetic-batch', status: 'running', concurrency: 2,
  counts: { total: 1, queued: 0, running: 1, awaiting_code: 0, success: 0, error: 0, cancelled: 0 },
  rows: [], active_jobs: [], ...changes,
});

test('sidebar 429 waits for Retry-After across visibility changes and never reports offline', async () => {
  let calls = 0;
  const page = await uiPage('workspace.js', async () => ++calls === 1
    ? response(429, { retry_after: 20 }, '15') : response(200, { batch: null }));
  assert.equal(calls, 1);
  assert.equal(page.byId('workspace-task-badge').textContent, '稍后更新');
  for (const listener of page.listeners.get('visibilitychange')) listener();
  await settle(); assert.equal(calls, 1);
  await page.time.advance(19_999); assert.equal(calls, 1);
  await page.time.advance(1); assert.equal(calls, 2);
  assert.equal(page.byId('workspace-task-badge').hidden, true);
});

test('batch submission 429 preserves credentials in the form and never auto-resubmits them', async () => {
  const requests = [];
  const page = await uiPage('app.js', async (path, options) => {
    requests.push({ path, method: options.method || 'GET' });
    return path === '/api/batches/current' ? response(200, { batch: null }) : response(429, { retry_after: 30 }, '30');
  });
  const input = 'synthetic@example.com----synthetic-password----SYNTHETIC';
  page.byId('accounts-text').value = input;
  await page.byId('batch-form').dispatch('submit');
  assert.equal(page.byId('accounts-text').value, input);
  assert.equal(page.byId('credentials-fields').disabled, false);
  assert.equal(page.byId('start-button').disabled, true);
  assert.match(page.byId('start-label').textContent, /30 秒/);
  await page.byId('batch-form').dispatch('submit'); assert.equal(requests.filter((r) => r.method === 'POST').length, 1);
  await page.time.advance(30_000);
  assert.equal(page.byId('start-button').disabled, false);
  assert.equal(requests.filter((r) => r.method === 'POST').length, 1);
});

test('status-query cooling does not disable or defer cancellation', async () => {
  const requests = [], batch = activeBatch();
  const page = await uiPage('app.js', async (path, options) => {
    requests.push(path);
    if (path === '/api/batches/current') return response(200, { batch });
    if (path.endsWith('/cancel')) return response(200, { ...batch, status: 'cancelled' });
    return response(429, { retry_after: 45 }, '45');
  });
  await page.time.advance(1000);
  assert.equal(page.byId('cancel-button').disabled, false);
  assert.match(page.byId('form-error-message').textContent, /45 秒/);
  await page.byId('cancel-button').click();
  assert.equal(requests.filter((path) => path.endsWith('/cancel')).length, 1);
  assert.equal(page.byId('cancel-button').hidden, true);
});

test('verification throttling preserves the code, keeps it editable, and requires an explicit retry', async () => {
  let submissions = 0;
  const job = { id: 'synthetic-job', email: 'synthetic@example.com', status: 'awaiting_code', challenge: { id: 'synthetic-challenge', kind: 'email' } };
  const batch = activeBatch({ active_jobs: [job] });
  const page = await uiPage('app.js', async (path, options) => {
    if (path.endsWith('/code')) { submissions += 1; return response(429, { retry_after: 15 }, '15'); }
    return response(200, path.endsWith('/current') ? { batch } : batch);
  });
  const input = page.created.find((element) => element.id === 'challenge-code-synthetic-job');
  const form = page.created.find((element) => element.className === 'challenge');
  input.value = '123456'; await form.dispatch('submit');
  assert.equal(input.value, '123456'); assert.equal(input.disabled, false);
  const button = form.children[3].children[1]; assert.equal(button.disabled, true);
  await form.dispatch('submit'); assert.equal(submissions, 1);
  await page.time.advance(15_000); assert.equal(button.disabled, false); assert.equal(submissions, 1);
});

const outputRecord = { id: 'a'.repeat(32), line: 1, email: 'synthetic@example.com',
  raw: { email: 'synthetic@example.com', access_token: 'synthetic-at', refresh_token: 'synthetic-rt', id_token: 'synthetic-id' },
  cpa: { type: 'codex', access_token: 'synthetic-at', refresh_token: 'synthetic-rt' },
  sub2api: { accounts: [{ credentials: { refresh_token: 'synthetic-rt' } }] } };
const completedBatch = () => activeBatch({ status: 'completed',
  counts: { total: 1, queued: 0, running: 0, awaiting_code: 0, success: 1, error: 0, cancelled: 0 },
  rows: [{ id: outputRecord.id, email: outputRecord.email, line: 1, status: 'success' }] });

test('throttled result handoff respects Retry-After then downloads stay entirely local', async () => {
  let results = 0, acks = 0, requests = 0; const batch = completedBatch();
  const page = await uiPage('app.js', async (path) => {
    requests++;
    if (path === '/api/batches/current') return response(200, { batch });
    if (path.endsWith('/results')) return ++results === 1 ? response(429, { retry_after: 10 }, '10') : response(200, { batch, records: [outputRecord] });
    if (path.endsWith('/ack')) { acks++; return response(200, {}); }
    throw new Error('Unexpected request');
  });
  assert.equal(results, 1); assert.equal(acks, 0); assert.match(page.byId('form-error-message').textContent, /10 秒/);
  await page.time.advance(9999); assert.equal(results, 1);
  await page.time.advance(1); assert.equal(results, 2); assert.equal(acks, 1);
  const before = requests;
  for (const id of ['cpa-button', 'sub2api-button', 'zip-button', 'rt-button']) await page.byId(id).click();
  assert.equal(requests, before); assert.equal(page.store.read().records.length, 1);
});

test('refresh restores acknowledged browser results when the server current task is already cleared', async () => {
  const page = await uiPage('app.js', async (path) => {
    assert.equal(path, '/api/batches/current'); return response(200, { batch: null });
  }, { cached: { batch: completedBatch(), records: [outputRecord] } });
  assert.equal(page.byId('count-success').textContent, '1');
  assert.equal(page.byId('downloads').hidden, false);
  assert.match(page.byId('results-body').textContent, /synthetic@example.com/);
  assert.equal(page.byId('start-button').disabled, false);
});

test('explicit browser clear hides RT and removes cache before server workspace deletion', async () => {
  const page = await uiPage('app.js', async (path, options) => {
    if (options.method === 'DELETE') { assert.equal(path, '/api/workspace'); return response(200, {}); }
    return response(200, { batch: null });
  }, { cached: { batch: completedBatch(), records: [outputRecord] } });
  const view = page.created.find((element) => element.textContent === '查看 RT'); await view.click();
  assert.equal(page.byId('rt-value').value, 'synthetic-rt');
  await page.byId('clear-browser-results').click();
  assert.equal(page.store.read(), null); assert.equal(page.byId('rt-value').value, ''); assert.equal(page.byId('rt-dialog').open, false);
  assert.equal(page.byId('downloads').hidden, true); assert.equal(page.byId('count-total').textContent, '0');
});

test('other tool pages deliver completed results to this browser and ACK them without legacy downloads', async () => {
  const paths = [], batch = completedBatch();
  const page = await uiPage('workspace.js', async (path) => {
    paths.push(path);
    if (path.endsWith('/current')) return response(200, { batch });
    if (path.endsWith('/results')) return response(200, { batch, records: [outputRecord] });
    if (path.endsWith('/ack')) return response(200, {});
    throw new Error('Unexpected request');
  });
  assert.deepEqual(paths, ['/api/batches/current', '/api/batches/synthetic-batch/results', '/api/batches/synthetic-batch/ack']);
  assert.equal(page.store.record(outputRecord.id).raw.refresh_token, 'synthetic-rt');
  assert.equal(page.store.read().collected, true); assert.equal(page.byId('workspace-task-badge').textContent, '1 个已缓存');
});

test('failed server cleanup still clears local results and offers an explicit cleanup retry', async () => {
  let deletes = 0;
  const page = await uiPage('app.js', async (path, options) => {
    if (options.method === 'DELETE') return ++deletes === 1 ? response(500, { error: 'temporary failure' }) : response(200, {});
    return response(200, { batch: null });
  }, { cached: { batch: completedBatch(), records: [outputRecord] } });
  await page.byId('clear-browser-results').click();
  assert.equal(page.store.read(), null); assert.equal(page.byId('downloads').hidden, true);
  assert.match(page.byId('clear-browser-results').textContent, /重试清理/); assert.equal(page.byId('clear-browser-results').disabled, false);
  await page.byId('clear-browser-results').click(); assert.equal(deletes, 2);
  assert.equal(page.byId('clear-browser-results').disabled, true); assert.equal(page.byId('form-error').hidden, true);
});
