import assert from 'node:assert/strict';
import test from 'node:test';
import { createBrowserResultStore, createResultCollector, resultDownload, cachedBatchWithoutServer, RESULT_CACHE_PREFIX, RESULT_CACHE_TTL } from '../rt_extractor/static/browser-results.mjs';

export const owner = 'synthetic-owner-1234567890';
export const record = (id = 'a'.repeat(32), line = 1) => ({ id, line, email: `synthetic-${line}@example.com`,
  raw: { email: `synthetic-${line}@example.com`, access_token: 'synthetic-at', refresh_token: `synthetic-rt-${line}`, id_token: 'synthetic-id' },
  cpa: { type: 'codex', email: `synthetic-${line}@example.com`, access_token: 'synthetic-at', refresh_token: `synthetic-rt-${line}`, id_token: 'synthetic-id' },
  sub2api: { accounts: [{ platform: 'openai', type: 'oauth', credentials: { access_token: 'synthetic-at', refresh_token: `synthetic-rt-${line}` } }] } });
export const batch = (records = [record()], status = 'completed') => ({ id: 'b'.repeat(32), status, concurrency: 2,
  counts: { total: records.length, success: records.length, error: 0, cancelled: 0, queued: 0, running: 0, awaiting_code: 0 },
  rows: records.map((item) => ({ id: item.id, line: item.line, email: item.email, status: 'success' })), active_jobs: [] });
export function storageMock() {
  const map = new Map();
  return { get length() { return map.size; }, key: (index) => [...map.keys()][index], getItem: (key) => map.get(key) ?? null,
    setItem: (key, value) => map.set(key, value), removeItem: (key) => map.delete(key) };
}

test('browser session result cache restores only the same owner and removes the previous owner partition', () => {
  const storage = storageMock();
  const first = createBrowserResultStore({ owner, storage }); first.save(batch(), [record()]);
  assert.equal(createBrowserResultStore({ owner, storage }).record(record().id).raw.refresh_token, 'synthetic-rt-1');
  const another = createBrowserResultStore({ owner: 'another-owner-1234567890', storage });
  assert.equal(another.read(), null); assert.equal(storage.getItem(RESULT_CACHE_PREFIX + owner), null);
  assert.equal(createBrowserResultStore({ owner, storage }).read(), null);
});

test('TTL expiry, corrupt JSON and invalid record ownership fail closed and erase stored data', () => {
  const storage = storageMock(); let now = 1000;
  const store = createBrowserResultStore({ owner, storage, now: () => now }); store.save(batch(), [record()]);
  now += RESULT_CACHE_TTL; assert.equal(store.read(), null); assert.equal(storage.length, 0);
  for (const content of ['{bad json', JSON.stringify({ version: 3, owner: 'wrong' }), JSON.stringify({ version: 3, owner, expiresAt: now + 100, batch: batch([]), records: [record()] })]) {
    storage.setItem(RESULT_CACHE_PREFIX + owner, content);
    assert.equal(createBrowserResultStore({ owner, storage, now: () => now }).read(), null);
    assert.equal(storage.length, 0);
  }
});

test('input credentials, OTP challenges and server paths never enter browser storage', () => {
  const storage = storageMock(), store = createBrowserResultStore({ owner, storage });
  const value = batch(); value.password = 'never-cache-password'; value.proxy = 'never-cache-proxy';
  value.current = { totp_secret: 'never-cache-secret' }; value.active_jobs = [{ challenge: { code: 'never-cache-code' } }];
  value.rows[0].password = 'never-cache-password'; value.result = { directory: '/private/path' };
  const output = record(); output.raw.password = 'never-cache-password'; output.cpa.password = 'never-cache-password';
  store.save(value, [output]);
  const serialized = storage.getItem(RESULT_CACHE_PREFIX + owner);
  assert.doesNotMatch(serialized, /never-cache|private\/path/);
  assert.match(serialized, /synthetic-rt-1/); assert.deepEqual(store.read().batch.active_jobs, []);
});

test('blocked or full session storage keeps downloads in page memory and reports the limitation', async () => {
  const storage = { getItem: () => null, setItem() { throw new Error('QuotaExceeded'); }, removeItem() {} };
  const store = createBrowserResultStore({ owner, storage }); store.save(batch(), [record()]);
  assert.equal(store.status().persistence, 'memory'); assert.match(store.status().notice, /刷新或离开页面后可能丢失/);
  assert.equal(await resultDownload(store.read(), 'rt').blob.text(), 'synthetic-rt-1\n');
  assert.equal(createBrowserResultStore({ owner, storage }).read(), null);
});

test('a capacity error preserves the current result and does not confirm unreceived results', async () => {
  const store = createBrowserResultStore({ owner, storage: storageMock(), maxBytes: 3000 });
  store.save(batch(), [record()]);
  const oversized = record('c'.repeat(32), 2); oversized.raw.access_token = 'x'.repeat(4000);
  assert.throws(() => store.save(batch([record(), oversized]), [oversized]), /缓存容量/);
  assert.equal(store.read().records.length, 1);
});

