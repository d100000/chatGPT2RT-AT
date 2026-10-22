import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import {
  NETWORK_API_URL, NETWORK_CACHE_KEY, NETWORK_CACHE_TTL, createNetworkController,
  normalizeNetworkResponse, readNetworkCache, readNetworkCooldown, retryAfterMilliseconds, riskLevel,
  validateTargetIP, ipToDecimal,
} from '../rt_extractor/static/network-core.mjs';

const NOW = Date.parse('2026-10-22T05:00:00Z');
const api = (changes = {}) => ({
  geo: { ip: '203.0.113.45', version: 4, country: 'Synthetic country', city: 'Synthetic city', asn: 'AS64512', isp: 'Synthetic ISP' },
  risk: 35, riskAssessment: { status: 'limited', scoreAvailable: true, currentSources: ['synthetic-source'], staleSources: [], undatedSources: [], assessedAt: '2026-10-22T05:00:00Z' },
  purityBand: { label: '较低风险迹象' }, ipType: 'hosting', signals: ['hosting'],
  sources: [{ source: 'synthetic-source', ok: true, risk: 35, observedAt: '2026-10-22T05:00:00Z', raw: { secret: 'DO-NOT-STORE-RAW' } }],
  sourceAttempts: [{ source: 'synthetic-source', status: 'success' }, { source: 'missing-source', status: 'not_called' }],
  fetchedAt: '2026-10-22T05:00:00Z', upstream: 'cached-only',
  blocklist: { listed: [], checked: 5, total: 5 }, quota: { selfLookup: { remaining: 18, limit: 20, resetsInSeconds: 3600 } },
  ...changes,
});
const response = (body = api(), status = 200, headers = {}) => ({
  ok: status >= 200 && status < 300, status,
  headers: { get: (name) => headers[name] ?? null }, text: async () => JSON.stringify(body),
});
function memoryStorage() {
  const values = new Map();
  return { getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: (key) => values.delete(key) };
}
function clock() {
  let current = NOW, id = 0;
  const timers = new Map();
  return {
    now: () => current,
    setTimer(callback, delay) { timers.set(++id, { callback, due: current + delay }); return id; },
    clearTimer(key) { timers.delete(key); },
    advance(milliseconds) {
      const goal = current + milliseconds;
      while (true) {
        const entry = [...timers.entries()].sort((a, b) => a[1].due - b[1].due).find(([, value]) => value.due <= goal);
        if (!entry) break;
        const [key, value] = entry; current = value.due; timers.delete(key); value.callback();
      }
      current = goal;
    },
  };
}

test('normalization keeps sourced IP facts, unavailable providers, and explicit evidence limitations', () => {
  const report = normalizeNetworkResponse(api({ password: 'DO-NOT-STORE-PASSWORD' }), NOW);
  assert.equal(report.ip, '203.0.113.45');
  assert.equal(report.risk, 35); assert.equal(report.verdict, '较低风险迹象');
  assert.equal(report.evidenceStatus, 'limited'); assert.equal(report.cachedOnly, true);
  assert.equal(report.sources[1].status, 'not_called');
  assert.deepEqual(report.quota, { remaining: 18, limit: 20, resetsInSeconds: 3600 });
  assert.doesNotMatch(JSON.stringify(report), /DO-NOT-STORE|password|"raw"/);
});

test('missing, false, insufficient, and invalid score evidence hides the compatibility score', () => {
  for (const changes of [
    { riskAssessment: undefined },
    { riskAssessment: { scoreAvailable: false, status: 'limited' } },
    { riskAssessment: { scoreAvailable: true, status: 'insufficient' } },
    ...[null, '35', NaN, Infinity, -1, 101].map((risk) => ({ risk })),
  ]) {
    const report = normalizeNetworkResponse(api(changes), NOW);
    assert.equal(report.risk, null); assert.equal(report.verdict, '证据不足'); assert.equal(report.riskLevel, 'unknown');
  }
  assert.equal(normalizeNetworkResponse(api({ risk: 0 }), NOW).risk, 0);
});

