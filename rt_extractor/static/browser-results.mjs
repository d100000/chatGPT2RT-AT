import { buildZipArchive } from './archive.mjs';

export const RESULT_CACHE_PREFIX = 'bob-ai-browser-results-v3:';
export const RESULT_CACHE_TTL = 30 * 60 * 1000;
const MAX_BYTES = 20 * 1024 * 1024;
const terminal = (batch) => batch && ['completed', 'cancelled'].includes(batch.status);
const clone = (value) => JSON.parse(JSON.stringify(value));
const tokenFields = new Set(['email', 'access_token', 'refresh_token', 'id_token', 'expires_in', 'obtained_at', 'expires_at', 'chatgpt_account_id', 'chatgpt_user_id', 'plan_type', 'sub', 'token_type']);
const forbidden = new Set(['password', 'totp_secret', 'totp_code', 'proxy', 'code', 'challenge', 'path', 'directory', 'cpa_path', 'sub2api_path']);

function clean(value) {
  if (Array.isArray(value)) return value.map(clean);
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value)
    .filter(([key]) => !forbidden.has(key) && !key.endsWith('_url')).map(([key, item]) => [key, clean(item)]));
  return value;
}

function safeBatch(batch) {
  if (!batch || typeof batch.id !== 'string' || !Array.isArray(batch.rows) || batch.rows.length > 1000
      || !batch.counts || !['total', 'success', 'error', 'cancelled', 'queued', 'running'].every((key) => Number.isInteger(batch.counts[key]) && batch.counts[key] >= 0)
      || !batch.rows.every((row) => row && typeof row.id === 'string' && typeof row.email === 'string' && typeof row.status === 'string')) return null;
  const fields = ['id', 'status', 'concurrency', 'counts', 'cooldown_seconds', 'error'];
  const result = Object.fromEntries(fields.filter((field) => batch[field] !== undefined).map((field) => [field, clean(batch[field])]));
  const rowFields = ['id', 'line', 'email', 'status', 'stage', 'message', 'error'];
  result.rows = batch.rows.map((row) => Object.fromEntries(rowFields.filter((field) => row[field] !== undefined).map((field) => [field, row[field]])));
  // Verification challenges and progress logs stay transient.
  result.active_jobs = [];
  return result;
}

function safeRecord(record) {
  if (!record || typeof record.id !== 'string' || !record.raw || typeof record.raw !== 'object'
      || !record.cpa || typeof record.cpa !== 'object' || !record.sub2api || typeof record.sub2api !== 'object') return null;
  const raw = Object.fromEntries(Object.entries(record.raw).filter(([key]) => tokenFields.has(key)));
  if (!['access_token', 'refresh_token', 'id_token'].every((key) => typeof raw[key] === 'string' && raw[key].trim())) return null;
  return { id: record.id, line: record.line, email: String(record.email || raw.email || ''),
    raw, cpa: clean(record.cpa), sub2api: clean(record.sub2api) };
}

