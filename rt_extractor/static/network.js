import { createNetworkController, validateTargetIP, ipToDecimal } from './network-core.mjs';

const byId = (id) => document.getElementById(id);
const write = (id, value) => { const element = byId(id); if (element) element.textContent = value; };
const show = (id, visible) => { const element = byId(id); if (element) element.hidden = !visible; };
const formatDate = (value) => {
  if (!value) return '时间未知';
  const parsed = new Date(value);
  return Number.isFinite(parsed.getTime()) ? parsed.toLocaleString('zh-CN', { hour12: false }) : '时间未知';
};
const typeLabels = { residential: '住宅宽带', hosting: '机房 / 托管网络', mobile: '移动网络', business: '企业网络', unknown: '未知' };
const usageLabels = { isp: '运营商宽带', dual_isp: '多运营商网络', business: '企业网络', hosting: '机房 / 托管网络', mobile: '移动网络', education: '教育网络', government: '政务网络', unknown: '用途未知' };
const signalLabels = { hosting: '数据中心 / 机房 IP', datacenter: '数据中心', proxy: '代理', vpn: 'VPN', tor: 'Tor', abuser: '滥用记录', abuse: '滥用记录', relay: '中继', mobile: '移动网络', residential: '住宅网络', bogon: '保留地址', spam: '垃圾邮件标记', recent_abuse: '近期滥用标记', whitelisted: '来源白名单' };
const sourceLabels = { success: '已返回', unavailable: '暂不可用', not_called: '未调用', unknown: '状态未知' };
const reasonLabels = { dormant: '来源休眠', quota: '来源额度限制', cached_only: '仅使用缓存', cache_miss: '暂无缓存', upstream_error: '来源请求失败', not_configured: '来源未配置', no_evidence: '暂无证据', stale: '过期资料' };
const serviceNames = { chatgpt: 'ChatGPT', openai: 'OpenAI', claude: 'Claude', gemini: 'Gemini', tiktok: 'TikTok', netflix: 'Netflix', disney: 'Disney+', disneyplus: 'Disney+', youtube: 'YouTube', spotify: 'Spotify' };
const refreshIds = ['network-summary-refresh', 'network-refresh', 'network-recheck', 'network-self-button', 'network-query-button'];
const circumference = 2 * Math.PI * 76;
let currentReport = null;
let detailReport = undefined;
let numericVisible = false;