test('malformed responses reject without echoing data; IPv6 and bounded text are supported', () => {
  for (const value of [null, [], {}, { geo: {} }, { geo: { ip: 'not-an-ip' } }, { geo: { ip: '999.0.0.1' } }]) {
    assert.throws(() => normalizeNetworkResponse(value, NOW), /^Error: invalid_response$/);
  }
  const report = normalizeNetworkResponse(api({ geo: { ip: '2001:db8::1', country: 'x'.repeat(1000) } }), NOW);
  assert.equal(report.ipVersion, 6); assert.equal(report.country.length, 120);
  assert.deepEqual([0, 14, 15, 49, 50, 69, 70, 100, null].map(riskLevel), ['low', 'low', 'moderate', 'moderate', 'moderate', 'moderate', 'high', 'high', 'unknown']);
});

test('cache validates age and schema and drops injected properties', () => {
  const report = normalizeNetworkResponse(api(), NOW);
  const raw = JSON.stringify({ version: 1, savedAt: NOW, report: { ...report, password: 'DO-NOT-STORE', extra: { access_token: 'DO-NOT-STORE' } } });
  assert.equal(readNetworkCache(raw, NOW + 1000).ip, report.ip);
  assert.doesNotMatch(JSON.stringify(readNetworkCache(raw, NOW)), /DO-NOT-STORE|access_token|password/);
  assert.equal(readNetworkCache(raw, NOW + NETWORK_CACHE_TTL), null);
  assert.equal(readNetworkCache(raw, NOW - 1), null);
  for (const bad of ['bad JSON', '{}', '[]', JSON.stringify({ version: 1, savedAt: NOW, report: {} })]) assert.equal(readNetworkCache(bad, NOW), null);
});

test('automatic start makes one honest browser self-query and concurrent refresh shares its promise', async () => {
  const storage = memoryStorage(); const changes = []; const calls = [];
  let resolve;
  const controller = createNetworkController({ storage, now: () => NOW, onChange: (state) => changes.push(state), fetchImpl: (...args) => { calls.push(args); return new Promise((done) => { resolve = done; }); } });
  const first = controller.start(); const second = controller.refresh();
  assert.equal(first, second); assert.equal(calls.length, 1); assert.equal(controller.getState().phase, 'loading');
  assert.equal(calls[0][0], NETWORK_API_URL); assert.equal(new URL(calls[0][0]).search, '');
  assert.equal(calls[0][1].credentials, 'omit'); assert.equal(calls[0][1].referrerPolicy, 'no-referrer'); assert.equal(calls[0][1].cache, 'no-store');
  assert.equal(calls[0][1].headers, undefined);
  resolve(response()); const completed = await first;
  assert.equal(completed.phase, 'limited'); assert.equal(completed.report.risk, 35);
  await controller.start(); assert.equal(calls.length, 1);
  assert.doesNotMatch(storage.getItem(NETWORK_CACHE_KEY), /DO-NOT-STORE-RAW/);
  assert.equal(changes[0].busy, true); assert.equal(changes.at(-1).busy, false);
  controller.dispose();
});

test('session navigation reuses normalized cache; manual refresh bypasses it and failure clears old scores', async () => {
  const storage = memoryStorage(); let calls = 0;
  const first = createNetworkController({ storage, now: () => NOW, fetchImpl: async () => { calls += 1; return response(); } });
  await first.start(); first.dispose();
  const second = createNetworkController({ storage, now: () => NOW + 1000, fetchImpl: async () => { calls += 1; throw new Error('untrusted response details'); } });
  const cached = await second.start(); assert.equal(cached.cached, true); assert.equal(calls, 1);
  const failed = await second.refresh(); assert.equal(calls, 2); assert.equal(failed.phase, 'error'); assert.equal(failed.report, null);
  assert.equal(storage.getItem(NETWORK_CACHE_KEY), null); assert.doesNotMatch(failed.error, /untrusted/);
  second.dispose();
});

