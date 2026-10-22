import { convertCPARecord, convertSub2ApiDocument, buildMergedSub2ApiDocument, formatMergedPreview } from './converter.mjs';
import { buildZipArchive, makeUniqueFileNames } from './archive.mjs';
import { parsePastedJsonDocuments } from './paste-input.mjs';
import { createRequestCooldowns, rateLimitError } from './request-limits.mjs';

const byId = (id) => document.getElementById(id);
const limits = { items: 1000, perDocument: 2 * 1024 * 1024, total: 20 * 1024 * 1024, preview: 24000 };
const encoder = new TextEncoder();
const modes = {
  cpaToSub2Api: { importLabel: '导入 CPA 认证文件', waiting: '等待导入 CPA 认证文件', placeholder: '{"type":"codex","access_token":"…","id_token":"…"}' },
  sub2ApiToCpa: { importLabel: '导入 Sub2API 配置', waiting: '等待导入 Sub2API 配置', placeholder: '{"accounts":[{"platform":"openai","type":"oauth","credentials":{…}}]}' },
};
let mode = 'cpaToSub2Api';
let rows = [];
let sourceCount = 0;
let totalBytes = 0;
let busy = false;
let dragging = 0;
const seenInputs = new Set();
const latestCooldown = createRequestCooldowns({ now: () => Date.now() });
let latestCooldownTimer;

function updateLatestCooldown() {
  clearTimeout(latestCooldownTimer);
  updateControls();
  if (latestCooldown.remaining('latest')) latestCooldownTimer = setTimeout(updateLatestCooldown, 1000);
}

function showError(message = '') {
  byId('converter-error').textContent = message;
  byId('converter-error').hidden = !message;
}

function safeReason(error) {
  const message = error instanceof Error ? error.message : '';
  if (message.startsWith('暂不支持 type=')) return '暂不支持此账号类型，请使用 OAuth 认证文件。';
  if (message.startsWith('暂不支持 platform=')) return '暂不支持此平台，仅支持 Codex、Claude、Antigravity 和 Gemini。';
  // Only converter-owned validation messages are displayed, never native JSON errors.
  if (/^(缺少 |access_token 不是|id_token 不是|credentials\.|不是有效的 sub2api|sub2api 配置中的|文件不是 JSON|account 不是对象|没有可导出的认证字段)/u.test(message)) return message;
  return '无法转换此账号，请检查文件格式及必需的认证字段。';
}

function updateControls() {
  byId('converter-input-fields').disabled = busy;
  byId('json-drop-zone').setAttribute('aria-disabled', String(busy));
  byId('mode-cpa').disabled = busy;
  byId('mode-sub2api').disabled = busy;
  byId('converter-clear').disabled = busy || (!rows.length && !byId('converter-json').value && !sourceCount);
  byId('convert-paste-button').disabled = busy || !byId('converter-json').value.trim();
  if (byId('converter-use-latest')) {
    const remaining = latestCooldown.remaining('latest');
    byId('converter-use-latest').disabled = busy || remaining > 0;
    byId('converter-use-latest').textContent = remaining ? `${remaining} 秒后可载入` : '载入最近提取结果 ↗';
  }
  for (const id of ['convert-merged', 'convert-split', 'convert-preview']) byId(id).disabled = busy;
  for (const button of byId('converter-results-body').querySelectorAll('button')) button.disabled = busy;
}

function appendText(parent, tag, text, className = '') {
  const element = document.createElement(tag);
  element.textContent = text;
  if (className) element.className = className;
  parent.append(element);
  return element;
}

function makeButton(label, action, parent) {
  const button = appendText(parent, 'button', label, 'row-download');
  button.type = 'button';
  button.addEventListener('click', action);
  return button;
}

function successfulRecords() { return rows.filter((row) => row.record).map((row) => row.record); }

