// IPOK's public API contract: https://ipok.io/developers
// Keep this module free of account data, DOM access, and platform credentials.
export const NETWORK_API_URL = 'https://ipok.io/api/ip';
export const NETWORK_CACHE_KEY = 'rt-network-check-v1';
export const NETWORK_COOLDOWN_KEY = 'rt-network-cooldown-v1';
export const NETWORK_CACHE_TTL = 10 * 60 * 1000;

const object = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
const text = (value, max = 120) => typeof value === 'string'
  ? value.replace(/[\u0000-\u001f\u007f]/gu, ' ').trim().slice(0, max) : '';
const names = (value, limit = 20) => Array.isArray(value)
  ? value.slice(0, limit).map((item) => text(item, 80)).filter(Boolean) : [];
const score = (value) => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100 ? value : null;
const count = (value) => Number.isInteger(value) && value >= 0 && value <= 1000000 ? value : null;
const finite = (value, min = -Infinity, max = Infinity) => typeof value === 'number' && Number.isFinite(value) && value >= min && value <= max ? value : null;
const date = (value) => typeof value === 'string' && value.length <= 64 && Number.isFinite(Date.parse(value))
  ? new Date(value).toISOString() : '';

function normalizeIPAddress(value) {
  if (typeof value !== 'string') return '';
  const input = value.trim().replace(/^\[([^\]]+)\]$/u, '$1');
  if (/^(?:\d{1,3}\.){3}\d{1,3}$/u.test(input)) {
    return input.split('.').every((part) => Number(part) <= 255 && String(Number(part)) === part) ? input : '';
  }
  if (!input.includes(':') || !/^[a-f0-9:.]+$/iu.test(input)) return '';
  try { return new URL(`http://[${input}]/`).hostname.slice(1, -1).toLowerCase(); } catch { return ''; }
}

function validIP(value) { return Boolean(normalizeIPAddress(value)); }

function ipv6Words(value) {
  const normalized = normalizeIPAddress(value);
  if (!normalized.includes(':')) return null;
  const [left, right = ''] = normalized.split('::');
  const head = left ? left.split(':') : [];
  const tail = right ? right.split(':') : [];
  return [...head, ...Array(8 - head.length - tail.length).fill('0'), ...tail].map((word) => parseInt(word, 16));
}

/** Numeric representation is an address conversion, never a risk estimate. */
export function ipToDecimal(value) {
  const ip = normalizeIPAddress(value);
  if (!ip) return '';
  if (!ip.includes(':')) return ip.split('.').reduce((result, part) => result * 256n + BigInt(part), 0n).toString();
  return ipv6Words(ip).reduce((result, part) => result * 65536n + BigInt(part), 0n).toString();
}

function publicIPv4(ip) {
  const [a, b, c] = ip.split('.').map(Number);
  return !(a === 0 || a === 10 || a === 127 || a >= 224
    || a === 100 && b >= 64 && b <= 127
    || a === 169 && b === 254 || a === 172 && b >= 16 && b <= 31
    || a === 192 && (b === 168 || b === 0 && (c === 0 || c === 2) || b === 88 && c === 99)
    || a === 198 && (b === 18 || b === 19 || b === 51 && c === 100)
    || a === 203 && b === 0 && c === 113);
}

/** Explicit target lookups accept literal public IP addresses, never hosts/URLs. */
export function validateTargetIP(value) {
  if (typeof value !== 'string') throw new Error('invalid_target');
  if (!value.trim()) return '';
  const ip = normalizeIPAddress(value);
  if (!ip) throw new Error('invalid_target');
  if (!ip.includes(':')) { if (publicIPv4(ip)) return ip; throw new Error('invalid_target'); }
  const words = ipv6Words(ip);
  if (words.slice(0, 5).every((word) => word === 0) && words[5] === 65535) {
    const embedded = [words[6] >> 8, words[6] & 255, words[7] >> 8, words[7] & 255].join('.');
    if (publicIPv4(embedded)) return ip;
    throw new Error('invalid_target');
  }
  if ((words[0] & 0xe000) !== 0x2000 || words[0] === 0x2001 && (words[1] === 0xdb8 || words[1] === 2
    || words[1] >= 0x10 && words[1] <= 0x2f)) throw new Error('invalid_target');
  return ip;
}