function append(parent, tag, value, className = '') {
  const child = document.createElement(tag);
  if (value !== undefined) child.textContent = value;
  if (className) child.className = className;
  parent.append(child);
  return child;
}
function detail(parent, title, value, className = 'ip-source-row') {
  const row = append(parent, 'div', undefined, className);
  append(row, 'dt', title);
  return append(row, 'dd', value);
}
function bandFor(score) { return score === null ? 'unknown' : score < 15 ? 'low' : score < 50 ? 'guarded' : score < 70 ? 'elevated' : 'high'; }
function offsetLabel(report) {
  if (report.utcOffsetLabel) return report.utcOffsetLabel;
  if (!Number.isFinite(report.utcOffset)) return '';
  const hours = Math.floor(Math.abs(report.utcOffset) / 3600);
  const minutes = Math.floor(Math.abs(report.utcOffset) % 3600 / 60);
  return `UTC${report.utcOffset < 0 ? '−' : '+'}${hours}${minutes ? `:${String(minutes).padStart(2, '0')}` : ''}`;
}
function countryLabel(report) {
  const code = report.countryCode || '';
  const flag = /^[A-Z]{2}$/u.test(code) ? String.fromCodePoint(...[...code].map((letter) => letter.charCodeAt(0) + 127397)) : '';
  return [flag, report.country, code].filter(Boolean).join(' ') || '未知';
}
function renderNumeric() {
  const value = currentReport ? ipToDecimal(currentReport.ip) : '';
  write('network-ip-numeric', numericVisible && value ? value : '••••••••');
  const toggle = byId('network-toggle-numeric');
  if (toggle) {
    toggle.disabled = !value;
    toggle.setAttribute('aria-pressed', String(numericVisible && Boolean(value)));
    toggle.setAttribute('aria-label', numericVisible ? '隐藏 IP 数值' : '显示 IP 数值');
  }
}
function renderGauge(report) {
  const score = report?.risk ?? null;
  const gauge = byId('network-gauge');
  if (gauge) {
    gauge.dataset.band = bandFor(score);
    gauge.setAttribute('aria-label', score === null ? '当前风险评分不可用，证据不足' : `IPOK 风险值 ${score} / 100，${report.verdict}`);
  }
  byId('network-gauge-arc')?.setAttribute('stroke-dasharray', `${score === null ? 0 : (circumference * score / 100).toFixed(3)} ${circumference.toFixed(3)}`);
  write('network-score', score === null ? '—' : String(score));
  write('network-verdict', report?.verdict || '等待检测');
}
function renderRiskSignals(report) {
  const container = byId('network-risk');
  if (!container) return;
  container.replaceChildren();
  if (!report) { container.textContent = '待检测'; return; }
  for (const signal of report.signals || []) append(container, 'span', signalLabels[signal] || signal, 'ip-signal-chip');
  for (const name of report.blocklists || []) append(container, 'span', `黑名单：${name}`, 'ip-signal-chip');
  if (!container.children.length) container.textContent = '未返回全局风险信号，不代表无风险。';
}
function renderDetails(report) {
  if (report === detailReport) return;
  detailReport = report;
  const sources = byId('network-details');
  if (sources) {
    sources.replaceChildren();
    if (!report) detail(sources, '检测资料', '等待 IPOK 返回逐源证据。');
    else {
      for (const source of report.sources || []) {
        const pieces = [sourceLabels[source.status] || '状态未知'];
        if (source.risk !== null && source.risk !== undefined) pieces.push(`来源风险 ${source.risk} / 100`);
        if (source.freshness === 'stale') pieces.push('资料已过期');
        else if (source.freshness === 'undated') pieces.push('资料时间未知');
        if (source.degraded) pieces.push('故障降级资料');
        if (source.observedAt) pieces.push(formatDate(source.observedAt));
        const cell = detail(sources, source.name, undefined);
        append(cell, 'p', pieces.join(' · '));
        if (source.reason || source.upstream) append(cell, 'p', [reasonLabels[source.reason] || source.reason, source.upstream].filter(Boolean).join(' · '), 'network-secondary');
        const flags = Object.entries(source.flags || {});
        if (flags.length) {
          const list = append(cell, 'div', undefined, 'network-tags');
          for (const [key, value] of flags) {
            const chip = append(list, 'span', `${value ? '' : '未标记：'}${signalLabels[key] || key}`, 'ip-signal-chip');
            chip.dataset.positive = String(value);
          }
        }
      }
      if (!(report.sources || []).length) detail(sources, '逐源资料', '此次未返回逐源资料。');
      detail(sources, '证据覆盖', `当前 ${report.currentSources?.length || 0} 个 · 过期 ${report.staleSources?.length || 0} 个 · 时间未知 ${report.undatedSources?.length || 0} 个`);
      const evidence = report.evidence || {};
      detail(sources, '直接风险证据', (evidence.directSources || []).join('、') || '未返回直接标记来源；不代表不存在风险。');
      const confidence = { none: '无明确置信资料', single: '单来源证据', multi: '多来源证据', unknown: '来源未提供置信度' };
      detail(sources, '证据强度', confidence[evidence.confidence] || '来源未提供置信度');
      const rdap = report.rdap || {};
      if (rdap.registry || rdap.org || rdap.country || rdap.registered) detail(sources, '注册资料', [rdap.registry, rdap.country, rdap.org, rdap.registered ? formatDate(rdap.registered) : ''].filter(Boolean).join(' · '));
    }
  }
  renderBreakdown(report);
  renderSnapshots('network-services', report?.services || []);
  renderSnapshots('network-media', report?.media || []);
}
function renderBreakdown(report) {
  const container = byId('network-score-breakdown');
  if (!container) return;
  container.replaceChildren();
  const breakdown = report?.riskBreakdown || {};
  if (!report || (!(breakdown.contributors || []).length && !(breakdown.floors || []).length)) {
    append(container, 'p', '来源未提供评分明细；不会自行补齐权重或计算总分。', 'network-secondary'); return;
  }
  if (report.risk === null) append(container, 'p', '当前总分不可用。下方仅保留来源逐项证据与历史评分资料。', 'network-secondary');
  for (const contributor of breakdown.contributors || []) {
    const row = append(container, 'div', undefined, 'ip-breakdown-row');
    append(row, 'span', contributor.source);
    append(row, 'span', [contributor.risk !== null ? `来源风险 ${contributor.risk} / 100` : '来源分数未知', Number.isFinite(contributor.weight) ? `来源权重 ${contributor.weight}` : '权重未知'].join(' · '));
  }
  for (const floor of breakdown.floors || []) {
    const row = append(container, 'div', undefined, 'ip-breakdown-row');
    append(row, 'span', `风险下限 · ${signalLabels[floor.key] || floor.key}`);
    append(row, 'span', [floor.floor !== null ? String(floor.floor) : '未知', floor.reason].filter(Boolean).join(' · '));
  }
  if (report.risk !== null) {
    if (Number.isFinite(breakdown.weightedAvg)) {
      const weighted = append(container, 'div', undefined, 'ip-breakdown-row');
      append(weighted, 'span', '来源加权值'); append(weighted, 'span', String(breakdown.weightedAvg));
    }
    const row = append(container, 'div', undefined, 'ip-breakdown-row');
    append(row, 'strong', 'IPOK 当前总分'); append(row, 'strong', `${report.risk} / 100`);
  }
}
function renderSnapshots(id, snapshots) {
  const container = byId(id);
  if (!container) return;
  container.replaceChildren();
  if (!snapshots.length) { append(container, 'p', '来源未提供地区资料快照。', 'network-secondary'); return; }
  const statuses = { available: '地区资料：可用', unavailable: '地区资料：不可用', blocked: '地区资料：受限', restricted: '地区资料：受限', unknown: '地区资料：未知' };
  for (const item of snapshots) {
    const row = append(container, 'div', undefined, 'ip-service-item');
    row.dataset.status = item.status || 'unknown';
    append(row, 'strong', item.name || serviceNames[item.key] || item.key);
    append(row, 'span', statuses[item.status] || `地区资料：${item.status || '未知'}`);
    const note = [item.reason || item.note, item.country || item.region, item.checkedAt ? `来源记录 ${formatDate(item.checkedAt)}` : ''].filter(Boolean);
    append(row, 'small', [...note, '地区资料快照，非实时解锁测试'].join(' · '));
  }
}
function renderProfile(report, phase) {
  write('network-type', report ? typeLabels[report.ipType] || '未知' : '待检测');
  write('network-usage-description', report ? `用途标签：${usageLabels[report.usageType] || report.usageType || '未知'}。网络用途不是用户身份或平台信任度。` : '等待来源返回 IP 类型与用途资料。');
  const count = report?.sourceCounts || {};
  write('network-source-counts', report ? `${count.success || 0} 成功 · ${count.unavailable || 0} 暂缺 · ${count.notCalled || 0} 未调用 · ${count.unknown || 0} 状态未知 · 直接证据 ${count.direct || 0} 个来源` : '等待各检测来源返回');
  const attribution = report?.attribution || {};
  write('network-attribution', `出口归属：${[attribution.provider, attribution.datacenter, attribution.company, attribution.asnOrg].filter(Boolean).join(' · ') || (report ? '来源未提供' : '待检测')}`);
  const shared = report?.sharedUsers || {};
  write('network-shared-users', shared.range || '未知');
  write('network-shared-note', shared.range ? '来源按 IP 用途估计的共享规模，非实测人数；仅作为共享拓扑背景，不是平台判断。' : '来源未提供共享规模估计，不能从当前风险值推算人数。');
  const meter = byId('network-shared-marker');
  if (meter) {
    const value = { good: 1, ok: 2, poor: 3 }[shared.quality];
    meter.hidden = !value || !shared.range;
    meter.min = 0; meter.max = 3; meter.low = 1.5; meter.high = 2.5; meter.optimum = 1;
    if (value) meter.value = value;
    meter.setAttribute('aria-label', `来源估计的共享背景：${{ good: '较少', ok: '一般', poor: '较多' }[shared.quality] || '未知'}，非实测人数`);
  }
  const native = report?.nativeType || 'unknown';
  write('network-native-type', { native: '注册与定位国家一致', broadcast: '注册与定位国家不一致', unknown: '未知' }[native] || '未知');
  write('network-native-note', native === 'unknown' ? '来源未提供明确国家一致性资料，暂无法判断；这不是原生出口证明。' : '这是来源对注册地与定位国家一致性的标签，不能证明原生出口或物理位置。');
  renderRiskSignals(report);
  const banner = byId('network-conclusion');
  if (banner) {
    if (!report) { banner.dataset.tone = 'neutral'; banner.textContent = phase === 'error' ? '检测尚未完成，请重试或查看 IPOK 原始页面。' : '正在获取当前 IP 的类型与风险证据…'; }
    else if (report.risk === null) { banner.dataset.tone = 'warning'; banner.textContent = '当前有效证据不足，暂不展示风险总分。请结合逐源状态、标记与资料日期查看。'; }
    else {
      banner.dataset.tone = report.risk >= 70 ? 'danger' : report.evidenceStatus !== 'current' || report.cachedOnly || report.degradedSources?.length ? 'warning' : 'positive';
      banner.textContent = `${typeLabels[report.ipType] || '类型未知'} · ${report.verdict}。${report.evidenceStatus !== 'current' || report.cachedOnly ? '部分证据有限或仅来自缓存，当前结果仅供参考。' : '结果描述本次返回的 IP 层证据。'}账号、设备、行为与平台策略仍需另行判断。`;
    }
  }
  const degraded = report?.degradedSources || [];
  show('network-degraded', Boolean(report && (report.cachedOnly || degraded.length)));
  write('network-degraded-details', report ? [degraded.length ? `${degraded.length} 个来源明确标记为故障降级：${degraded.join('、')}。` : '', report.cachedOnly ? '部分商业来源仅提供缓存；缓存资料不等于本次实时回源。' : '', '逐源时间与状态见下方证据表。'].filter(Boolean).join(' ') : '');
  show('network-evidence-warning', Boolean(report && (report.risk === null || report.evidenceStatus !== 'current' || report.cachedOnly || degraded.length)));
}
function renderGeo(report) {
  const unknown = '未知';
  write('network-location', report ? [report.country, report.region, report.city].filter(Boolean).join(' / ') || unknown : '待检测');
  write('network-country', report ? countryLabel(report) : '—');
  write('network-region', report?.region || unknown); write('network-city', report?.city || unknown);
  write('network-timezone', report ? [report.timezone, offsetLabel(report)].filter(Boolean).join(' · ') || unknown : unknown);
  write('network-coordinates', Number.isFinite(report?.latitude) && Number.isFinite(report?.longitude) ? `${report.latitude.toFixed(4)}, ${report.longitude.toFixed(4)}` : unknown);
  write('network-isp', report?.isp || unknown); write('network-org', report?.org || report?.asName || unknown);
  write('network-asn', report?.asn || unknown); write('network-reverse', report?.reverse || unknown);
}
function render(state) {
  const { report, phase, cooldownSeconds = 0, busy } = state;
  const selfReport = state.selfReport || (report?.isSelfLookup !== false ? report : null);
  const isTarget = Boolean(state.targetIP || report?.targetIP || report?.isSelfLookup === false);
  if (currentReport?.ip !== report?.ip) { numericVisible = false; write('network-copy-status', ''); }
  currentReport = report;
  document.documentElement.dataset.networkState = phase === 'idle' ? 'loading' : phase;
  document.documentElement.dataset.riskLevel = selfReport?.riskLevel || 'unknown';
  let summary = phase === 'error' && !selfReport ? (cooldownSeconds ? `检测受限 · ${cooldownSeconds} 秒后重试` : '检测未完成 · 可重试') : '正在检测浏览器出口…';
  let status = phase === 'error' ? state.error : '正在获取检测资料…';
  if (selfReport) summary = [selfReport.evidenceStatus === 'current' ? '当前证据' : selfReport.evidenceStatus === 'limited' ? '证据有限' : '证据不足', selfReport.cachedOnly ? 'IPOK 缓存来源' : '', state.cached && !isTarget ? '10 分钟内本页缓存' : ''].filter(Boolean).join(' · ');
  if (report) status = `检测完成 · ${report.isSelfLookup === false ? '指定 IP' : '浏览器出口'} · ${report.evidenceStatus === 'current' ? '当前证据' : report.evidenceStatus === 'limited' ? '证据有限' : '证据不足'}${report.cachedOnly ? ' · IPOK 缓存来源' : ''}${state.cached ? ' · 10 分钟内本页缓存' : ''}${report.schemaVersion === 1 ? ' · 旧版缓存，重新检测可补充更多字段' : ''}`;
  write('network-summary-status', summary); write('network-status', status);
  write('network-summary-ip', selfReport?.ip || '—');
  write('network-summary-score', selfReport?.risk !== null && selfReport ? `${selfReport.risk} / 100` : '—');
  if (byId('network-summary-score')) byId('network-summary-score').dataset.band = bandFor(selfReport?.risk ?? null);
  write('network-summary-label', selfReport?.verdict || (phase === 'error' ? '暂未获得检测结果' : '等待检测结果'));
  write('network-ip-label', isTarget ? '当前查询 IP' : '你的公网出口 IP');
  write('network-ip', report?.ip || '—'); write('network-ip-version', report ? `IPv${report.ipVersion}` : 'IP');
  write('network-query-context', isTarget ? `正在查看指定地址；浏览器当前出口为 ${selfReport?.ip || '待检测'}。` : '留空检测当前浏览器出口；示例只填入地址，点击查询后才会请求。');
  renderGauge(report); renderProfile(report, phase); renderGeo(report); renderDetails(report); renderNumeric();
  write('network-updated', report ? formatDate(report.fetchedAt) : '尚未完成检测');
  const time = byId('network-updated'); if (time) { if (report) time.setAttribute('datetime', report.fetchedAt); else time.removeAttribute('datetime'); }
  write('network-cached-at', report ? `${state.cached ? '会话缓存于' : '本次检测于'} ${formatDate(report.checkedAt)}` : '暂无缓存');
  write('network-evidence', report ? `${report.currentSources?.length || 0} 个当前来源 · ${report.evidenceStatus === 'current' ? '证据可用' : '证据有限或不足'}` : '等待来源证据');
  show('network-evidence', Boolean(report));
  const quota = report?.isSelfLookup === false ? report.targetQuota : report?.quota;
  write('network-quota', quota ? `本日${isTarget ? '指定 IP 查询' : '自查'}剩余 ${quota.remaining} / ${quota.limit} 次（按来源 IP，UTC 日重置）` : '来源未提供剩余额度');
  write('network-error', phase === 'error' ? `${state.error}${cooldownSeconds ? ` ${cooldownSeconds} 秒后可重试。` : ''}` : ''); show('network-error', phase === 'error');
  for (const id of refreshIds) {
    const button = byId(id); if (!button) continue;
    button.disabled = busy || cooldownSeconds > 0;
    const label = busy ? '正在检测…' : cooldownSeconds > 0 ? `${cooldownSeconds} 秒后重试` : id === 'network-query-button' ? '查询' : id === 'network-self-button' ? '检测当前出口' : '重新检测';
    button.setAttribute('aria-label', label); button.title = label;
    if (['network-refresh', 'network-recheck', 'network-query-button'].includes(id)) button.textContent = label;
  }
  const input = byId('network-query-input'); if (input) input.disabled = Boolean(busy);
  const copy = byId('network-copy-ip'); if (copy) copy.disabled = !report?.ip || Boolean(busy);
}