function render() {
  const records = successfulRecords();
  const names = makeUniqueFileNames(records.map((record) => record.outputFileName));
  records.forEach((record, index) => { record.downloadName = names[index]; });
  byId('convert-count-total').textContent = rows.length.toLocaleString('zh-CN');
  byId('convert-count-success').textContent = records.length.toLocaleString('zh-CN');
  byId('convert-count-error').textContent = (rows.length - records.length).toLocaleString('zh-CN');
  byId('converter-empty').hidden = rows.length > 0;
  byId('converter-results').hidden = !rows.length;
  byId('converter-downloads').hidden = !records.length;
  byId('convert-merged').hidden = mode !== 'cpaToSub2Api';
  byId('convert-preview').hidden = mode !== 'cpaToSub2Api';
  byId('convert-split').textContent = mode === 'sub2ApiToCpa' ? '下载 CPA ZIP' : records.length > 3 ? '下载拆分 Sub2API ZIP' : '下载拆分文件';
  byId('converter-export-help').textContent = mode === 'cpaToSub2Api'
    ? '仅导出转换成功的账号。拆分结果超过 3 个时自动打包为 ZIP。'
    : '每个成功账号生成一个 CPA 认证文件，批量下载打包为 ZIP；不支持的平台或非 OAuth 账号会跳过。';
  if (!busy) byId('converter-status').textContent = rows.length
    ? `转换成功 ${records.length} 个，失败或跳过 ${rows.length - records.length} 个。可继续导入文件或粘贴内容。`
    : modes[mode].waiting;

  const fragment = document.createDocumentFragment();
  for (const row of rows) {
    const tr = document.createElement('tr');
    const account = appendText(tr, 'td', '', 'account-cell');
    appendText(account, 'div', row.record?.email || row.record?.entryLabel || row.label, 'account-email');
    if (row.record?.email || row.record?.entryLabel) appendText(account, 'div', row.label, 'converter-source');
    appendText(tr, 'td', row.record?.providerLabel || '—', 'provider-cell');
    const status = appendText(tr, 'td', '', 'table-status');
    appendText(status, 'span', row.record ? '成功' : row.skipped ? '已跳过' : '失败', `job-badge ${row.record ? 'success' : 'error'}`);
    const actions = appendText(tr, 'td', '');
    if (row.record) {
      const group = appendText(actions, 'div', '', 'row-actions');
      const preview = makeButton('预览 JSON', () => previewDocument(row.record.document, row.record.downloadName), group);
      preview.setAttribute('aria-haspopup', 'dialog');
      makeButton('下载 JSON ↓', () => saveBlob(jsonBlob(row.record.document), row.record.downloadName), group);
    } else {
      appendText(actions, 'div', row.reason, 'row-message failed');
    }
    fragment.append(tr);
  }
  byId('converter-results-body').replaceChildren(fragment);
  updateControls();
}

function addFailure(label, reason, skipped = false) {
  if (rows.length >= limits.items) { showError('累计已达到 1,000 个账号上限，请下载结果并清空后继续。'); return; }
  rows.push({ label, reason, skipped });
}

async function inputFingerprint(value) {
  // Canonical JSON makes formatting and object-key order irrelevant. Keep only
  // hashes in the deduplication set; token material already lives in result rows.
  const canonical = JSON.stringify(value, (_key, item) => item && typeof item === 'object' && !Array.isArray(item)
    ? Object.fromEntries(Object.keys(item).sort().map((key) => [key, item[key]])) : item);
  const digest = await crypto.subtle.digest('SHA-256', encoder.encode(canonical));
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('');
}

async function seenBefore(value, label) {
  const fingerprint = await inputFingerprint(value);
  if (seenInputs.has(fingerprint)) {
    addFailure(label, '重复导入，已忽略。相同内容仅转换一次；更新后的 Token 可重新导入。', true);
    return true;
  }
  seenInputs.add(fingerprint);
  return false;
}

async function addDocument(document, label) {
  const entries = mode === 'cpaToSub2Api' && Array.isArray(document) ? document : [document];
  let count = entries.length;
  if (mode === 'sub2ApiToCpa') count = Array.isArray(document) ? document.length : Array.isArray(document?.accounts) ? document.accounts.length : 1;
  if (count > limits.items - rows.length) {
    addFailure(label, '此文档的账号数超过剩余额度。累计最多 1,000 个账号，请拆分文档或清空结果后重试。');
    return false;
  }
  if (!count) { addFailure(label, '此文档中没有可转换的账号。'); return false; }
  if (mode === 'cpaToSub2Api') {
    let allSucceeded = true;
    for (const [index, entry] of entries.entries()) {
      const sourceName = entries.length > 1 ? `${label} #${index + 1}` : label;
      if (await seenBefore(entry, sourceName)) { allSucceeded = false; continue; }
      try {
        rows.push({ label: sourceName, record: convertCPARecord(entry, { sourceName }) });
      } catch (error) { allSucceeded = false; addFailure(sourceName, safeReason(error)); }
    }
    return allSucceeded;
  }
  if (await seenBefore(document, label)) return false;
  try {
    const result = convertSub2ApiDocument(document, { sourceName: label });
    for (const record of result.converted) rows.push({ label, record });
    for (const skipped of result.skipped) addFailure(`${label} · ${skipped.entryLabel}`, safeReason(new Error(skipped.reason)), true);
    return !result.skipped.length && result.converted.length === count;
  } catch (error) { addFailure(label, safeReason(error)); return false; }
}