const booleanFlags = (value) => {
  if (!object(value)) return {};
  const allowed = ['proxy', 'vpn', 'tor', 'hosting', 'mobile', 'abuser', 'abuse', 'relay', 'whitelisted', 'residential', 'bogon', 'spam', 'recent_abuse', 'datacenter'];
  return Object.fromEntries(allowed.filter((key) => typeof value[key] === 'boolean').map((key) => [key, value[key]]));
};
const shortRecord = (value, keys) => object(value)
  ? Object.fromEntries(keys.map((key) => [key, text(value[key], 200)]).filter(([, value]) => value)) : {};
const floorList = (value) => Array.isArray(value) ? value.slice(0, 20).filter(object).map((item) => ({
  key: text(item.key, 80), floor: score(item.floor), reason: text(item.reason, 200),
})).filter((item) => item.key || item.floor !== null) : [];
const snapshots = (value) => Array.isArray(value) ? value.slice(0, 30).filter(object).map((item) => ({
  key: text(item.key, 80), name: text(item.name || item.label, 100), status: text(item.status, 60) || 'unknown',
  reason: text(item.reason || item.note, 240), country: text(item.country, 80), checkedAt: date(item.checkedAt || item.observedAt),
  region: text(item.region, 100), note: text(item.note || item.reason, 240),
})).filter((item) => item.key || item.name) : [];

export function riskLevel(value) {
  if (score(value) === null) return 'unknown';
  if (value < 15) return 'low';
  if (value < 70) return 'moderate';
  return 'high';
}

function fallbackVerdict(value) {
  if (value === null) return '证据不足';
  if (value < 15) return '低风险迹象';
  if (value < 50) return '较低风险迹象';
  if (value < 70) return '警惕';
  return '高风险';
}