test('expired and inaccessible session storage do not block a fresh query', async () => {
  const storage = memoryStorage();
  storage.setItem(NETWORK_CACHE_KEY, JSON.stringify({ version: 1, savedAt: NOW - NETWORK_CACHE_TTL, report: normalizeNetworkResponse(api(), NOW - NETWORK_CACHE_TTL) }));
  for (const store of [storage, { getItem() { throw new Error('blocked'); }, removeItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); } }]) {
    let calls = 0; const controller = createNetworkController({ storage: store, now: () => NOW, fetchImpl: async () => { calls += 1; return response(); } });
    const state = await controller.start(); assert.equal(calls, 1); assert.equal(state.report.ip, '203.0.113.45'); controller.dispose();
  }
});

test('429 waits for Retry-After, stays disabled across navigation, and then permits manual retry', async () => {
  const time = clock(); const storage = memoryStorage(); let calls = 0;
  const controller = createNetworkController({ ...time, storage, fetchImpl: async () => { calls += 1; return calls === 1 ? response({}, 429, { 'Retry-After': '12' }) : response(); } });
  const state = await controller.start(); assert.equal(state.errorKind, 'rate_limited'); assert.equal(state.cooldownSeconds, 12);
  await controller.refresh(); assert.equal(calls, 1);
  const other = createNetworkController({ ...time, storage, fetchImpl: async () => { calls += 1; return response(); } });
  assert.equal((await other.start()).cooldownSeconds, 12); assert.equal(calls, 1); other.dispose();
  time.advance(12000); assert.equal(controller.getState().cooldownSeconds, 0);
  await controller.refresh(); assert.equal(calls, 2); assert.equal(controller.getState().phase, 'limited'); controller.dispose();
});

test('Retry-After handles missing/exposed-unavailable/date values and bounded cooldowns', () => {
  assert.equal(retryAfterMilliseconds(null, NOW), 60000);
  assert.equal(retryAfterMilliseconds('0', NOW), 60000);
  assert.equal(retryAfterMilliseconds('12', NOW), 12000);
  assert.equal(retryAfterMilliseconds(new Date(NOW + 30000).toUTCString(), NOW), 30000);
  assert.equal(retryAfterMilliseconds('999999999', NOW), 86400000);
  assert.equal(readNetworkCooldown(JSON.stringify({ version: 1, errorKind: 'rate_limited', cooldownUntil: NOW + 1000 }), NOW), NOW + 1000);
  assert.equal(readNetworkCooldown('{}', NOW), 0);
});

test('timeout aborts the browser request and never produces an invented score', async () => {
  const time = clock(); let signal;
  const controller = createNetworkController({ ...time, timeoutMs: 20000, fetchImpl: (_url, options) => { signal = options.signal; return new Promise(() => {}); } });
  const pending = controller.start(); time.advance(20000);
  const state = await pending; assert.equal(signal.aborted, true); assert.equal(state.errorKind, 'timeout'); assert.equal(state.report, null); assert.equal(state.busy, false);
  controller.dispose();
});

test('upstream errors, HTML, malformed JSON, and unusable payloads show safe failure states', async () => {
  for (const reply of [
    response({ error: 'DO-NOT-DISPLAY' }, 503),
    { ok: true, status: 200, text: async () => '<html>DO-NOT-DISPLAY</html>' },
    { ok: true, status: 200, text: async () => '{"secret":"DO-NOT-DISPLAY",bad}' },
    response({ secret: 'DO-NOT-DISPLAY' }),
  ]) {
    const controller = createNetworkController({ now: () => NOW, fetchImpl: async () => reply });
    const state = await controller.start(); assert.equal(state.phase, 'error'); assert.equal(state.report, null); assert.doesNotMatch(state.error, /DO-NOT-DISPLAY/); controller.dispose();
  }
});