async function perform(action, message) {
  if (busy) return;
  busy = true;
  showError();
  updateControls();
  byId('converter-status').textContent = message;
  try { await action(); }
  catch { showError('操作未完成，请重试。文件和已有转换结果仍保留在当前页面中。'); }
  finally { busy = false; render(); }
}

function reserveSources(count, bytes) {
  if (sourceCount + count > limits.items) { showError('累计最多导入 1,000 个文件或 JSON 文档，请下载结果并清空后继续。'); return false; }
  if (totalBytes + bytes > limits.total) { showError('累计输入超过 20 MiB，请下载结果并清空后分批导入。'); return false; }
  sourceCount += count;
  totalBytes += bytes;
  return true;
}

async function importFiles(fileList, directory = false) {
  const files = Array.from(fileList).filter((file) => !directory || /\.json$/iu.test(file.name));
  if (!files.length) { showError('没有找到 .json 文件，请选择包含认证 JSON 的文件或目录。'); return; }
  await perform(async () => {
    if (!reserveSources(files.length, files.reduce((sum, file) => sum + file.size, 0))) return;
    for (const [index, file] of files.entries()) {
      if (rows.length >= limits.items) { showError('已达到 1,000 个账号上限，剩余文件未处理。请下载并清空后分批导入。'); break; }
      const label = file.webkitRelativePath || file.name;
      if (!/\.json$/iu.test(file.name)) { addFailure(label, '请选择 .json 文件。'); continue; }
      if (file.size > limits.perDocument) { addFailure(label, '文件超过 2 MiB，请拆分后重试。'); continue; }
      let document;
      try { document = JSON.parse((await file.text()).replace(/^\uFEFF/u, '')); }
      catch { addFailure(label, '无法读取有效 JSON，请检查文件编码和 JSON 语法。'); continue; }
      await addDocument(document, label);
      if (index % 25 === 0) {
        byId('converter-status').textContent = `正在处理文件 ${index + 1} / ${files.length}…`;
        await new Promise((resolve) => setTimeout(resolve, 0));
      }
    }
  }, '正在读取并转换 JSON 文件…');
}

async function importPasted() {
  const text = byId('converter-json').value;
  if (!text.trim()) return;
  await perform(async () => {
    if (rows.length >= limits.items) { showError('已达到 1,000 个账号上限，粘贴内容未处理。请下载并清空后分批导入。'); return; }
    const bytes = encoder.encode(text).length;
    if (bytes > limits.total || totalBytes + bytes > limits.total) { showError('累计输入超过 20 MiB，请分批粘贴，或清空结果后重试。'); return; }
    const parsed = parsePastedJsonDocuments(text.replace(/^\uFEFF/u, ''));
    if (!reserveSources(parsed.documents.length + parsed.issues.length, bytes)) return;
    let allSucceeded = !parsed.issues.length && parsed.documents.length > 0;
    for (const issue of parsed.issues) addFailure(issue.label, 'JSON 语法无效或内容不完整，请检查对应文档。');
    for (const [index, document] of parsed.documents.entries()) {
      const label = `粘贴内容 #${index + 1}`;
      if (encoder.encode(JSON.stringify(document)).length > limits.perDocument) {
        allSucceeded = false;
        addFailure(label, '单个 JSON 文档超过 2 MiB，请拆分后重试。');
      } else {
        const succeeded = await addDocument(document, label);
        allSucceeded = succeeded && allSucceeded;
      }
      if (index % 25 === 0) await new Promise((resolve) => setTimeout(resolve, 0));
    }
    if (allSucceeded) {
      byId('converter-json').value = '';
    }
  }, '正在解析并转换粘贴内容…');
}