/** Normalize only displayable IP facts. Raw response bodies never enter storage. */
export function normalizeNetworkResponse(payload, checkedAt = Date.now()) {
  if (!object(payload) || !object(payload.geo)) throw new Error('invalid_response');
  const ip = text(payload.geo.ip, 64);
  if (!validIP(ip)) throw new Error('invalid_response');
  const assessment = object(payload.riskAssessment) ? payload.riskAssessment : {};
  const evidenceStatus = ['current', 'limited', 'insufficient'].includes(assessment.status) ? assessment.status : 'insufficient';
  const availableScore = assessment.scoreAvailable === true && evidenceStatus !== 'insufficient' ? score(payload.risk) : null;
  const currentSources = names(assessment.currentSources);
  const staleSources = names(assessment.staleSources);
  const undatedSources = names(assessment.undatedSources);
  const attempts = Array.isArray(payload.sourceAttempts) ? payload.sourceAttempts.slice(0, 20).filter(object) : [];
  const rawSources = Array.isArray(payload.sources) ? payload.sources.slice(0, 20).filter(object) : [];
  const allNames = [...new Set([...rawSources.map((item) => text(item.source, 80)), ...attempts.map((item) => text(item.source, 80))].filter(Boolean))].slice(0, 20);
  const sources = allNames.map((name) => {
    const source = rawSources.find((item) => text(item.source, 80) === name) || {};
    const attempt = attempts.find((item) => text(item.source, 80) === name) || {};
    const status = ['success', 'unavailable', 'not_called'].includes(attempt.status)
      ? attempt.status : source.ok === true ? 'success' : source.ok === false ? 'unavailable' : 'unknown';
    return {
      name, status, risk: score(source.risk), observedAt: date(source.observedAt),
      freshness: currentSources.includes(name) ? 'current' : staleSources.includes(name) ? 'stale' : undatedSources.includes(name) ? 'undated' : 'unknown',
      flags: booleanFlags(source.flags), reason: text(attempt.reason || source.reason, 200),
      upstream: text(attempt.upstream || source.upstream || source.cache, 80),
      elapsedMs: finite(attempt.elapsedMs, 0, 3600000),
      degraded: source.degraded === true || source.cache === 'stale'
        || /(?:stale|degrad)/iu.test(String(attempt.upstream || source.upstream || ''))
        || /(?:stale|degrad)/iu.test(String(attempt.reason || source.reason || '')),
    };
  });
  const quota = object(payload.quota?.selfLookup) ? payload.quota.selfLookup : {};
  const targetQuota = object(payload.quota?.arbitraryLookup) ? payload.quota.arbitraryLookup : {};
  const quotaRecord = (value) => count(value.remaining) !== null && count(value.limit) !== null
    ? { remaining: count(value.remaining), limit: count(value.limit), resetsInSeconds: count(value.resetsInSeconds) } : null;
  const shared = object(payload.sharedUsers) ? payload.sharedUsers : {};
  const evidence = object(payload.evidence) ? payload.evidence : {};
  const breakdown = object(payload.riskBreakdown) ? payload.riskBreakdown : {};
  const zone = object(payload.geo.timezone) ? payload.geo.timezone : {};
  const utcOffset = finite(payload.geo.offset ?? payload.geo.utcOffset ?? zone.offset, -50400, 50400);
  const sourceAttempts = attempts.map((item) => ({ source: text(item.source, 80),
    status: ['success', 'unavailable', 'not_called'].includes(item.status) ? item.status : 'unknown',
    reason: text(item.reason, 200), upstream: text(item.upstream, 80), elapsedMs: finite(item.elapsedMs, 0, 3600000),
  }));
  const blocklist = object(payload.blocklist) ? payload.blocklist : {};
  const isoNow = new Date(Number.isFinite(checkedAt) ? checkedAt : Date.now()).toISOString();
  return {
    schemaVersion: 2, ip, ipVersion: ip.includes(':') ? 6 : 4, isSelfLookup: true, targetIP: '',
    countryCode: /^[a-z]{2}$/iu.test(text(payload.geo.countryCode, 8)) ? text(payload.geo.countryCode, 8).toUpperCase() : '',
    timezone: text(typeof payload.geo.timezone === 'string' ? payload.geo.timezone : zone.id || zone.name, 100),
    utcOffset, utcOffsetLabel: text(payload.geo.utcOffsetLabel || zone.utc, 30),
    latitude: finite(payload.geo.lat ?? payload.geo.latitude, -90, 90), longitude: finite(payload.geo.lon ?? payload.geo.longitude, -180, 180),
    org: text(payload.geo.org || payload.geo.organization), reverse: text(payload.geo.reverse || payload.geo.reverseDns, 253),
    country: text(payload.geo.country), region: text(payload.geo.region), city: text(payload.geo.city),
    asn: Number.isInteger(payload.geo.asn) && payload.geo.asn > 0 && payload.geo.asn <= 4294967295
      ? `AS${payload.geo.asn}` : text(payload.geo.asn, 40), asName: text(payload.geo.asName), isp: text(payload.geo.isp),
    ipType: ['residential', 'hosting', 'mobile', 'business', 'unknown'].includes(payload.ipType) ? payload.ipType : 'unknown',
    usageType: ['isp', 'dual_isp', 'business', 'hosting', 'mobile', 'education', 'government', 'unknown'].includes(payload.usageType) ? payload.usageType : 'unknown',
    nativeType: ['native', 'broadcast', 'unknown'].includes(payload.nativeType) ? payload.nativeType : 'unknown',
    sharedUsers: { range: text(shared.range, 60), quality: ['good', 'ok', 'poor', 'unknown'].includes(shared.quality) ? shared.quality : 'unknown', estimated: true },
    attribution: shortRecord(payload.attribution, ['provider', 'datacenter', 'company', 'asnOrg']),
    rdap: { ...shortRecord(payload.rdap, ['registry', 'country', 'org']), registered: date(payload.rdap?.registered) },
    evidence: { dedicatedVotes: count(evidence.dedicatedVotes), voters: names(evidence.voters), directSources: names(evidence.directSources),
      confidence: ['none', 'single', 'multi'].includes(evidence.confidence) ? evidence.confidence : 'unknown',
      singleSource: text(evidence.singleSource, 80), floors: floorList(evidence.floors) },
    riskBreakdown: { contributors: Array.isArray(breakdown.contributors) ? breakdown.contributors.slice(0, 20).filter(object).map((item) => ({
      source: text(item.source, 80), risk: score(item.risk), weight: finite(item.weight, 0, 100000),
    })).filter((item) => item.source) : [], weightedAvg: score(breakdown.weightedAvg), floors: floorList(breakdown.floors), final: score(breakdown.final) },
    services: snapshots(payload.services), media: snapshots(payload.media),
    sourceAttempts, sourceCounts: { success: sources.filter((item) => item.status === 'success').length,
      unavailable: sources.filter((item) => item.status === 'unavailable').length, notCalled: sources.filter((item) => item.status === 'not_called').length,
      unknown: sources.filter((item) => item.status === 'unknown').length, direct: names(evidence.directSources).length,
      current: currentSources.length, stale: staleSources.length, undated: undatedSources.length },
    degradedSources: sources.filter((item) => item.degraded).map((item) => item.name),
    reasons: names(assessment.reasons),
    risk: availableScore,
    verdict: availableScore === null ? '证据不足' : text(payload.purityBand?.label, 100) || fallbackVerdict(availableScore),
    riskLevel: riskLevel(availableScore), evidenceStatus,
    currentSources, staleSources, undatedSources, sources,
    signals: names(payload.signals), blocklists: names(blocklist.listed),
    blacklistChecked: count(blocklist.checked), blacklistTotal: count(blocklist.total),
    cachedOnly: payload.upstream === 'cached-only',
    fetchedAt: date(payload.fetchedAt) || date(assessment.assessedAt) || isoNow,
    assessedAt: date(assessment.assessedAt), checkedAt: isoNow,
    quota: quotaRecord(quota), targetQuota: quotaRecord(targetQuota),
  };
}