let storage;
try { storage = window.sessionStorage; } catch { /* Detection works without session storage. */ }
const newController = () => createNetworkController({ fetchImpl: window.fetch.bind(window), storage, onChange: render });
let controller = newController();
function clearQueryFeedback() { write('network-query-feedback', ''); show('network-query-feedback', false); }
byId('network-summary-refresh')?.addEventListener('click', () => { clearQueryFeedback(); return controller.refresh({ targetIP: '' }); });
for (const id of ['network-refresh', 'network-recheck']) byId(id)?.addEventListener('click', () => { clearQueryFeedback(); return controller.refresh(); });
byId('network-self-button')?.addEventListener('click', () => { clearQueryFeedback(); if (byId('network-query-input')) byId('network-query-input').value = ''; return controller.refresh({ targetIP: '' }); });
byId('network-query-input')?.addEventListener('input', clearQueryFeedback);
byId('network-query-form')?.addEventListener('submit', async (event) => {
  event.preventDefault(); clearQueryFeedback();
  let targetIP;
  try { targetIP = validateTargetIP(byId('network-query-input').value); }
  catch { write('network-query-feedback', '请输入有效的公网 IPv4 或 IPv6 地址；留空检测当前浏览器出口。'); show('network-query-feedback', true); return; }
  return controller.query(targetIP);
});
for (const button of document.querySelectorAll('[data-network-example]')) button.addEventListener('click', () => { if (byId('network-query-input')) byId('network-query-input').value = button.dataset.networkExample; clearQueryFeedback(); });
byId('network-copy-ip')?.addEventListener('click', async () => {
  if (!currentReport?.ip) return;
  try { await navigator.clipboard.writeText(currentReport.ip); write('network-copy-status', '已复制 IP 地址'); }
  catch { write('network-copy-status', '复制未完成，可手动选择上方 IP 地址。'); }
});
byId('network-toggle-numeric')?.addEventListener('click', () => { numericVisible = !numericVisible; renderNumeric(); });
window.addEventListener('pagehide', () => controller.dispose());
window.addEventListener('pageshow', (event) => { if (event.persisted) { controller = newController(); void controller.start(); } });
void controller.start();
