/**
 * The real IP page renderer and registered controls, exercised with a minimal
 * DOM and an injected controller. Browser layout and downloads have separate
 * visual checks; these tests never request IPOK or use real account data.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import * as networkCore from '../rt_extractor/static/network-core.mjs';

class Element {
  constructor(tagName = 'div') {
    this.tagName = tagName.toUpperCase();
    this.value = '';
    this.disabled = false;
    this.hidden = false;
    this.children = [];
    this.dataset = {};
    this.style = { setProperty() {}, removeProperty() {} };
    this.attributes = new Map();
    this.listeners = new Map();
    this.ownText = '';
    const classes = new Set();
    this.classList = {
      add: (...items) => items.forEach((item) => classes.add(item)),
      remove: (...items) => items.forEach((item) => classes.delete(item)),
      contains: (item) => classes.has(item),
      toggle: (item, force) => {
        const include = force ?? !classes.has(item);
        if (include) classes.add(item); else classes.delete(item);
        return include;
      },
    };
  }
  set textContent(value) { this.ownText = String(value ?? ''); this.children = []; }
  get textContent() { return this.ownText + this.children.map((child) => typeof child === 'string' ? child : child.textContent).join(''); }
  set innerHTML(_value) { throw new Error('Network data must not be assigned as HTML'); }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.append(child); return child; }
  replaceChildren(...children) { this.children = children; this.ownText = ''; }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  removeAttribute(name) { this.attributes.delete(name); }
  hasAttribute(name) { return this.attributes.has(name); }
  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }
  querySelectorAll(selector) {
    return this.children.flatMap((child) => typeof child === 'string' ? [] : [
      ...(selector === '*' || child.tagName?.toLowerCase() === selector ? [child] : []),
      ...child.querySelectorAll(selector),
    ]);
  }
  focus() { this.focused = true; }
  select() { this.selected = true; }
  async dispatch(type, extra = {}) {
    if (type === 'click' && this.disabled) return;
    const event = { target: this, currentTarget: this, preventDefault() {}, stopPropagation() {}, ...extra };
    for (const listener of this.listeners.get(type) || []) await listener(event);
    await Promise.resolve();
  }
  click() { return this.dispatch('click'); }
}

const NOW = Date.parse('2026-10-22T09:00:00Z');
const sourceData = (overrides = {}) => ({
  geo: {
    ip: '198.51.100.42', country: '合成测试国家', countryCode: 'ZZ', region: '合成测试州',
    city: '合成测试城市', timezone: 'Etc/UTC', offset: 0, lat: 34.0522, lon: -118.2436,
    asn: 'AS64500', asName: 'Synthetic ASN', isp: '合成测试 ISP', org: '合成测试组织', reverse: 'synthetic.example.test',
  },
  risk: 35, riskAssessment: { status: 'limited', scoreAvailable: true, currentSources: ['synthetic-current'], staleSources: ['synthetic-stale'], undatedSources: [], assessedAt: '2026-10-22T09:00:00Z' },
  purityBand: { label: '较低风险迹象' }, ipType: 'residential', usageType: 'isp', nativeType: 'unknown',
  sharedUsers: { range: '1-5', quality: 'good' }, attribution: { provider: '合成测试出口提供商', company: '合成测试公司', asnOrg: '合成 ASN 组织' },
  evidence: { confidence: 'single', dedicatedVotes: 1, voters: ['synthetic-current'], directSources: ['synthetic-current'], floors: [] },
  riskBreakdown: { contributors: [{ source: 'synthetic-current', risk: 35, weight: 1 }], weightedAvg: 35, floors: [], final: 35 },
  sources: [
    { source: 'synthetic-current', ok: true, risk: 35, flags: { hosting: true, proxy: false }, observedAt: '2026-10-22T09:00:00Z' },
    { source: 'synthetic-stale', ok: true, risk: 70, flags: { vpn: true }, observedAt: '2026-09-01T09:00:00Z', upstream: 'stale' },
  ],
  sourceAttempts: [{ source: 'synthetic-current', status: 'success', upstream: 'live', elapsedMs: 34 }, { source: 'synthetic-stale', status: 'success', reason: 'quota', upstream: 'stale', elapsedMs: 5 }],
  signals: ['hosting'], blocklist: { listed: [], checked: 5, total: 5 },
  rdap: { registry: 'Synthetic registry', country: 'ZZ', registered: '2020-01-01', org: 'Synthetic registry organization' },
  services: [{ key: 'chatgpt', status: 'available' }, { key: 'claude', status: 'unknown' }],
  media: [{ key: 'netflix', status: 'unknown' }],
  upstream: 'cached-only', fetchedAt: '2026-10-22T09:00:00Z', quota: { selfLookup: { remaining: 12, limit: 20, resetsInSeconds: 3600 } },
  ...overrides,
});

function createPage() {
  const elements = new Map();
  const byId = (id) => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const examples = [new Element('button'), new Element('button')];
  examples[0].dataset.networkExample = '1.1.1.1';
  examples[1].dataset.networkExample = '8.8.8.8';
  const document = {
    getElementById: byId,
    createElement: (tag) => new Element(tag),
    createElementNS: (_namespace, tag) => new Element(tag),
    createTextNode: (text) => ({ textContent: String(text) }),
    createDocumentFragment: () => new Element('fragment'),
    documentElement: new Element('html'), body: new Element('body'),
    querySelectorAll: (selector) => selector.includes('data-network-example') ? examples : [],
    querySelector: (selector) => selector.startsWith('#') ? byId(selector.slice(1)) : null,
    addEventListener() {},
  };
  document.body.dataset.page = 'network';
  const clipboard = [];
  const navigator = { clipboard: { writeText: async (value) => { clipboard.push(value); } } };
  const windowListeners = new Map();
  const window = {
    navigator, sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    fetch: async () => { throw new Error('No network requests are allowed in render tests'); },
    addEventListener(type, listener) {
      if (!windowListeners.has(type)) windowListeners.set(type, []);
      windowListeners.get(type).push(listener);
    },
  };
  const calls = [];
  let onChange;
  let state = { phase: 'idle', report: null, selfReport: null, busy: false, cooldownSeconds: 0, cached: false, error: '', targetIP: '' };
  const createNetworkController = (options) => {
    onChange = options.onChange;
    return {
      getState: () => state,
      start: async () => { calls.push({ kind: 'start' }); onChange(state); return state; },
      refresh: async (options) => { calls.push({ kind: 'refresh', options }); return state; },
      query: async (value) => { calls.push({ kind: 'query', value }); return state; },
      dispose: () => { calls.push({ kind: 'dispose' }); },
    };
  };
  const source = readFileSync(new URL('../rt_extractor/static/network.js', import.meta.url), 'utf8')
    .replace(/^import[^\n]*\n/gm, '');
  const context = vm.createContext({
    ...networkCore, createNetworkController, document, window, navigator, console,
    URL, URLSearchParams, TextEncoder, Date, Error, BigInt, Number,
    setTimeout: () => 1, clearTimeout() {}, requestAnimationFrame: (callback) => callback(),
  });
  vm.runInContext(source, context, { filename: 'network.js' });
  return {
    byId, calls, clipboard, examples, document,
    emit(changes) { state = { ...state, ...changes }; onChange(state); },
    report(raw = sourceData()) {
      const report = networkCore.normalizeNetworkResponse(raw, NOW);
      state = { ...state, phase: 'limited', report, selfReport: report, busy: false, cached: false, error: '', targetIP: '' };
      onChange(state); return report;
    },
    allText() { return [...elements.values()].map((element) => element.textContent).join('\n'); },
  };
}

test('expanded report renders geographic facts, sourced estimates, and evidence details', () => {
  const page = createPage();
  page.report();
  assert.equal(page.byId('network-ip').textContent, '198.51.100.42');
  assert.equal(page.byId('network-score').textContent, '35');
  assert.match(page.byId('network-country').textContent, /合成测试国家/);
  assert.equal(page.byId('network-region').textContent, '合成测试州');
  assert.equal(page.byId('network-city').textContent, '合成测试城市');
  assert.match(page.byId('network-timezone').textContent, /Etc\/UTC/);
  assert.match(page.byId('network-coordinates').textContent, /34\.0522/);
  assert.match(page.byId('network-isp').textContent, /合成测试 ISP/);
  assert.match(page.byId('network-org').textContent, /合成测试组织/);
  assert.equal(page.byId('network-reverse').textContent, 'synthetic.example.test');
  assert.match(page.byId('network-shared-users').textContent, /1.?5/);
  assert.match(page.byId('network-shared-note').textContent, /估|非实测/);
  assert.equal(page.byId('network-shared-marker').hidden, false);
  assert.equal(page.byId('network-shared-marker').value, 1, 'The shared marker represents the returned qualitative band, not the risk score');
  assert.match(page.byId('network-native-type').textContent, /未知/);
  assert.match(page.byId('network-native-note').textContent, /不是原生出口证明|不能证明原生/);
  assert.match(page.byId('network-details').textContent, /synthetic-current/);
  assert.match(page.byId('network-details').textContent, /synthetic-stale/);
  assert.match(page.byId('network-details').textContent, /资料已过期/);
  assert.match(page.byId('network-details').textContent, /未标记：代理/);
  assert.equal(page.byId('network-degraded').hidden, false);
  assert.match(page.byId('network-services').textContent, /ChatGPT/i);
  assert.match(page.byId('network-media').textContent, /Netflix/i);
  assert.match(page.byId('network-services').textContent, /地区资料快照.*非实时解锁测试/);
  assert.doesNotMatch(page.byId('network-services').textContent, /实时.*解锁成功/);
});

test('missing optional facts remain unknown and never fabricate shared-user estimates', () => {
  const page = createPage();
  page.report(sourceData({ geo: { ip: '203.0.113.77' }, sharedUsers: undefined, nativeType: undefined, attribution: undefined, evidence: undefined, riskBreakdown: undefined, rdap: undefined, services: undefined, media: undefined }));
  for (const id of ['network-country', 'network-region', 'network-city', 'network-timezone', 'network-coordinates', 'network-org', 'network-reverse']) {
    assert.match(page.byId(id).textContent, /未知|暂无|—|未提供/);
  }
  assert.match(page.byId('network-shared-users').textContent, /未知|暂无|—|未提供/);
  assert.doesNotMatch(page.byId('network-shared-users').textContent, /1.?5/);
  assert.equal(page.byId('network-shared-marker').hidden, true);
});

test('a supported zero risk and zero coordinates remain real values rather than missing data', () => {
  const page = createPage();
  page.report(sourceData({
    geo: { ip: '203.0.113.77', country: '合成零坐标地区', timezone: 'Etc/UTC', offset: 0, lat: 0, lon: 0 },
    risk: 0, riskAssessment: { status: 'current', scoreAvailable: true, currentSources: ['synthetic-current'], staleSources: [] },
    purityBand: { label: '低风险迹象' }, upstream: 'live',
  }));
  assert.equal(page.byId('network-score').textContent, '0');
  assert.match(page.byId('network-coordinates').textContent, /0/);
  assert.doesNotMatch(page.byId('network-coordinates').textContent, /未知|暂无|未提供/);
  assert.match(page.byId('network-timezone').textContent, /UTC/);
});

test('insufficient evidence removes the numeric score and resets the SVG risk arc', () => {
  const page = createPage();
  page.report();
  page.report(sourceData({ risk: 0, riskAssessment: { status: 'insufficient', scoreAvailable: false, currentSources: [], staleSources: [] } }));
  assert.equal(page.byId('network-score').textContent, '—');
  assert.match(page.byId('network-verdict').textContent, /证据不足/);
  assert.equal(page.document.documentElement.dataset.riskLevel, 'unknown');
  const arc = page.byId('network-gauge-arc');
  assert.ok(arc.hidden || arc.getAttribute('stroke-dasharray') === '0 478' || /^0(?:[ ,]|$)/u.test(arc.getAttribute('stroke-dasharray') || ''), 'No colored arc may imply a score when evidence is insufficient');
});

test('failed refresh clears stale detailed values rather than presenting old data as current', () => {
  const page = createPage();
  page.report();
  page.emit({ phase: 'error', report: null, selfReport: null, error: '合成检测失败', busy: false });
  assert.equal(page.byId('network-score').textContent, '—');
  assert.equal(page.byId('network-ip').textContent, '—');
  assert.equal(page.byId('network-error').hidden, false);
  assert.match(page.byId('network-error').textContent, /合成检测失败/);
  assert.doesNotMatch(page.byId('network-details').textContent, /synthetic-current/);
  assert.doesNotMatch(page.byId('network-country').textContent, /合成测试国家/);
});

test('an explicit target report never replaces the browser self-IP summary', () => {
  const page = createPage();
  const self = page.report();
  const target = networkCore.normalizeNetworkResponse(sourceData({ geo: { ip: '1.1.1.1', country: '合成目标国家' }, risk: 55 }), NOW);
  target.isSelfLookup = false;
  page.emit({ report: target, selfReport: self, targetIP: '1.1.1.1' });
  assert.equal(page.byId('network-ip').textContent, '1.1.1.1');
  assert.equal(page.byId('network-summary-ip').textContent, self.ip);
  assert.equal(page.byId('network-summary-score').textContent, '35 / 100');
  assert.match(`${page.byId('network-ip-label').textContent} ${page.byId('network-query-context').textContent}`, /查询|目标/);
  page.emit({ phase: 'error', report: null, selfReport: self, targetIP: '1.1.1.1', error: '合成目标查询失败' });
  assert.equal(page.byId('network-ip').textContent, '—');
  assert.equal(page.byId('network-summary-ip').textContent, self.ip);
});

test('provider strings stay text and cannot create HTML through detailed rendering', () => {
  const page = createPage();
  const untrustedName = '<img src=x onerror=synthetic()>';
  page.report(sourceData({
    geo: { ip: '203.0.113.77', country: '<script>synthetic()</script>', org: untrustedName },
    sources: [{ source: untrustedName, ok: true, risk: 35, flags: { proxy: true }, observedAt: '2026-10-22T09:00:00Z' }],
    sourceAttempts: [{ source: untrustedName, status: 'success' }],
    riskAssessment: { status: 'limited', scoreAvailable: true, currentSources: [untrustedName], staleSources: [] },
  }));
  assert.equal(page.byId('network-country').textContent, '<script>synthetic()</script>');
  assert.match(page.byId('network-details').textContent, /<img src=x onerror=synthetic\(\)>/);
  assert.equal(page.byId('network-details').querySelectorAll('img').length, 0);
});

test('entering an IP does not query until submission and invalid targets never request', async () => {
  const page = createPage();
  page.report();
  page.calls.length = 0;
  page.byId('network-query-input').value = '1.1.1.1';
  await page.byId('network-query-input').dispatch('input');
  assert.equal(page.calls.length, 0);
  await page.byId('network-query-form').dispatch('submit');
  assert.ok(page.calls.some((call) => call.kind === 'query' && call.value === '1.1.1.1' || call.kind === 'refresh' && call.options?.targetIP === '1.1.1.1'));
  page.calls.length = 0;
  page.byId('network-query-input').value = '127.0.0.1';
  await page.byId('network-query-form').dispatch('submit');
  assert.equal(page.calls.length, 0);
  assert.match(`${page.byId('network-query-feedback').textContent} ${page.byId('network-error').textContent}`, /公网|有效|IP/);
});

test('refresh preserves the active target while self controls explicitly request the browser exit', async () => {
  const page = createPage();
  page.report();
  page.calls.length = 0;
  await page.byId('network-refresh').click();
  await page.byId('network-recheck').click();
  assert.equal(page.calls[0].kind, 'refresh');
  assert.equal(page.calls[0].options, undefined);
  assert.equal(page.calls[1].kind, 'refresh');
  assert.equal(page.calls[1].options, undefined);
  page.byId('network-query-input').value = '1.1.1.1';
  await page.byId('network-self-button').click();
  assert.equal(page.calls[2].options.targetIP, '');
  assert.equal(page.byId('network-query-input').value, '');
  await page.byId('network-summary-refresh').click();
  assert.equal(page.calls[3].options.targetIP, '');
  page.calls.length = 0;
  await page.examples[0].click();
  assert.equal(page.byId('network-query-input').value, '1.1.1.1');
  assert.equal(page.calls.length, 0, 'Example addresses only fill the input and never consume lookup quota');
});

test('copy and numeric-IP visibility controls operate only on the displayed address', async () => {
  const page = createPage();
  page.report();
  await page.byId('network-copy-ip').click();
  assert.deepEqual(page.clipboard, ['198.51.100.42']);
  assert.doesNotMatch(page.byId('network-ip-numeric').textContent, /3325256746/);
  await page.byId('network-toggle-numeric').click();
  assert.equal(page.byId('network-ip-numeric').textContent, '3325256746');
  await page.byId('network-toggle-numeric').click();
  assert.doesNotMatch(page.byId('network-ip-numeric').textContent, /3325256746/);
});

test('loading and rate-limit cooldown disable actions without inventing results', () => {
  const page = createPage();
  page.emit({ phase: 'loading', report: null, selfReport: null, busy: true });
  for (const id of ['network-refresh', 'network-summary-refresh', 'network-query-button', 'network-self-button']) {
    assert.equal(page.byId(id).disabled, true, id);
  }
  assert.equal(page.byId('network-query-input').disabled, true);
  assert.equal(page.byId('network-copy-ip').disabled, true);
  page.emit({ phase: 'error', busy: false, cooldownSeconds: 40, error: '合成额度限制' });
  for (const id of ['network-refresh', 'network-summary-refresh', 'network-query-button']) assert.equal(page.byId(id).disabled, true, id);
  assert.equal(page.byId('network-score').textContent, '—');
});