test('page module auto-starts once and renders the same response into summary and detail views', async () => {
  const elements = new Map();
  const element = () => ({ dataset: {}, listeners: new Map(), children: [], textContent: '', hidden: false,
    setAttribute() {}, removeAttribute() {}, append(...items) { this.children.push(...items); }, replaceChildren(...items) { this.children = items; },
    addEventListener(name, listener) { this.listeners.set(name, listener); } });
  const document = { documentElement: element(), getElementById(id) { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); }, createElement: element, querySelectorAll() { return []; } };
  let calls = 0;
  const window = { sessionStorage: memoryStorage(), fetch: async () => { calls += 1; return response(); }, addEventListener() {} };
  const source = readFileSync(new URL('../rt_extractor/static/network.js', import.meta.url), 'utf8').replace(/^import[^\n]*\n/gm, '');
  vm.runInNewContext(source, { document, window, createNetworkController, validateTargetIP, ipToDecimal, Date }, { filename: 'network.js' });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(calls, 1); assert.equal(document.documentElement.dataset.networkState, 'limited');
  assert.equal(elements.get('network-summary-ip').textContent, '203.0.113.45');
  assert.equal(elements.get('network-ip').textContent, '203.0.113.45');
  assert.equal(elements.get('network-score').textContent, '35');
  assert.match(elements.get('network-status').textContent, /证据有限.*IPOK 缓存来源/);
});

const extendedAPI = () => api({
  geo: { ip: '203.0.113.45', country: 'Synthetic country', countryCode: 'US', region: 'Synthetic region', city: 'Synthetic city',
    timezone: 'America/Los_Angeles', offset: -25200, lat: 0, lon: -118.2436, org: 'Synthetic Org', reverse: 'synthetic.example', asn: 64512 },
  usageType: 'isp', nativeType: 'broadcast', sharedUsers: { range: '1-5', quality: 'good' },
  attribution: { provider: 'Synthetic Provider', datacenter: 'Synthetic Datacenter', company: 'Synthetic Company', asnOrg: 'Synthetic ASN', secret: 'DO-NOT-STORE' },
  rdap: { registry: 'ARIN', country: 'US', registered: '2020-01-01T00:00:00Z', org: 'Synthetic Registrant' },
  evidence: { dedicatedVotes: 2, voters: ['source-a', 'source-b'], directSources: ['source-a'], confidence: 'multi', floors: [{ key: 'hosting', floor: 35 }], raw: 'DO-NOT-STORE' },
  riskBreakdown: { contributors: [{ source: 'source-a', risk: 15, weight: 0.8 }], weightedAvg: 15, floors: [{ key: 'hosting', floor: 35 }], final: 35 },
  sources: [{ source: 'source-a', ok: true, risk: 15, flags: { hosting: true, proxy: false, access_token: 'DO-NOT-STORE' }, observedAt: '2026-10-22T05:00:00Z' }, { source: 'source-b', ok: false, risk: 40, flags: { vpn: true }, cache: 'stale' }],
  sourceAttempts: [{ source: 'source-a', status: 'success', elapsedMs: 123, reason: 'cached', upstream: 'cache' }, { source: 'source-b', status: 'unavailable', elapsedMs: 200, reason: 'degraded_cache', upstream: 'stale' }, { source: 'dormant', status: 'not_called', reason: 'dormant' }],
  services: [{ key: 'chatgpt', status: 'available', region: 'US', note: 'Known-region snapshot only' }],
  media: [{ key: 'netflix', status: 'unknown' }],
});