function jsonBlob(document) { return new Blob([JSON.stringify(document, null, 2) + '\n'], { type: 'application/json;charset=utf-8' }); }

function saveBlob(blob, fileName) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = fileName;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function dateName() { return new Date().toISOString().replace(/[:.]/gu, '-'); }

function previewDocument(document, title) {
  byId('converter-preview-title').textContent = title;
  byId('converter-preview-value').textContent = formatMergedPreview(document, limits.preview);
  byId('converter-preview-dialog').showModal();
}

function closePreview() {
  byId('converter-preview-dialog').close();
  byId('converter-preview-value').textContent = '';
  byId('converter-preview-title').textContent = '预览转换结果';
}

function reset() {
  closePreview();
  rows = [];
  sourceCount = 0;
  totalBytes = 0;
  seenInputs.clear();
  byId('converter-json').value = '';
  byId('json-files').value = '';
  byId('json-directory').value = '';
  if (byId('converter-origin')) byId('converter-origin').hidden = true;
  showError();
  render();
}

function setMode(next, internal = false) {
  if ((!internal && busy) || next === mode) return;
  mode = next;
  byId('mode-cpa').setAttribute('aria-pressed', String(mode === 'cpaToSub2Api'));
  byId('mode-sub2api').setAttribute('aria-pressed', String(mode === 'sub2ApiToCpa'));
  byId('converter-import-heading').textContent = modes[mode].importLabel;
  byId('converter-json').placeholder = modes[mode].placeholder;
  byId('converter-paste-help').textContent = mode === 'cpaToSub2Api'
    ? '支持单个 JSON、连续 JSON、JSONL 和 CPA 账号数组。可继续导入，结果会追加到列表。'
    : '支持完整 Sub2API 配置、账号数组或单账号对象，也支持连续 JSON 和 JSONL。只转换 OAuth 账号。';
  reset();
}

async function loadLatestExtraction() {
  if (latestCooldown.remaining('latest')) return;
  await perform(async () => {
    const csrf = document.querySelector('meta[name="rt-csrf"]')?.content;
    if (!csrf) { showError('本地页面凭据缺失，请刷新后重试。'); return; }
    const localJson = async (path) => {
      const response = await fetch(path, {
        credentials: 'same-origin', cache: 'no-store', headers: { 'X-RT-CSRF': csrf },
      });
      if (response.status === 429) {
        const error = rateLimitError(response, await response.json().catch(() => ({})));
        latestCooldown.hold('latest', error.retryAfter);
        updateLatestCooldown();
        throw error;
      }
      if (!response.ok) throw new Error('local-request-failed');
      return response.json();
    };
    let source, label;
    try {
      const { batch } = await localJson('/api/batches/current');
      if (batch) {
        if (!['completed', 'cancelled'].includes(batch.status)) {
          showError('最近批次仍在进行，请等待结束后载入。提取页可随时查看进度和已成功账号。'); return;
        }
        if (!batch.counts?.success || !batch.result) {
          showError('最近批次没有成功结果，请先在 Token 获取中完成提取。'); return;
        }
        source = await localJson(`/api/batches/${encodeURIComponent(batch.id)}/conversion-source`);
        label = `最近提取批次 · ${batch.counts.success} 个成功账号`;
      } else {
        const { job } = await localJson('/api/jobs/current');
        if (!job || job.status !== 'success') {
          showError('没有可载入的提取结果，请先在 Token 获取中完成提取。'); return;
        }
        source = { accounts: [await localJson(`/api/jobs/${encodeURIComponent(job.id)}/cpa-download`)] };
        label = `最近提取账号 · ${job.email}`;
      }
    } catch (error) {
      showError(error.status === 429 ? error.message : '无法读取最近提取结果，请确认本地服务正在运行，刷新页面后重试。'); return;
    }
    const bytes = encoder.encode(JSON.stringify(source)).length;
    if (!Array.isArray(source?.accounts) || !source.accounts.length || source.accounts.length > limits.items || bytes > limits.total) {
      showError('最近结果为空或超过转换容量，请分批导入已下载的认证文件。'); return;
    }
    // Keep the selected direction. A full CPA source preserves plan/refresh
    // metadata that the older download schema deliberately leaves out.
    const direction = mode;
    const cpaRecords = source.accounts;
    let input = cpaRecords;
    if (direction === 'sub2ApiToCpa') {
      input = buildMergedSub2ApiDocument(cpaRecords.map((record) => convertCPARecord(record)));
    }
    reset();
    reserveSources(1, bytes);
    await addDocument(input, label);
    const origin = byId('converter-origin');
    if (origin) {
      origin.textContent = `${label}，当前转换方向：${direction === 'cpaToSub2Api' ? 'CPA → Sub2API' : 'Sub2API → CPA'}。可切换方向后重新载入。`;
      origin.hidden = false;
    }
  }, '正在载入最近提取结果…');
}