// Reconstruct a whitelisted API-shaped object to validate cached facts using the
// same rules as fresh data. Extra cache properties are discarded, never trusted.
function cachePayload(report) {
  if (!object(report)) return null;
  return {
    geo: { ip: report.ip, country: report.country, region: report.region, city: report.city,
      asn: report.asn, asName: report.asName, isp: report.isp, countryCode: report.countryCode,
      timezone: report.timezone, offset: report.utcOffset, utcOffsetLabel: report.utcOffsetLabel,
      lat: report.latitude, lon: report.longitude, org: report.org, reverse: report.reverse },
    ipType: report.ipType, usageType: report.usageType, nativeType: report.nativeType, sharedUsers: report.sharedUsers,
    attribution: report.attribution, rdap: report.rdap, evidence: report.evidence, riskBreakdown: report.riskBreakdown,
    services: report.services, media: report.media, risk: report.risk,
    riskAssessment: { status: report.evidenceStatus, scoreAvailable: report.risk !== null,
      currentSources: report.currentSources, staleSources: report.staleSources, undatedSources: report.undatedSources,
      assessedAt: report.assessedAt, reasons: report.reasons },
    purityBand: { label: report.verdict },
    sources: Array.isArray(report.sources) ? report.sources.slice(0, 20).filter(object).map((item) => ({
      source: item.name, ok: item.status === 'success' ? true : item.status === 'unavailable' ? false : undefined,
      risk: item.risk, observedAt: item.observedAt, flags: item.flags, degraded: item.degraded, reason: item.reason, upstream: item.upstream,
    })) : [],
    sourceAttempts: Array.isArray(report.sources) ? report.sources.slice(0, 20).filter(object).map((item) => ({ source: item.name, status: item.status, reason: item.reason, upstream: item.upstream, elapsedMs: item.elapsedMs })) : [],
    signals: report.signals, blocklist: { listed: report.blocklists, checked: report.blacklistChecked, total: report.blacklistTotal },
    upstream: report.cachedOnly === true ? 'cached-only' : '', fetchedAt: report.fetchedAt,
    quota: { selfLookup: report.quota, arbitraryLookup: report.targetQuota },
  };
}