test('expanded screenshot facts keep source metadata and round-trip through the normalized cache', () => {
  const report = normalizeNetworkResponse(extendedAPI(), NOW);
  assert.equal(report.schemaVersion, 2); assert.equal(report.countryCode, 'US');
  assert.equal(report.timezone, 'America/Los_Angeles'); assert.equal(report.utcOffset, -25200);
  assert.equal(report.latitude, 0); assert.equal(report.longitude, -118.2436); assert.equal(report.asn, 'AS64512');
  assert.equal(report.org, 'Synthetic Org'); assert.equal(report.reverse, 'synthetic.example');
  assert.deepEqual(report.sharedUsers, { range: '1-5', quality: 'good', estimated: true });
  assert.equal(report.nativeType, 'broadcast'); assert.equal(report.usageType, 'isp');
  assert.deepEqual(report.degradedSources, ['source-b']); assert.equal(report.sourceCounts.success, 1); assert.equal(report.sourceCounts.unavailable, 1);
  assert.equal(report.sourceCounts.notCalled, 1); assert.equal(report.evidence.confidence, 'multi'); assert.equal(report.evidence.dedicatedVotes, 2);
  assert.equal(report.attribution.company, 'Synthetic Company'); assert.equal(report.rdap.registry, 'ARIN');
  assert.deepEqual(report.sources[0].flags, { proxy: false, hosting: true });
  assert.equal(report.sources[1].degraded, true); assert.equal(report.sourceAttempts[0].elapsedMs, 123);
  assert.equal(report.riskBreakdown.floors[0].floor, 35); assert.equal(report.services[0].region, 'US');
  assert.match(report.services[0].note, /snapshot/); assert.equal(report.media[0].status, 'unknown');
  const restored = readNetworkCache(JSON.stringify({ version: 2, savedAt: NOW, report }), NOW + 1000);
  assert.deepEqual(restored, report);
  assert.doesNotMatch(JSON.stringify(report), /DO-NOT-STORE|access_token|"raw"|"secret"/);
});

test('missing or incorrectly typed extended facts remain unknown without invented data', () => {
  const report = normalizeNetworkResponse(api({ geo: { ip: '203.0.113.45', lat: '34', lon: 181, countryCode: 'INVALID', offset: 999999 },
    usageType: 'secret-value', nativeType: 'really-native', sharedUsers: [], evidence: { dedicatedVotes: '4' },
    services: [{ key: 'chatgpt', password: 'DO-NOT-STORE' }], riskBreakdown: { weightedAvg: Infinity } }), NOW);
  assert.equal(report.latitude, null); assert.equal(report.longitude, null); assert.equal(report.countryCode, ''); assert.equal(report.utcOffset, null);
  assert.equal(report.usageType, 'unknown'); assert.equal(report.nativeType, 'unknown'); assert.equal(report.sharedUsers.range, '');
  assert.equal(report.sharedUsers.quality, 'unknown'); assert.equal(report.evidence.dedicatedVotes, null); assert.equal(report.riskBreakdown.weightedAvg, null);
  assert.equal(report.services[0].status, 'unknown'); assert.doesNotMatch(JSON.stringify(report), /DO-NOT-STORE|secret-value/);
  const old = readNetworkCache(JSON.stringify({ version: 1, savedAt: NOW, report: { ip: '203.0.113.45', risk: null, evidenceStatus: 'insufficient' } }), NOW);
  assert.equal(old.schemaVersion, 1); assert.equal(old.nativeType, 'unknown'); assert.equal(old.sharedUsers.range, '');
});

test('public target validation and IP numeric conversion support IPv4 and IPv6 literals', async () => {
  const { validateTargetIP, ipToDecimal } = await import('../rt_extractor/static/network-core.mjs');
  for (const ip of ['1.1.1.1', '8.8.8.8', '2001:4860:4860::8888', '2606:4700:4700::1111']) assert.ok(validateTargetIP(ip));
  assert.equal(validateTargetIP(''), ''); assert.equal(validateTargetIP(' 1.1.1.1 '), '1.1.1.1');
  for (const ip of ['https://1.1.1.1', 'example.com', '1.1.1.1:80', '01.1.1.1', '999.1.1.1', '0.0.0.0', '10.0.0.1', '127.0.0.1', '169.254.1.1', '172.16.0.1', '192.168.1.1', '100.64.0.1', '198.18.1.1', '203.0.113.1', '224.0.0.1', '::', '::1', 'fd00::1', 'fe80::1', 'ff02::1', '2001:db8::1', '::ffff:192.168.1.1']) assert.throws(() => validateTargetIP(ip), /^Error: invalid_target$/);
  assert.equal(ipToDecimal('1.1.1.1'), '16843009'); assert.equal(ipToDecimal('255.255.255.255'), '4294967295');
  assert.equal(ipToDecimal('::1'), '1'); assert.equal(ipToDecimal('ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff'), '340282366920938463463374607431768211455');
  assert.equal(ipToDecimal('bad'), '');
});