byId('mode-cpa').addEventListener('click', () => setMode('cpaToSub2Api'));
byId('mode-sub2api').addEventListener('click', () => setMode('sub2ApiToCpa'));
byId('json-files-button').addEventListener('click', () => byId('json-files').click());
byId('json-directory-button').addEventListener('click', () => byId('json-directory').click());
byId('json-files').addEventListener('change', async (event) => { await importFiles(event.target.files); event.target.value = ''; });
byId('json-directory').addEventListener('change', async (event) => { await importFiles(event.target.files, true); event.target.value = ''; });
byId('converter-json').addEventListener('input', updateControls);
byId('convert-paste-button').addEventListener('click', importPasted);
byId('converter-clear').addEventListener('click', reset);
byId('converter-use-latest')?.addEventListener('click', loadLatestExtraction);
byId('converter-preview-close').addEventListener('click', closePreview);
byId('converter-preview-dialog').addEventListener('cancel', (event) => { event.preventDefault(); closePreview(); });
byId('converter-preview-dialog').addEventListener('close', () => { byId('converter-preview-value').textContent = ''; });
byId('convert-preview').addEventListener('click', () => previewDocument(buildMergedSub2ApiDocument(successfulRecords()), '合并 Sub2API JSON'));
byId('convert-merged').addEventListener('click', () => perform(async () => {
  const records = successfulRecords();
  if (mode === 'cpaToSub2Api' && records.length) saveBlob(jsonBlob(buildMergedSub2ApiDocument(records)), `sub2api-${dateName()}.json`);
}, '正在准备合并 JSON…'));
byId('convert-split').addEventListener('click', () => perform(async () => {
  const records = successfulRecords();
  if (!records.length) return;
  if (mode === 'sub2ApiToCpa' || records.length > 3) {
    const entries = records.map((record) => ({ fileName: record.downloadName, text: JSON.stringify(record.document, null, 2) + '\n' }));
    saveBlob(buildZipArchive(entries), `${mode === 'sub2ApiToCpa' ? 'cpa' : 'sub2api-split'}-${dateName()}.zip`);
  } else {
    for (const record of records) {
      saveBlob(jsonBlob(record.document), record.downloadName);
      await new Promise((resolve) => setTimeout(resolve, 150));
    }
  }
}, '正在准备拆分文件…'));

const dropZone = byId('json-drop-zone');
dropZone.addEventListener('dragenter', (event) => { event.preventDefault(); if (!busy) { dragging += 1; dropZone.classList.add('dragging'); } });
dropZone.addEventListener('dragover', (event) => { event.preventDefault(); if (event.dataTransfer) event.dataTransfer.dropEffect = busy ? 'none' : 'copy'; });
dropZone.addEventListener('dragleave', (event) => { event.preventDefault(); dragging = Math.max(0, dragging - 1); if (!dragging) dropZone.classList.remove('dragging'); });
dropZone.addEventListener('drop', (event) => {
  event.preventDefault();
  dragging = 0;
  dropZone.classList.remove('dragging');
  if (!busy && event.dataTransfer) importFiles(event.dataTransfer.files);
});
// Dropping outside the import area must not navigate away from in-memory results.
document.addEventListener('dragover', (event) => event.preventDefault());
document.addEventListener('drop', (event) => event.preventDefault());
render();
if (typeof window !== 'undefined' && new URLSearchParams(window.location.search).get('source') === 'latest') {
  loadLatestExtraction();
}