export function createBrowserResultStore({ owner, storage, now = Date.now, maxBytes = MAX_BYTES, storageMode = 'session' } = {}) {
  const validOwner = typeof owner === 'string' && /^[A-Za-z0-9_-]{16,128}$/.test(owner);
  const key = validOwner ? RESULT_CACHE_PREFIX + owner : null;
  let state = null, persistence = key && storage ? storageMode : 'memory', notice = key && storage ? '' : '浏览器不支持安全共享缓存，结果仅保留在当前页面内存；请下载后再离开或刷新。';
  let lastSerialized = null;
  const remove = () => { try { if (key) storage?.removeItem(key); } catch {} };
  const publish = () => {
    if (!state) return;
    const serialized = JSON.stringify(state);
    if (new TextEncoder().encode(serialized).length > maxBytes) {
      throw new Error('本浏览器结果超过缓存容量，请先下载并清除当前结果。');
    }
    try {
      if (!key || !storage) throw new Error('No session storage');
      storage.setItem(key, serialized);
      if (storage.getItem(key) !== serialized) throw new Error('Storage verification failed');
      lastSerialized = serialized; persistence = storageMode; notice = '';
    } catch {
      remove(); lastSerialized = null; persistence = 'memory';
      notice = '浏览器共享缓存不可用或空间不足，结果仅保留在当前页面内存；请立即下载，刷新或离开页面后可能丢失。';
    }
  };
  function sync() {
    if (!key || !storage || persistence === 'memory') return;
    try {
      const raw = storage.getItem(key);
      if (raw === lastSerialized) return;
      lastSerialized = raw; state = null;
      if (raw && new TextEncoder().encode(raw).length <= maxBytes) {
        const parsed = JSON.parse(raw);
        if (parsed.version === 3 && parsed.owner === owner && Number.isFinite(parsed.expiresAt)
            && parsed.expiresAt > now() && parsed.expiresAt <= now() + RESULT_CACHE_TTL
            && Array.isArray(parsed.records) && parsed.records.length <= 1000) {
          const batch = safeBatch(parsed.batch);
          const records = parsed.records.map(safeRecord).filter(Boolean);
          const known = new Set(batch?.rows.filter((row) => row.status === 'success').map((row) => row.id));
          if (batch && records.length === parsed.records.length && records.every((record) => known.has(record.id)) && new Set(records.map((record) => record.id)).size === records.length) state = { ...parsed, batch, records };
        }
      }
      if (!state) { remove(); lastSerialized = null; }
    } catch { remove(); lastSerialized = null; persistence = 'memory'; }
  }
  if (key && storage) {
    try {
      for (let index = storage.length - 1; index >= 0; index -= 1) {
        const oldKey = storage.key(index);
        if (oldKey?.startsWith(RESULT_CACHE_PREFIX) && oldKey !== key) storage.removeItem(oldKey);
      }
    } catch { persistence = 'memory'; }
    sync();
  }
  const live = () => {
    sync();
    if (state && state.expiresAt <= now()) { state = null; remove(); lastSerialized = null; }
    return state;
  };
  const read = () => { const value = live(); return value ? clone(value) : null; };
  return {
    owner, read,
    status() { return { persistence, notice }; },
    save(batch, incoming = []) {
      let normalized = safeBatch(batch);
      if (!normalized) throw new Error('无法缓存当前批次，请刷新后重试。');
      const previous = live();
      const same = previous?.batch.id === normalized.id;
      if (same && terminal(previous.batch) && !terminal(normalized)) normalized = previous.batch;
      if (same) {
        const previousRows = new Map(previous.batch.rows.map((row) => [row.id, row]));
        let preserved = false;
        normalized.rows = normalized.rows.map((row) => {
          const old = previousRows.get(row.id);
          if (old && ['success', 'error', 'cancelled'].includes(old.status) && old.status !== row.status) {
            preserved = true; return old;
          }
          return row;
        });
        if (preserved) {
          normalized.counts = { total: normalized.rows.length, queued: 0, running: 0, awaiting_code: 0, success: 0, error: 0, cancelled: 0 };
          for (const row of normalized.rows) if (Object.hasOwn(normalized.counts, row.status)) normalized.counts[row.status]++;
        }
      }
      const records = new Map((same ? previous.records : []).map((record) => [record.id, record]));
      const known = new Set(normalized.rows.filter((row) => row.status === 'success').map((row) => row.id));
      for (const item of incoming) {
        const record = safeRecord(item);
        if (!record || !known.has(record.id)) throw new Error('收到的结果与当前批次不匹配，未确认接收。');
        records.set(record.id, record);
      }
      const next = { version: 3, owner, expiresAt: now() + RESULT_CACHE_TTL,
        batch: normalized, records: [...records.values()].sort((a, b) => (a.line || 0) - (b.line || 0)),
        collected: same ? !!previous.collected : false };
      if (new TextEncoder().encode(JSON.stringify(next)).length > maxBytes) throw new Error('本浏览器结果超过缓存容量，请先下载并清除当前结果。');
      state = next; publish(); return read();
    },
    markCollected(id) { if (live()?.batch.id === id) { state.collected = true; publish(); } },
    record(id) { const record = live()?.records.find((item) => item.id === id); return record ? clone(record) : null; },
    clear() { state = null; lastSerialized = null; notice = ''; remove(); },
  };
}

// A restart/TTL may remove an unfinished server task. Keep only results already
// delivered to this browser, and let the user start again without a stuck form.
export function cachedBatchWithoutServer(store, expectedId) {
  const cached = store.read();
  if (expectedId !== undefined && cached?.batch.id !== expectedId) return cached?.batch || null;
  if (!cached || terminal(cached.batch)) return cached?.batch || null;
  const batch = cached.batch;
  const pending = (batch.counts.queued || 0) + (batch.counts.running || 0) + (batch.counts.awaiting_code || 0);
  const ended = { ...batch, status: 'cancelled', active_jobs: [],
    error: '服务器临时任务已结束或过期；本浏览器已接收的成功结果仍可下载。',
    counts: { ...batch.counts, queued: 0, running: 0, awaiting_code: 0, cancelled: batch.counts.cancelled + pending },
    rows: batch.rows.map((row) => ['queued', 'running', 'awaiting_code'].includes(row.status) ? { ...row, status: 'cancelled', message: '临时任务已结束' } : row) };
  store.save(ended); store.markCollected(batch.id); return ended;
}