test('explicit target queries preserve browser-exit data and never overwrite its cache', async () => {
  const storage = memoryStorage(); const calls = [];
  const controller = createNetworkController({ storage, now: () => NOW, fetchImpl: async (url, options) => {
    calls.push({ url, options }); const target = new URL(url).searchParams.get('ip');
    return response(target ? api({ geo: { ip: target }, quota: { arbitraryLookup: { remaining: 14, limit: 15 } } }) : extendedAPI());
  } });
  await controller.start(); const cached = storage.getItem(NETWORK_CACHE_KEY);
  const target = await controller.query('8.8.8.8');
  assert.equal(target.targetIP, '8.8.8.8'); assert.equal(target.report.targetIP, '8.8.8.8'); assert.equal(target.report.isSelfLookup, false);
  assert.equal(target.selfReport.ip, '203.0.113.45'); assert.equal(target.report.quota.limit, 15); assert.equal(storage.getItem(NETWORK_CACHE_KEY), cached);
  assert.equal(new URL(calls[1].url).searchParams.get('ip'), '8.8.8.8'); assert.equal(calls[1].options.headers, undefined);
  await controller.refresh(); assert.equal(new URL(calls[2].url).searchParams.get('ip'), '8.8.8.8');
  const self = await controller.start(); assert.equal(self.report.ip, '203.0.113.45'); assert.equal(self.cached, true); assert.equal(calls.length, 3);
  controller.dispose();
});

test('invalid targets, verification refusal, and mismatched target responses fail safely', async () => {
  const controller = createNetworkController({ now: () => NOW, fetchImpl: async () => { throw new Error('must-not-call'); } });
  const bad = await controller.query('127.0.0.1'); assert.equal(bad.errorKind, 'invalid_target'); controller.dispose();
  for (const reply of [response({}, 403), response(api())]) {
    const storage = memoryStorage();
    const instance = createNetworkController({ now: () => NOW, storage, fetchImpl: async () => reply });
    const state = await instance.query('1.1.1.1'); assert.equal(state.phase, 'error'); assert.equal(state.report, null);
    assert.ok(['verification_required', 'invalid_response'].includes(state.errorKind)); assert.equal(storage.getItem(NETWORK_CACHE_KEY), null); instance.dispose();
  }
});

test('target 429 cooldown metadata remains separate from current-exit cached facts', async () => {
  const time = clock(), storage = memoryStorage(); let calls = 0;
  const controller = createNetworkController({ ...time, storage, fetchImpl: async () => ++calls === 1 ? response(extendedAPI()) : response({}, 429, { 'Retry-After': '12' }) });
  await controller.start(); const cached = storage.getItem(NETWORK_CACHE_KEY);
  await controller.query('1.1.1.1'); assert.equal(storage.getItem(NETWORK_CACHE_KEY), cached); assert.equal(controller.getState().selfReport.ip, '203.0.113.45');
  const instance = createNetworkController({ ...time, storage, fetchImpl: async () => { throw new Error('must-not-call'); } });
  const state = await instance.start(); assert.equal(state.cooldownSeconds, 12); assert.equal(calls, 2);
  controller.dispose(); instance.dispose();
});

test('provider contribution weights retain their original scale through cache normalization', () => {
  for (const weight of [0, 0.8, 2, 3, 100000]) {
    const report = normalizeNetworkResponse(api({ riskBreakdown: { contributors: [{ source: 'synthetic-source', risk: 35, weight }] } }), NOW);
    assert.equal(report.riskBreakdown.contributors[0].weight, weight);
    const restored = readNetworkCache(JSON.stringify({ version: 2, savedAt: NOW, report }), NOW);
    assert.equal(restored.riskBreakdown.contributors[0].weight, weight);
  }
  for (const weight of [-1, Infinity, '3', 100001]) {
    assert.equal(normalizeNetworkResponse(api({ riskBreakdown: { contributors: [{ source: 'synthetic-source', risk: 35, weight }] } }), NOW).riskBreakdown.contributors[0].weight, null);
  }
});