test('result handoff stores before ACK, batches successful records, and restores completed results after ACK', async () => {
  const storage = storageMock(), store = createBrowserResultStore({ owner, storage });
  const collector = createResultCollector({ store }); const events = [];
  await collector.collect(batch(), async (path, options) => {
    events.push(path);
    if (path.endsWith('/results')) return { batch: batch(), records: [record()] };
    assert.equal(store.record(record().id).raw.refresh_token, 'synthetic-rt-1');
    assert.equal(createBrowserResultStore({ owner, storage }).read().records.length, 1);
    assert.deepEqual(JSON.parse(options.body), { ids: [record().id], complete: true }); return {};
  });
  assert.equal(events.length, 2); assert.equal(store.read().collected, true);
  await collector.collect(batch(), () => assert.fail('Collected results must never be requested again'));
  assert.equal(createBrowserResultStore({ owner, storage }).read().collected, true);
});

test('handoff failures never ACK and running results are fetched at most once per five seconds', async () => {
  const store = createBrowserResultStore({ owner, storage: storageMock() }); let now = 0, calls = 0;
  const collector = createResultCollector({ store, now: () => now });
  const running = batch([record()], 'running');
  const request = async (path) => { calls++; if (path.endsWith('/results')) throw new Error('Try later'); assert.fail('Must not ACK missing payload'); };
  await assert.rejects(collector.collect(running, request), /Try later/);
  for (let index = 0; index < 20; index++) await collector.collect(running, request);
  assert.equal(calls, 1); now = 5000; await assert.rejects(collector.collect(running, request)); assert.equal(calls, 2);
});

test('clearing invalidates an in-flight handoff and cannot refill results or send ACK', async () => {
  const store = createBrowserResultStore({ owner, storage: storageMock() }), collector = createResultCollector({ store });
  let resolve; const result = new Promise((done) => { resolve = done; });
  const pending = collector.collect(batch(), async (path) => {
    assert.ok(path.endsWith('/results')); return result;
  });
  collector.reset(); store.clear(); resolve({ batch: batch(), records: [record()] });
  await pending; assert.equal(store.read(), null);
});

test('all Token, CPA, Sub2API and batch exports are browser-generated', async () => {
  const state = { batch: batch(), records: [record()] };
  for (const kind of ['raw', 'cpa', 'sub2api']) {
    const download = resultDownload(state, kind, record().id);
    const value = JSON.parse(await download.blob.text()); assert.match(download.filename, /\.json$/);
    assert.ok(kind === 'sub2api' ? value.accounts.length === 1 : value.refresh_token === 'synthetic-rt-1');
  }
  assert.equal(JSON.parse(await resultDownload(state, 'sub2api').blob.text()).accounts.length, 1);
  for (const kind of ['raw', 'cpa']) {
    const { blob, filename } = resultDownload(state, kind); assert.match(filename, /\.zip$/);
    const bytes = new Uint8Array(await blob.arrayBuffer()); assert.deepEqual([...bytes.slice(0, 2)], [80, 75]);
    assert.match(new TextDecoder().decode(bytes), /synthetic-rt-1/);
  }
});

test('server task expiry keeps received results and releases a formerly active form', () => {
  const store = createBrowserResultStore({ owner, storage: storageMock() });
  const value = batch([record()], 'running'); value.counts.running = 1; value.counts.total = 2;
  value.rows.push({ id: 'd'.repeat(32), email: 'pending@example.com', status: 'running', line: 2 });
  store.save(value, [record()]); const restored = cachedBatchWithoutServer(store);
  assert.equal(restored.status, 'cancelled'); assert.equal(restored.counts.cancelled, 1);
  assert.equal(store.read().records.length, 1); assert.equal(store.read().collected, true);
});

test('a mismatched server record is never ACKed or added to browser results', async () => {
  const store = createBrowserResultStore({ owner, storage: storageMock() }), collector = createResultCollector({ store });
  let calls = 0;
  await assert.rejects(collector.collect(batch(), async (path) => {
    calls++; assert.ok(path.endsWith('/results'));
    return { batch: batch(), records: [record('e'.repeat(32))] };
  }), /当前批次不匹配/);
  assert.equal(calls, 1); assert.equal(store.read().records.length, 0);
});

test('ACK failure leaves browser results downloadable and a later idempotent retry finishes handoff', async () => {
  const store = createBrowserResultStore({ owner, storage: storageMock() }); let now = 0, attempts = 0;
  const collector = createResultCollector({ store, now: () => now });
  const request = async (path) => {
    if (path.endsWith('/results')) return { batch: batch(), records: attempts ? [] : [record()] };
    if (++attempts === 1) throw new Error('ACK connection lost'); return {};
  };
  await assert.rejects(collector.collect(batch(), request), /ACK connection lost/);
  assert.equal(await resultDownload(store.read(), 'rt').blob.text(), 'synthetic-rt-1\n');
  now = 5000; await collector.collect(batch(), request);
  assert.equal(attempts, 2); assert.equal(store.read().collected, true);
});