let runtimeStore;
export function getBrowserResults() {
  if (!runtimeStore) {
    let storage;
    // Without cross-tab locking, keep credentials only in this page. A copied
    // sessionStorage marker must never let a second tab take and ACK results.
    try { if (globalThis.navigator?.locks?.request) storage = globalThis.localStorage; } catch {}
    runtimeStore = createBrowserResultStore({ owner: globalThis.document?.querySelector('meta[name="rt-workspace"]')?.content, storage, storageMode: 'local' });
    globalThis.addEventListener?.('storage', (event) => {
      if (event.key !== null && event.key !== RESULT_CACHE_PREFIX + runtimeStore.owner) return;
      runtimeStore.read();
      globalThis.document?.dispatchEvent(new CustomEvent('rt:browser-results'));
    });
  }
  return runtimeStore;
}
export function withBrowserResultLock(operation) {
  const owner = getBrowserResults().owner || 'page';
  return globalThis.navigator?.locks?.request
    ? globalThis.navigator.locks.request(RESULT_CACHE_PREFIX + owner, { mode: 'exclusive' }, operation)
    : Promise.resolve().then(operation);
}

export function createResultCollector({ store, now = Date.now, minInterval = 5000, lock = (operation) => operation(), requireKnown = false } = {}) {
  let pending = null, lastAt = -Infinity, lastBatch = null, generation = 0;
  return {
    async collect(batch, requestJSON) {
      if (pending) return pending;
      const requestGeneration = generation;
      const operation = lock(async () => {
        if (requestGeneration !== generation) return null;
        const existing = store.read();
        if (requireKnown && existing?.batch.id !== batch.id) return null;
        if (existing?.batch.id === batch.id && existing.collected && terminal(existing.batch)) return existing.batch;
        const state = store.save(batch);
        const missing = (batch.counts?.success || 0) > state.records.length;
        if (!missing && !terminal(batch)) return batch;
        if (lastBatch === batch.id && now() - lastAt < minInterval) return batch;
        lastBatch = batch.id; lastAt = now();
        const payload = await requestJSON(`/api/batches/${encodeURIComponent(batch.id)}/results`);
        if (requestGeneration !== generation || (requireKnown && store.read()?.batch.id !== batch.id)) return null;
        const received = store.save(payload.batch || batch, payload.records || []);
        // The owner lock covers receipt, browser persistence, and deletion ACK.
        const complete = terminal(payload.batch || batch);
        await requestJSON(`/api/batches/${encodeURIComponent(batch.id)}/ack`, {
          method: 'POST', body: JSON.stringify({ ids: received.records.map((record) => record.id), complete }),
        });
        if (requestGeneration !== generation) return null;
        if (complete) store.markCollected(batch.id);
        return payload.batch || batch;
      });
      pending = operation;
      try { return await operation; } finally { if (pending === operation) pending = null; }
    },
    settled(id) { const state = store.read(); return state?.batch.id === id && !!state.collected; },
    reset() { generation += 1; pending = null; lastBatch = null; lastAt = -Infinity; },
  };
}

let collector;
export function collectBatchResults(batch, requestJSON) {
  collector ||= createResultCollector({ store: getBrowserResults(), lock: withBrowserResultLock, requireKnown: true });
  return collector.collect(batch, requestJSON);
}
export function isBatchCollected(id) {
  return getBrowserResults().read()?.batch.id === id && !!getBrowserResults().read()?.collected;
}
export function resetResultCollection() { collector?.reset(); }

export function mergedSub2Api(records) {
  return { type: 'sub2api-data', version: 1, exported_at: new Date().toISOString(), proxies: [],
    accounts: records.flatMap((record) => Array.isArray(record.sub2api.accounts) ? record.sub2api.accounts : []) };
}
const jsonBlob = (value) => new Blob([JSON.stringify(value, null, 2) + '\n'], { type: 'application/json' });
export function resultDownload(state, kind, jobId = null) {
  const records = jobId ? state?.records.filter((record) => record.id === jobId) : state?.records;
  if (!records?.length) throw new Error('本浏览器尚未接收到这些结果，或缓存已过期。');
  if (jobId) {
    const record = records[0], value = kind === 'cpa' ? record.cpa : kind === 'sub2api' ? record.sub2api : record.raw;
    return { blob: jsonBlob(value), filename: `${kind === 'cpa' ? 'codex' : kind === 'sub2api' ? 'sub2api' : 'tokens'}-${record.id}.json` };
  }
  const batchId = state.batch.id;
  if (kind === 'sub2api') return { blob: jsonBlob(mergedSub2Api(records)), filename: `sub2api-${batchId}.json` };
  if (kind === 'rt') return { blob: new Blob([records.map((record) => record.raw.refresh_token).join('\n') + '\n'], { type: 'text/plain' }), filename: `refresh-tokens-${batchId}.txt` };
  const entries = records.map((record) => ({ fileName: `${kind === 'cpa' ? 'codex' : 'tokens'}-${record.id}.json`, text: JSON.stringify(kind === 'cpa' ? record.cpa : record.raw, null, 2) + '\n' }));
  if (kind !== 'cpa') entries.push({ fileName: 'manifest.json', text: JSON.stringify({ batch_id: batchId, rows: state.batch.rows }, null, 2) });
  return { blob: buildZipArchive(entries), filename: `${kind === 'cpa' ? 'cpa' : 'tokens'}-${batchId}.zip` };
}
