import { activeTask, numberText as num, percentText as pct, timeText, statusText, requestCount, modelIds, svgDataURL, filtersFrom } from './eval-ui.mjs';

const $ = (id) => document.getElementById(id);
const csrf = document.querySelector('meta[name="rt-csrf"]').content;
const state = { questions: [], tasks: [], samples: [], page: 1, total: 0, filters: {}, selected: new Map(), pending: false, active: false, view: 'run', signature: '', review: null, blind: null, cooldown: 0 };
const rendering = new Set();
let pollTimer;
let refreshVersion = 0;
function el(tag, text, cls) { const node = document.createElement(tag); if (text != null) node.textContent = text; if (cls) node.className = cls; return node; }
function button(text, action, cls = 'button button-secondary') { const node = el('button', text, cls); node.type = 'button'; node.addEventListener('click', action); return node; }
function error(error) { const text = error.message || String(error); $('eval-error').textContent = text; $('eval-error').hidden = false; if ($('review-dialog').open) $('review-status').textContent = text; }
function message(text) { $('eval-message').textContent = text; }
async function api(path, options = {}) {
  const response = await fetch(`/api/pelican${path}`, { credentials: 'same-origin', cache: 'no-store', ...options, headers: { 'X-RT-CSRF': csrf, 'Content-Type': 'application/json' }, body: options.body === undefined ? undefined : JSON.stringify(options.body) });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    if (response.status === 429) state.cooldown = Date.now() + Number(response.headers.get('Retry-After') || payload.retry_after || 60) * 1000;
    throw new Error(payload.error || `请求失败 HTTP ${response.status}`);
  }
  return response.json();
}
async function act(callback) { $('eval-error').hidden = true; try { return await callback(); } catch (e) { error(e); } }
function query(extra = {}) { return new URLSearchParams({ ...state.filters, ...extra }).toString(); }
function connection(prefix = 'eval') {
  return { base_url: $(`${prefix}-url`).value.trim(), api_key: $(`${prefix}-key`).value,
    protocol: $(`${prefix}-protocol`).value, reasoning_effort: $(`${prefix}-effort`).value,
    max_output_tokens: Number($(`${prefix}-tokens`).value), timeout_seconds: Number($(`${prefix}-timeout`).value) };
}
function plan() {
  return { ...connection(), models: modelIds($('eval-models').value), questions: [...$('eval-question').selectedOptions].map(o => o.value),
    scope: $('eval-scope').value, repeats: Number($('eval-repeats').value),
    interval_seconds: Number($('eval-interval').value), max_rounds: Number($('eval-rounds').value) };
}
function updatePlan() {
  const config = plan();
  $('eval-repeats').disabled = config.scope !== 'batch';
  $('eval-schedule').hidden = $('eval-mode').value !== 'scheduled';
  const count = requestCount(config.questions.length, config.models.length, config.scope, config.repeats);
  const rounds = $('eval-mode').value === 'scheduled' ? config.max_rounds : 1;
  $('eval-estimate').textContent = `一轮 = ${config.scope === 'simple' ? '单题、单模型一次请求' : '完整批量计划'}，${count} 次请求。${rounds ? `计划共 ${rounds * count} 次。` : '定时计划不限轮数。'}持续循环总数不限。`;
  $('eval-once').disabled = config.questions.length !== 1 || config.models.length !== 1 || state.active || state.pending;
  $('eval-fields').disabled = state.active || state.pending;
  $('eval-start').disabled = state.active || state.pending;
  $('eval-continuous').disabled = state.active || state.pending;
  const questions = state.questions.filter(q => config.questions.includes(q.id));
  $('eval-prompt').textContent = questions.map(q => q.prompt).join('\n\n');
  $('eval-baseline').textContent = questions.map(q => q.baseline).join('\n');
  $('eval-source').replaceChildren();
  questions.forEach(q => {
    const link = el('a', `题面来源 · v${q.version} / 规则 v${q.rubricVersion}`, 'text-button'); link.href = q.source; link.target = '_blank'; link.rel = 'noopener noreferrer'; $('eval-source').append(link);
  });
}
async function start(mode) {
  if (!$('eval-form').reportValidity()) return;
  if (state.pending || state.active) return;
  const data = plan();
  data.mode = mode;
  if (mode === 'once') { data.scope = 'simple'; data.repeats = 1; }
  // A simple single-round plan remains exactly one independent request.
  if (mode === 'batch' && data.scope === 'simple') data.mode = 'once';
  state.pending = true; updatePlan();
  try {
    await api('/tasks', { method: 'POST', body: data });
    $('eval-key').value = ''; // The running server task holds the only remaining credential.
    message('任务已在服务端开始。密钥已从输入框清空；所有成功、失败和取消样本都会保留。');
    state.page = 1; state.signature = ''; await refresh();
  } finally { state.pending = false; updatePlan(); }
}
function renderTasks() {
  const target = $('eval-tasks'); target.replaceChildren();
  const active = state.tasks.filter(activeTask);
  state.active = active.length > 0;
  const shown = [...active, ...state.tasks.filter(t => !activeTask(t)).slice(0, 3)];
  if (!shown.length) target.append(el('p', '暂无后台评测任务。填写连接后，可以单次验证或开始持续循环。', 'field-help'));
  shown.forEach(task => {
    const item = el('div', null, 'eval-task'); item.dataset.active = String(activeTask(task));
    const info = el('div'); info.append(el('strong', `${statusText(task.status)} · 第 ${task.round} 轮`), el('p', `${task.id.slice(0, 8)} · 已结束 ${task.completed_requests} 次 · 每轮 ${task.config.requests_per_round} 次 · ${task.config.scope === 'simple' ? '简单' : '批量'} / ${task.config.mode} · 并发 1`));
    if (task.next_run_at) info.append(el('p', `下一轮：${timeText(task.next_run_at)}`));
    if (task.error) info.append(el('p', task.error, 'eval-error-text'));
    if (task.unsaved_sample) info.append(button('下载未保存样本', () => saveBlob(new Blob([JSON.stringify(task.unsaved_sample, null, 2)], { type: 'application/json' }), 'pelican-unsaved.json'), 'text-button'));
    item.append(info);
    if (activeTask(task)) { const stop = button('停止任务', () => act(async () => { stop.disabled = true; try { await api(`/tasks/${task.id}/stop`, { method: 'POST', body: {} }); await refresh(); } finally { stop.disabled = false; } })); stop.disabled = task.status === 'stopping'; item.append(stop); }
    target.append(item);
  });
  const batchSelect = $('filter-batch'), previous = batchSelect.value;
  batchSelect.replaceChildren(new Option('全部批次', ''));
  state.tasks.forEach(t => batchSelect.add(new Option(`${t.id.slice(0, 8)} · ${timeText(t.started_at)}`, t.id)));
  batchSelect.value = previous;
  document.dispatchEvent(new CustomEvent('pelican:tasks', { detail: state.tasks }));
  updatePlan();
}
async function persistRender(sample, image, success) {
  if (sample.technical.browser_rendered != null || sample.status === 'running' || rendering.has(sample.id)) return;
  rendering.add(sample.id);
  try {
    const data = { success, svg_sha256: sample.svg_sha256 };
    if (success) {
      const w = image.naturalWidth, h = image.naturalHeight;
      if (!w || !h) throw new Error('图片没有有效尺寸');
      const ratio = Math.min(1200 / w, 1200 / h, 1);
      const canvas = document.createElement('canvas'); canvas.width = Math.max(1, Math.round(w * ratio)); canvas.height = Math.max(1, Math.round(h * ratio));
      const ctx = canvas.getContext('2d'); ctx.drawImage(image, 0, 0, canvas.width, canvas.height);
      data.png = canvas.toDataURL('image/png');
    } else data.error = '浏览器 img 解码失败（未修复原始 SVG）';
    const result = await api(`/samples/${sample.id}/render`, { method: 'POST', body: data });
    Object.assign(sample, result.sample);
    state.signature = '';
    // Buttons become available without waiting for another poll.
    renderSamples();
  } catch (e) { error(new Error(`保存浏览器渲染证据失败：${e.message}`)); }
  finally { rendering.delete(sample.id); }
}
function preview(sample, persist = true) {
  const box = el('div', null, 'eval-preview');
  if (sample.svg && sample.technical.safe) {
    const img = el('img'); img.alt = '被测模型生成的原始 SVG';
    if (persist && sample.technical.browser_rendered == null) {
      img.addEventListener('load', () => persistRender(sample, img, true), { once: true });
      img.addEventListener('error', () => persistRender(sample, img, false), { once: true });
    }
    img.src = svgDataURL(sample.svg); box.append(img);
  } else box.append(el('p', sample.status === 'running' ? '生成中，等待完整输出…' : sample.technical.issues.join('；') || '没有可安全预览的 SVG'));
  return box;
}
function checks(sample) {
  const group = el('div', null, 'eval-checks');
  for (const [label, val] of [['请求', sample.status === 'success' ? true : sample.status === 'running' ? null : false], ['完整 SVG', sample.technical.complete_svg], ['结构', sample.technical.structure_valid], ['安全', sample.technical.safe], ['浏览器渲染', sample.technical.browser_rendered]]) group.append(el('span', `${label}：${val == null ? '未检查' : val ? '通过' : '未通过'}`, val == null ? '' : val ? 'pass' : 'fail'));
  return group;
}
function scoreLabel(sample) {
  const s = sample.score;
  if (s.total == null && sample.status !== 'running' && (sample.status !== 'success' || !sample.technical.safe)) return '不可视觉评分 · 请求或 SVG 未通过';
  return s.total == null ? '待评分 · — / 20' : `${num(s.total)} / 20 · ${s.count} 名评审${s.count === 1 ? '（仅单评）' : s.double_blind ? '（两名人工盲评）' : ''}${s.needs_recheck ? ' · 有分歧待复核' : ''}`;
}
function sampleCard(sample, selectable = true) {
  const card = el('article', null, 'eval-sample'), header = el('div', null, 'eval-sample-header');
  header.append(el('h3', sample.requested_model));
  if (selectable) {
    const label = el('label', null, 'eval-check'), check = el('input'); check.type = 'checkbox'; check.checked = state.selected.has(sample.id); check.setAttribute('aria-label', `比较样本 ${sample.id.slice(0, 8)}`);
    check.addEventListener('change', () => { check.checked ? state.selected.set(sample.id, sample) : state.selected.delete(sample.id); updateCompare(); });
    label.append(check, document.createTextNode('比较')); header.append(label);
  }
  card.append(header, preview(sample));
  const body = el('div', null, 'eval-sample-body');
  body.append(el('p', `${sample.batch_id.slice(0, 8)} / 第 ${sample.round} 轮 · ${statusText(sample.status)} · SVG ${sample.technical.svg_count} 个`, 'field-help'), checks(sample), el('p', scoreLabel(sample), 'eval-score'));
  body.append(el('p', `耗时 ${sample.duration_ms == null ? '未知' : num(sample.duration_ms / 1000) + ' s'} · 首字 ${sample.ttft_ms == null ? '未知' : num(sample.ttft_ms / 1000) + ' s'} · Token ${num(sample.usage?.total)}`, 'field-help'));
  if (sample.error) body.append(el('p', sample.error, 'eval-error-text'));
  const actions = el('div', null, 'eval-actions');
  actions.append(button('详情 / 原始输出', () => act(() => openDetail(sample.id))));
  const review = button('看图评分', () => act(() => openReview(sample))); review.disabled = sample.status !== 'success' || !sample.render_evidence; actions.append(review);
  if (sample.svg) actions.append(button('SVG ↓', () => act(() => download(`/samples/${sample.id}/svg`, `pelican-${sample.id}.svg`)), 'text-button'));
  body.append(actions); card.append(body); return card;
}
function renderSamples() {
  const area = $('eval-samples'); area.replaceChildren();
  if (!state.samples.length) area.append(el('p', state.total ? '此页没有样本，请返回上一页。' : '没有符合条件的样本。开始测试后，每次生成都会保留在这里。', 'eval-empty'));
  state.samples.forEach(s => area.append(sampleCard(s)));
  $('eval-sample-count').textContent = `${state.total} 条 · 全部保留`;
  $('eval-page-label').textContent = `${state.page} / ${Math.max(1, Math.ceil(state.total / 12))}`;
  $('eval-prev').disabled = state.page <= 1; $('eval-next').disabled = state.page * 12 >= state.total;
  updateCompare();
}
function updateCompare() { $('eval-compare').disabled = state.selected.size < 2; $('eval-compare').textContent = `并排比较（${state.selected.size}）`; }
async function refresh() {
  const version = ++refreshVersion;
  const [tasks, samples] = await Promise.all([api('/tasks'), api(`/samples?${query({ page: state.page })}`)]);
  if (version !== refreshVersion) return;
  state.tasks = tasks.tasks; renderTasks();
  const signature = JSON.stringify(samples);
  if (signature !== state.signature) { state.signature = signature; state.samples = samples.samples; state.total = samples.total; renderSamples(); }
}
async function poll() {
  clearTimeout(pollTimer);
  try { if (!document.hidden && Date.now() >= state.cooldown) await refresh(); }
  catch (e) { error(e); }
  finally { pollTimer = setTimeout(poll, Math.max(state.active ? 3000 : 12000, state.cooldown - Date.now())); }
}
function saveBlob(blob, filename) { const url = URL.createObjectURL(blob), a = el('a'); a.href = url; a.download = filename; document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
async function download(path, filename) {
  const response = await fetch(`/api/pelican${path}`, { headers: { 'X-RT-CSRF': csrf }, credentials: 'same-origin', cache: 'no-store' });
  if (!response.ok) { const result = await response.json(); throw new Error(result.error || '下载失败'); }
  saveBlob(await response.blob(), filename);
}
function details(title, content) { const node = el('details', null, 'eval-details'); node.append(el('summary', title), el('pre', typeof content === 'string' ? content : JSON.stringify(content, null, 2))); return node; }
async function openDetail(identifier) {
  const { sample } = await api(`/samples/${identifier}`), area = $('sample-detail');
  $('sample-title').textContent = `${sample.requested_model} · ${sample.id.slice(0, 8)}`;
  area.replaceChildren(preview(sample), checks(sample), el('p', scoreLabel(sample), 'eval-score'));
  area.append(el('p', `请求模型：${sample.requested_model}；返回模型：${sample.returned_model ?? '未知'}。模型名称不证明身份。`, 'field-help'), el('p', `开始 ${timeText(sample.started_at)}；结束 ${timeText(sample.ended_at)}`, 'field-help'));
  const metadata = Object.fromEntries(Object.entries(sample).filter(([k]) => !['raw_output', 'raw_wire', 'svg', 'render_evidence', 'reviews', 'review_audit'].includes(k)));
  area.append(details('请求、Token、批次与参数明细', metadata), details('完整原始回答（只脱敏，不修图）', sample.raw_output), details('完整响应证据（SSE / JSON）', sample.raw_wire), details('评分理由、位置与修订记录', { reviews: sample.reviews, audit: sample.review_audit, recheck: sample.recheck }));
  const actions = el('div', null, 'eval-actions');
  if (sample.svg) actions.append(button('下载原始 SVG', () => act(() => download(`/samples/${sample.id}/svg`, `pelican-${sample.id}.svg`))));
  area.append(actions);
  if (sample.score.count >= 2) {
    const form = el('form'); const reviewer = el('input'), note = el('textarea');
    reviewer.placeholder = '复核者代号'; reviewer.required = true; reviewer.setAttribute('aria-label', '复核者代号');
    note.placeholder = '复核意见（保留原始两份评分及分歧，不覆盖均分）'; note.required = true; note.setAttribute('aria-label', '复核意见');
    const submit = el('button', '保存复核意见', 'button button-secondary'); submit.type = 'submit'; form.append(reviewer, note, submit);
    form.addEventListener('submit', e => { e.preventDefault(); act(async () => { submit.disabled = true; try { await api(`/samples/${identifier}/recheck`, { method: 'POST', body: { reviewer: reviewer.value, note: note.value } }); message('复核已保存。'); $('sample-dialog').close(); await refresh(); } finally { submit.disabled = false; } }); });
    area.append(form);
  }
  $('sample-dialog').showModal();
}
async function openReview(sample, blind = null) {
  if (!blind) sample = (await api(`/samples/${sample.id}`)).sample;
  const evidence = blind ? sample.image : sample.render_evidence?.data_url;
  if (!evidence) throw new Error('图片证据尚未保存，请等待预览完成后再评分');
  state.review = { sample, blind };
  $('review-title').textContent = blind ? `匿名评审 · ${sample.alias}` : `视觉评分 · ${sample.id.slice(0, 8)}`;
  const box = $('review-image'); box.replaceChildren();
  const image = el('img'); image.src = evidence; image.alt = blind ? sample.alias : '本次评分依据的已保存图片';
  const wrapper = el('div', null, 'eval-preview'); wrapper.append(image); box.append(wrapper);
  box.append(el('p', '所有评审均使用这张已保存的图片。动画样本按首次加载后保存的画面评分。', 'field-help'));
  const rubric = sample.question.scoringRubric;
  $('review-scale').textContent = rubric.scale.map((s, i) => `${i}：${s}`).join('；');
  $('review-form').reset(); $('review-reviewer').readOnly = Boolean(blind); $('review-reviewer').value = blind?.reviewer || '';
  const dimensions = $('review-dimensions'); dimensions.replaceChildren();
  for (const dim of rubric.dimensions) {
    const row = el('section', null, 'eval-review-dimension'); row.append(el('h3', `${dim.id} · ${dim.title}`), el('p', dim.criteria));
    const select = el('select'); select.id = `score-${dim.id}`; select.required = true; select.add(new Option('请选择分数', '')); for (let n = 0; n <= 5; n++) select.add(new Option(String(n), String(n)));
    const label = el('label', '分数'); label.htmlFor = select.id; label.append(select);
    const reason = el('textarea'); reason.id = `reason-${dim.id}`; reason.rows = 2; reason.required = true; reason.maxLength = 3000;
    const reasonLabel = el('label', '具体理由'); reasonLabel.htmlFor = reason.id; reasonLabel.append(reason);
    const location = el('input'); location.id = `location-${dim.id}`; location.required = true; location.maxLength = 3000; location.placeholder = '例如：画面右下脚与踏板；无缺陷请明确填写';
    const locationLabel = el('label', '问题位置'); locationLabel.htmlFor = location.id; locationLabel.append(location);
    row.append(label, reasonLabel, locationLabel); dimensions.append(row);
  }
  $('review-status').textContent = ''; $('ai-review-settings').hidden = Boolean(blind);
  $('review-dialog').showModal();
}
async function loadReport() {
  const data = await api(`/report?${query()}`), target = $('eval-report'); target.replaceChildren();
  if (!data.groups.length) { target.append(el('p', '暂无符合当前筛选的样本。', 'eval-empty')); return; }
  data.groups.forEach(group => {
    const item = el('section', null, 'eval-report-group'); item.append(el('h3', `${group.conditions.requested_model} · ${group.conditions.question_id} v${group.conditions.question_version}`));
    const metrics = el('dl', null, 'eval-metrics');
    const values = [['样本 / 运行中', `${group.samples} / ${group.running}`], ['成功 / 失败 / 取消', `${group.success} / ${group.failed} / ${group.cancelled}`], ['失败率', pct(group.failure_rate)], ['有效 SVG', `${group.valid_svg}/${group.samples} · ${pct(group.valid_svg_rate)}`], ['浏览器渲染成功', `${group.render_success}/${group.render_attempted} · ${pct(group.render_rate)}`], ['渲染覆盖率', pct(group.render_coverage)], ['评分覆盖率', `${group.scored}/${group.samples} · ${pct(group.scoring_coverage)}`], ['视觉均分 / 20', `${num(group.total_mean)}（n=${group.scored}）`], ['最低 / 最高', `${num(group.total_min)} / ${num(group.total_max)}`], ['重复波动（总体标准差）', num(group.total_population_sd)], ['单评 / 双人盲评', `${group.single_review_samples} / ${group.double_blind_samples}`], ['待复核 / 已复核', `${group.needs_recheck} / ${group.rechecked}`]];
    for (const [title, value] of values) { const entry = el('div'); entry.append(el('dt', title), el('dd', value)); metrics.append(entry); }
    item.append(metrics, el('p', `各维均分：${Object.entries(group.dimensions).map(([k, v]) => `${k} ${num(v)}`).join(' · ') || '待评分'}。平均评审分歧：${num(group.mean_disagreement)}。${group.interpretation}`));
    item.append(details('性能信息（不计入能力分）', { average_duration_ms: group.duration_mean_ms, duration_reported: group.duration_reported, average_ttft_ms: group.ttft_mean_ms, ttft_reported: group.ttft_reported, token_usage: group.token_usage }), details('完整比较条件与评审方式', { ...group.conditions, review_methods: group.review_methods, reviewer_count: group.reviewer_count }));
    target.append(item);
  });
}
async function changeView(name) {
  state.view = name;
  for (const button of document.querySelectorAll('[data-eval-view]')) { const active = button.dataset.evalView === name; button.setAttribute('aria-pressed', String(active)); button.className = `button button-${active ? 'primary' : 'secondary'}`; }
  for (const view of ['run', 'blind', 'report']) $(`eval-view-${view}`).hidden = name !== view;
  document.querySelector('.eval-taskbar').hidden = name === 'blind';
  if (name === 'report') await loadReport();
}
$('eval-form').addEventListener('submit', e => { e.preventDefault(); act(() => start($('eval-mode').value)); });
$('eval-once').addEventListener('click', () => act(() => start('once')));
$('eval-continuous').addEventListener('click', () => act(() => start('continuous')));
for (const event of ['input', 'change']) $('eval-form').addEventListener(event, updatePlan);
$('eval-load-models').addEventListener('click', () => act(async () => {
  const control = $('eval-load-models'); control.disabled = true;
  try {
    const data = await api('/models', { method: 'POST', body: connection() }); const list = $('eval-model-catalog'); list.replaceChildren(); list.hidden = false;
    if (!data.models.length) list.append(el('p', '接口返回空目录，请手动填写模型 ID。', 'field-help'));
    for (const model of data.models) {
      const label = el('label', null, 'eval-check'), input = el('input'); input.type = 'checkbox'; input.checked = modelIds($('eval-models').value).includes(model);
      input.addEventListener('change', () => { const selected = new Set(modelIds($('eval-models').value)); input.checked ? selected.add(model) : selected.delete(model); $('eval-models').value = [...selected].join('\n'); updatePlan(); });
      label.append(input, document.createTextNode(model)); list.append(label);
    }
    message(`已读取 ${data.models.length} 个模型；当前选择保持不变。`);
  } finally { control.disabled = false; }
}));
$('eval-filter').addEventListener('click', () => act(async () => {
  state.filters = filtersFrom(Object.fromEntries(['model', 'question', 'batch', 'mode', 'technical', 'scoring', 'from', 'to'].map(k => [k, $(`filter-${k}`).value]))); state.page = 1; state.signature = ''; await refresh();
}));
$('refresh-eval').addEventListener('click', () => act(refresh));
for (const [id, step] of [['eval-prev', -1], ['eval-next', 1]]) $(id).addEventListener('click', () => act(async () => { state.page += step; await refresh(); }));
$('eval-compare').addEventListener('click', () => { const area = $('compare-samples'); area.replaceChildren(...[...state.selected.values()].map(s => sampleCard(s, false))); $('compare-dialog').showModal(); });
document.querySelectorAll('[data-export]').forEach(b => b.addEventListener('click', () => act(async () => { b.disabled = true; try { await download(`/export/${b.dataset.export}?${query()}`, `pelican-report.${b.dataset.export}`); } finally { b.disabled = false; } })));
document.querySelectorAll('[data-close]').forEach(b => b.addEventListener('click', () => $(b.dataset.close).close()));
document.querySelectorAll('[data-eval-view]').forEach(b => b.addEventListener('click', () => act(() => changeView(b.dataset.evalView))));
$('report-refresh').addEventListener('click', () => act(loadReport));
$('review-form').addEventListener('submit', e => { e.preventDefault(); act(async () => {
  const { sample, blind } = state.review, dimensions = {};
  for (const d of sample.question.scoringRubric.dimensions) dimensions[d.id] = { score: Number($(`score-${d.id}`).value), reason: $(`reason-${d.id}`).value, location: $(`location-${d.id}`).value };
  $('review-fields').disabled = true;
  try {
    const path = blind ? `/blind/${blind.id}/${encodeURIComponent(sample.alias)}` : `/samples/${sample.id}/reviews`;
    await api(path, { method: 'POST', body: { reviewer: $('review-reviewer').value, dimensions } });
    $('review-dialog').close(); message('人工评分已保存。'); if (blind) blind.buttons.get(sample.alias).textContent = '已提交 · 可修订'; await refresh();
  } finally { $('review-fields').disabled = false; }
}); });
$('review-reviewer').addEventListener('change', () => {
  if (state.review?.blind) return;
  const previous = state.review?.sample.reviews?.find(r => r.method === 'human' && r.reviewer === $('review-reviewer').value.trim());
  if (!previous) return;
  for (const d of state.review.sample.question.scoringRubric.dimensions) for (const [field, prefix] of [['score', 'score'], ['reason', 'reason'], ['location', 'location']]) $(`${prefix}-${d.id}`).value = previous?.dimensions[d.id]?.[field] ?? '';
});
$('ai-review-form').addEventListener('submit', e => { e.preventDefault(); act(async () => {
  const id = state.review.sample.id; $('ai-review-fields').disabled = true; $('review-status').textContent = 'AI 正在根据实际渲染图片评审…';
  try {
    await api(`/samples/${id}/ai-review`, { method: 'POST', body: { ...connection('ai'), model: $('ai-model').value, enabled: $('ai-enabled').checked, vision_confirmed: $('ai-vision').checked } });
    $('ai-key').value = ''; $('review-status').textContent = 'AI 评分已保存，可在样本详情中查看依据。'; await refresh();
  } catch (e) { $('review-status').textContent = e.message; throw e; }
  finally { $('ai-review-fields').disabled = false; }
}); });
$('blind-start').addEventListener('click', () => act(async () => {
  const control = $('blind-start'); control.disabled = true;
  try {
    const data = await api('/blind', { method: 'POST', body: { reviewer: $('blind-reviewer').value, filters: state.filters } });
    state.blind = { ...data, buttons: new Map() }; const area = $('blind-samples'); area.replaceChildren();
    if (!data.samples.length) area.append(el('p', '暂无可盲评的图片。请先在历史中查看并渲染成功样本。', 'eval-empty'));
    data.samples.forEach(sample => { const card = el('article', null, 'eval-sample'), img = el('img'); img.src = sample.image; img.alt = sample.alias; const box = el('div', null, 'eval-preview'); box.append(img); const body = el('div', null, 'eval-sample-body'), action = button('独立评分', () => openReview(sample, state.blind)); body.append(action); card.append(el('h3', sample.alias, 'eval-sample-header'), box, body); area.append(card); state.blind.buttons.set(sample.alias, action); });
  } finally { control.disabled = false; }
}));
document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
await act(async () => {
  const data = await api('/catalog'); state.questions = data.questions.filter(q => q.enabled);
  state.questions.forEach((q, index) => { const option = new Option(q.title, q.id, index === 0, index === 0); $('eval-question').add(option); $('filter-question').add(new Option(q.title, q.id)); });
  updatePlan(); await refresh();
});
pollTimer = setTimeout(poll, 3000);