export function readNetworkCache(raw, now = Date.now()) {
  if (typeof raw !== 'string' || raw.length > 100000) return null;
  try {
    const cached = JSON.parse(raw);
    if (!object(cached) || ![1, 2].includes(cached.version) || !Number.isFinite(cached.savedAt)
        || cached.savedAt > now || now - cached.savedAt >= NETWORK_CACHE_TTL) return null;
    const report = normalizeNetworkResponse(cachePayload(cached.report), cached.savedAt);
    report.schemaVersion = cached.version;
    return report;
  } catch { return null; }
}

export function retryAfterMilliseconds(value, now = Date.now()) {
  if (typeof value !== 'string' || !value.trim()) return 60000;
  const seconds = Number(value);
  const milliseconds = Number.isFinite(seconds) && seconds > 0 ? seconds * 1000 : Date.parse(value) - now;
  return Number.isFinite(milliseconds) && milliseconds > 0 ? Math.min(86400000, Math.max(1000, milliseconds)) : 60000;
}

export function readNetworkCooldown(raw, now = Date.now()) {
  if (typeof raw !== 'string' || raw.length > 100000) return 0;
  try {
    const cached = JSON.parse(raw);
    return object(cached) && cached.version === 1 && cached.errorKind === 'rate_limited'
      && Number.isFinite(cached.cooldownUntil) && cached.cooldownUntil > now
      && cached.cooldownUntil <= now + 86400000 ? cached.cooldownUntil : 0;
  } catch { return 0; }
}

const errorMessages = {
  timeout: 'IPOK 检测超时，请确认网络连接后重新检测。',
  rate_limited: 'IPOK 检测额度或请求频率已达限制，请稍后重试。',
  unavailable: 'IPOK 检测服务暂时不可用，请稍后重试，或打开原始页面查看。',
  invalid_response: 'IPOK 暂未返回有效的检测资料，请稍后重新检测。',
  network: '无法连接 IPOK，请检查浏览器网络或代理设置后重试。',
  invalid_target: '请输入有效的公网 IPv4 或 IPv6 地址；留空检测当前浏览器出口。',
  verification_required: 'IPOK 暂不允许此次查询，可能需要在来源网站完成验证，或稍后重试。',
};