function serialLock() {
  let tail = Promise.resolve();
  return (operation) => { const pending = tail.then(operation); tail = pending.catch(() => {}); return pending; };
}

test('shared browser stores synchronize reads and one owner lock prevents multi-tab ACK stealing', async () => {
  const storage = storageMock(), first = createBrowserResultStore({ owner, storage, storageMode: 'local' });
  const second = createBrowserResultStore({ owner, storage, storageMode: 'local' });
  first.save(batch()); const lock = serialLock(); let calls = 0;
  const collectors = [first, second].map((store) => createResultCollector({ store, lock, requireKnown: true }));
  const request = async (path) => { calls++; return path.endsWith('/results') ? { batch: batch(), records: [record()] } : {}; };
  await Promise.all(collectors.map((collector) => collector.collect(batch(), request)));
  assert.equal(calls, 2); assert.equal(first.record(record().id).raw.refresh_token, 'synthetic-rt-1');
  assert.equal(second.record(record().id).raw.refresh_token, 'synthetic-rt-1'); assert.equal(second.read().collected, true);
});

test('owner lock orders clear after in-flight handoff and stale collectors cannot refill a cleared workspace', async () => {
  const storage = storageMock(), first = createBrowserResultStore({ owner, storage }), second = createBrowserResultStore({ owner, storage });
  first.save(batch()); const lock = serialLock(); const collector = createResultCollector({ store: first, lock, requireKnown: true });
  let resolve, getStarted; const started = new Promise((done) => { getStarted = done; });
  const response = new Promise((done) => { resolve = done; }); const events = [];
  const collecting = collector.collect(batch(), async (path) => {
    if (path.endsWith('/results')) { events.push('GET'); getStarted(); return response; }
    events.push('ACK'); return {};
  });
  await started;
  const clearing = lock(async () => { events.push('clear'); second.clear(); });
  resolve({ batch: batch(), records: [record()] }); await Promise.all([collecting, clearing]);
  assert.deepEqual(events, ['GET', 'ACK', 'clear']); assert.equal(first.read(), null); assert.equal(second.read(), null);
  await collector.collect(batch(), () => assert.fail('A cleared marker must not refill results'));
  assert.equal(first.read(), null);
});

test('a page without an originating browser marker never takes another page result in fallback mode', async () => {
  const store = createBrowserResultStore({ owner }); const collector = createResultCollector({ store, requireKnown: true });
  await collector.collect(batch(), () => assert.fail('Unrelated page must not take or ACK the payload'));
  assert.equal(store.read(), null); assert.equal(store.status().persistence, 'memory');
});

test('a late empty current response cannot cancel a different tab newly submitted batch', () => {
  const storage = storageMock(), first = createBrowserResultStore({ owner, storage }), second = createBrowserResultStore({ owner, storage });
  const previousId = first.read()?.batch.id ?? null;
  const started = batch([], 'running'); started.counts.total = 1; started.counts.running = 1;
  started.rows = [{ id: record().id, line: 1, email: record().email, status: 'running' }]; second.save(started);
  const restored = cachedBatchWithoutServer(first, previousId);
  assert.equal(restored.status, 'running'); assert.equal(first.read().collected, false); assert.equal(second.read().batch.status, 'running');
});

test('a second tab stale running snapshot cannot regress delivered success rows or erase acknowledged Tokens', async () => {
  const storage = storageMock(), first = createBrowserResultStore({ owner, storage }), second = createBrowserResultStore({ owner, storage });
  const old = batch([], 'running'); old.counts.total = 2; old.counts.running = 1; old.counts.queued = 1;
  old.rows = [{ id: record().id, line: 1, email: record().email, status: 'running' }, { id: 'f'.repeat(32), line: 2, email: 'pending@example.com', status: 'queued' }];
  first.save(old);
  const newer = structuredClone(old); newer.rows[0].status = 'success'; newer.counts.running = 0; newer.counts.success = 1;
  first.save(newer, [record()]); // The server may already have deleted this result after its ACK.
  const collector = createResultCollector({ store: second, requireKnown: true });
  await collector.collect(old, () => assert.fail('Already delivered result must stay local'));
  for (const store of [first, second, createBrowserResultStore({ owner, storage })]) {
    assert.equal(store.read().records.length, 1); assert.equal(store.read().batch.rows[0].status, 'success');
    assert.equal(store.read().batch.counts.success, 1); assert.equal(store.record(record().id).raw.refresh_token, 'synthetic-rt-1');
  }
});