/** One shared request and a short session cache for all views in the same tab. */
export function createNetworkController({
  fetchImpl, storage, onChange = () => {}, now = Date.now,
  setTimer = setTimeout, clearTimer = clearTimeout, timeoutMs = 20000,
} = {}) {
  let state = { phase: 'idle', report: null, error: '', errorKind: null, cached: false, busy: false, cooldownUntil: 0, targetIP: '', selfReport: null };
  let pending = null;
  let abortController = null;
  let timeoutTimer = null;
  let cooldownTimer = null;
  let started = false;
  let disposed = false;
  const getState = () => ({ ...state, cooldownSeconds: Math.max(0, Math.ceil((state.cooldownUntil - now()) / 1000)) });
  const emit = () => { if (!disposed) onChange(getState()); };
  const removeCache = () => { try { storage?.removeItem(NETWORK_CACHE_KEY); } catch {} };
  function startCooldown() {
    clearTimer(cooldownTimer);
    const tick = () => {
      if (disposed) return;
      emit();
      if (state.cooldownUntil > now()) cooldownTimer = setTimer(tick, 1000);
    };
    if (state.cooldownUntil > now()) cooldownTimer = setTimer(tick, 1000);
  }
  const phaseFor = (report) => report.evidenceStatus === 'current' && !report.cachedOnly && report.risk !== null ? 'ready' : 'limited';

  function refresh(options = {}) {
    const { force = true } = options;
    let targetIP;
    try { targetIP = validateTargetIP(Object.hasOwn(options, 'targetIP') ? options.targetIP : state.targetIP); }
    catch { state = { ...state, phase: 'error', report: null, errorKind: 'invalid_target', error: errorMessages.invalid_target, busy: false }; emit(); return Promise.resolve(getState()); }
    if (disposed) return Promise.resolve(getState());
    if (pending) return pending;
    if (state.cooldownUntil > now()) { emit(); return Promise.resolve(getState()); }
    let cacheText, cooldownText;
    try {
      cacheText = storage?.getItem(NETWORK_CACHE_KEY);
      cooldownText = storage?.getItem(NETWORK_COOLDOWN_KEY);
    } catch {}
    if (!started) {
      const cooldownUntil = readNetworkCooldown(cooldownText, now()) || readNetworkCooldown(cacheText, now());
      if (cooldownUntil) {
        started = true;
        state = { ...state, phase: 'error', errorKind: 'rate_limited', error: errorMessages.rate_limited, cooldownUntil };
        emit();
        startCooldown();
        return Promise.resolve(getState());
      }
    }
    if (!force && started && targetIP === state.targetIP) return Promise.resolve(getState());
    started = true;
    if (!force && !targetIP) {
      let cached = null;
      try { cached = readNetworkCache(cacheText, now()); } catch {}
      if (cached) {
        state = { ...state, phase: phaseFor(cached), report: cached, selfReport: cached, targetIP: '', cached: true, error: '', errorKind: null };
        emit();
        return Promise.resolve(getState());
      }
    }
    if (!targetIP) removeCache();
    state = { ...state, phase: 'loading', report: null, targetIP, selfReport: targetIP ? state.selfReport : null, cached: false, busy: true, error: '', errorKind: null, cooldownUntil: 0 };
    emit();
    abortController = new AbortController();
    const requestSignal = abortController.signal;
    const request = async () => {
      let response;
      try {
        const url = targetIP ? `${NETWORK_API_URL}?${new URLSearchParams({ ip: targetIP })}` : NETWORK_API_URL;
        response = await fetchImpl(url, {
          method: 'GET', mode: 'cors', credentials: 'omit', cache: 'no-store', referrerPolicy: 'no-referrer', signal: requestSignal,
        });
      } catch { throw new Error('network'); }
      if (response.status === 429) {
        const error = new Error('rate_limited');
        error.retryMs = retryAfterMilliseconds(response.headers?.get('Retry-After'), now());
        throw error;
      }
      if (response.status === 403) throw new Error('verification_required');
      if (!response.ok) throw new Error('unavailable');
      try {
        const body = await response.text();
        if (body.length > 1000000) throw new Error('invalid_response');
        const report = normalizeNetworkResponse(JSON.parse(body), now());
        if (targetIP && normalizeIPAddress(report.ip) !== targetIP) throw new Error('invalid_response');
        report.isSelfLookup = !targetIP; report.targetIP = targetIP;
        if (targetIP) report.quota = report.targetQuota;
        return report;
      } catch { throw new Error('invalid_response'); }
    };
    const timeout = new Promise((_resolve, reject) => {
      timeoutTimer = setTimer(() => {
        reject(new Error('timeout'));
        abortController?.abort();
      }, timeoutMs);
    });
    pending = (async () => {
      try {
        const report = await Promise.race([request(), timeout]);
        if (!disposed) {
          state = { ...state, phase: phaseFor(report), report, selfReport: targetIP ? state.selfReport : report, error: '', errorKind: null };
          if (!targetIP) { try { storage?.setItem(NETWORK_CACHE_KEY, JSON.stringify({ version: 2, savedAt: now(), report })); } catch {} }
        }
      } catch (error) {
        if (!disposed) {
          const kind = Object.hasOwn(errorMessages, error?.message) ? error.message : 'network';
          state = { ...state, phase: 'error', report: null, errorKind: kind, error: errorMessages[kind],
            cooldownUntil: kind === 'rate_limited' ? now() + error.retryMs : 0 };
          if (kind === 'rate_limited') {
            try { storage?.setItem(NETWORK_COOLDOWN_KEY, JSON.stringify({ version: 1, errorKind: kind, cooldownUntil: state.cooldownUntil })); } catch {}
          } else if (!targetIP) removeCache();
        }
      } finally {
        clearTimer(timeoutTimer);
        timeoutTimer = null;
        abortController = null;
        pending = null;
        state = { ...state, busy: false };
        emit();
        startCooldown();
      }
      return getState();
    })();
    return pending;
  }

  return {
    getState, refresh,
    start: () => refresh({ force: false, targetIP: '' }),
    query: (targetIP) => refresh({ targetIP }),
    dispose() {
      disposed = true;
      abortController?.abort();
      clearTimer(timeoutTimer);
      clearTimer(cooldownTimer);
    },
  };
}
