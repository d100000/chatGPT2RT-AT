import { createRequestCooldowns, rateLimitError, requestLimitAction } from './request-limits.mjs';
import { getBrowserResults, collectBatchResults, isBatchCollected, resetResultCollection, resultDownload, cachedBatchWithoutServer, withBrowserResultLock } from './browser-results.mjs';

(() => {
  'use strict';
  const byId = (id) => document.getElementById(id);
  const csrf = document.querySelector('meta[name="rt-csrf"]').content;
  const activeStates = new Set(['queued', 'running', 'awaiting_code', 'cancelling']);
  const batchLabels = { queued: '等待启动', running: '批量提取中', awaiting_code: '等待验证码', cancelling: '正在取消', completed: '处理完成', cancelled: '已取消' };
  const rowLabels = { queued: '待处理', running: '提取中', awaiting_code: '等待验证', success: '成功', error: '失败', cancelled: '已取消' };
  const maxAccounts = 1000;
  const maxFileBytes = 2 * 1024 * 1024;
  let batch = null;
  let initializing = true;
  let submitting = false;
  let importing = false;
  let cancelling = false;
  let pollTimer = null;
  let pollRequest = 0;
  let pollPending = false;
  let initializePending = false;
  let initializeTimer = null;
  let cooldownTimer = null;
  const cooldowns = createRequestCooldowns({ now: () => Date.now() });
  const resultStore = getBrowserResults();
  let clearing = false;
  let cleanupRetry = false;
  let expiryTimer = null;
  const activeEntries = new Map();
  let lastRows = '';
  let networkNotice = false;
  let rtAccount = null;
  let rtGeneration = 0;

  function showError(message, errors = []) {
    byId('form-error-message').textContent = message || '';
    const list = byId('validation-errors');
    list.replaceChildren();
    for (const error of errors) {
      const item = document.createElement('li');
      item.textContent = Number(error.line) > 0 ? `第 ${error.line} 行：${error.message}` : error.message;
      list.append(item);
    }
    list.hidden = !errors.length;
    byId('form-error').hidden = !message && !errors.length;
  }

  function connection(online) {
    byId('connection').classList.toggle('offline', !online);
    byId('connection-label').textContent = online ? '会话服务已连接' : '会话服务连接中断';
  }

  async function request(path, options = {}) {
    const action = requestLimitAction(path, options.method || 'GET');
    const remaining = cooldowns.remaining(action);
    if (remaining) throw rateLimitError(null, { retry_after: remaining });
    let response;
    try {
      response = await fetch(path, {
        credentials: 'same-origin', cache: 'no-store', ...options,
        headers: { 'X-RT-CSRF': csrf, 'Content-Type': 'application/json', ...(options.headers || {}) },
      });
    } catch (error) {
      if (error.name === 'AbortError') throw error;
      connection(false);
      throw new Error('无法连接会话服务，请稍后重试。');
    }
    connection(true);
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      if (response.status === 429) {
        const error = rateLimitError(response, payload);
        cooldowns.hold(action, error.retryAfter);
        refreshCooldownControls();
        throw error;
      }
      const error = new Error(payload.error || `请求失败（${response.status}），请重试。`);
      error.status = response.status;
      error.errors = Array.isArray(payload.errors) ? payload.errors : [];
      throw error;
    }
    return response;
  }

  function refreshCooldownControls() {
    clearTimeout(cooldownTimer);
    updateControls();
    const retry = byId('rt-retry-button');
    retry.disabled = false;
    retry.textContent = '重试';
    if (cooldowns.active()) cooldownTimer = setTimeout(refreshCooldownControls, 1000);
  }

  async function jsonRequest(path, options) {
    return (await request(path, options)).json();
  }

  function inputCount() {
    return byId('accounts-text').value.split(/\r\n|\n|\r/).filter((line) => line.trim()).length;
  }

  function updateCount() {
    const count = inputCount();
    byId('import-count').textContent = `已输入 ${count.toLocaleString('zh-CN')} 条${count > maxAccounts ? '，超过 1,000 条上限' : ''}`;
    byId('import-count').classList.toggle('over-limit', count > maxAccounts);
  }

  function updateControls() {
    const active = !!batch && activeStates.has(batch.status);
    const stopping = cancelling || batch?.status === 'cancelling';
    const busy = active || submitting || importing || initializing || clearing;
    byId('credentials-fields').disabled = busy;
    const submitRemaining = cooldowns.remaining('submit');
    const cancelRemaining = cooldowns.remaining('cancel');
    byId('start-button').disabled = busy || submitRemaining > 0;
    byId('start-label').textContent = importing ? '正在读取文件…' : submitting ? '正在提交…' : active ? '批次进行中…' : submitRemaining ? `${submitRemaining} 秒后可重试` : batch ? '开始新批次' : '开始批量提取';
    byId('cancel-button').hidden = !active;
    byId('cancel-button').disabled = stopping || cancelRemaining > 0;
    byId('cancel-button').textContent = stopping ? '正在取消…' : cancelRemaining ? `${cancelRemaining} 秒后可取消` : '取消整批';
    const clear = byId('clear-browser-results');
    if (clear) clear.disabled = clearing || submitting || (!cleanupRetry && !batch && !resultStore.read());
    for (const entry of activeEntries.values()) updateCodeControls(entry);
  }

  function updateCodeControls(entry) {
    const disabled = entry.submitting || cancelling || batch?.status === 'cancelling' || !entry.challengeId;
    const remaining = cooldowns.remaining('code');
    entry.input.disabled = disabled;
    entry.button.disabled = disabled || remaining > 0;
    entry.button.textContent = entry.submitting ? '验证中…' : remaining ? `${remaining} 秒后验证` : '验证';
  }

  function clearActiveJobs() {
    for (const entry of activeEntries.values()) entry.input.value = '';
    activeEntries.clear();
    byId('active-jobs').replaceChildren();
  }

  function makeActiveEntry(job) {
    const section = document.createElement('section');
    section.className = 'active-job';
    const heading = document.createElement('div');
    heading.className = 'active-job-heading';
    const email = document.createElement('h3');
    email.className = 'job-email';
    email.id = `active-email-${job.id}`;
    section.setAttribute('aria-labelledby', email.id);
    const badge = document.createElement('span');
    heading.append(email, badge);
    const message = document.createElement('p');
    message.className = 'job-message';
    const form = document.createElement('form');
    form.className = 'challenge';
    const title = document.createElement('h3');
    const description = document.createElement('p');
    const label = document.createElement('label');
    label.className = 'sr-only';
    label.textContent = `${job.email} 的 6 位验证码`;
    const input = document.createElement('input');
    input.id = `challenge-code-${job.id}`;
    input.type = 'text';
    input.inputMode = 'numeric';
    input.autocomplete = 'one-time-code';
    input.pattern = '[0-9]{6}';
    input.maxLength = 6;
    input.placeholder = '000000';
    input.required = true;
    label.htmlFor = input.id;
    const button = document.createElement('button');
    button.type = 'submit';
    button.className = 'button button-primary';
    button.textContent = '验证';
    button.setAttribute('aria-label', `提交 ${job.email} 的验证码`);
    const row = document.createElement('div');
    row.className = 'code-row';
    row.append(input, button);
    const error = document.createElement('p');
    error.className = 'code-error';
    error.setAttribute('role', 'alert');
    error.hidden = true;
    form.append(title, description, label, row, error);
    const details = document.createElement('details');
    details.className = 'progress-details';
    const summary = document.createElement('summary');
    summary.textContent = '运行记录';
    const events = document.createElement('ol');
    events.className = 'timeline';
    events.setAttribute('aria-label', `${job.email} 的运行记录`);
    details.append(summary, events);
    section.append(heading, message, form, details);
    const entry = { section, email, badge, message, form, title, description, input, button, error, events, job, challengeId: null, submitting: false, eventsSignature: '' };
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (entry.submitting || cancelling || batch?.status === 'cancelling' || !entry.challengeId || cooldowns.remaining('code')) return;
      const code = input.value.trim();
      if (!/^[0-9]{6}$/.test(code)) {
        error.textContent = '请输入 6 位数字验证码。';
        error.hidden = false;
        return;
      }
      const batchId = batch?.id;
      const challengeId = entry.challengeId;
      entry.submitting = true;
      error.hidden = true;
      updateCodeControls(entry);
      try {
        await jsonRequest(`/api/jobs/${encodeURIComponent(job.id)}/code`, { method: 'POST', body: JSON.stringify({ challenge_id: challengeId, code }) });
        if (entry.challengeId === challengeId) input.value = '';
        if (batch?.id === batchId) await pollBatch();
      } catch (failure) {
        if (activeEntries.get(job.id) === entry && entry.challengeId === challengeId) {
          error.textContent = failure.message;
          error.hidden = false;
        }
      } finally {
        entry.submitting = false;
        if (activeEntries.get(job.id) === entry) updateCodeControls(entry);
      }
    });
    return entry;
  }

  function renderActiveJobs(jobs) {
    const visibleJobs = activeStates.has(batch?.status) ? jobs : [];
    const ids = new Set(visibleJobs.map((job) => job.id));
    for (const [id, entry] of activeEntries) {
      if (!ids.has(id)) {
        entry.input.value = '';
        entry.section.remove();
        activeEntries.delete(id);
      }
    }
    for (const job of visibleJobs) {
      let entry = activeEntries.get(job.id);
      if (!entry) {
        entry = makeActiveEntry(job);
        activeEntries.set(job.id, entry);
        byId('active-jobs').append(entry.section);
      }
      entry.job = job;
      entry.email.textContent = job.email;
      entry.badge.textContent = rowLabels[job.status] || job.status;
      entry.badge.className = `job-badge ${badgeClass(job.status)}`;
      entry.message.textContent = job.message || '';
      const challenge = job.status === 'awaiting_code' ? job.challenge : null;
      const challengeId = challenge?.id || null;
      if (entry.challengeId !== challengeId) {
        entry.challengeId = challengeId;
        entry.input.value = '';
        entry.error.textContent = '';
        entry.error.hidden = true;
      }
      entry.form.hidden = !challenge;
      if (challenge) {
        const emailChallenge = challenge.kind === 'email';
        entry.title.textContent = emailChallenge ? '需要邮箱验证码' : '需要 Authenticator 动态码';
        entry.description.textContent = emailChallenge ? '请查看此账号的邮箱，在这里输入收到的 6 位验证码。' : '打开此账号的认证器，在这里输入当前 6 位动态码。';
      }
      updateCodeControls(entry);
      const signature = JSON.stringify(job.events || []);
      if (signature !== entry.eventsSignature) {
        entry.eventsSignature = signature;
        entry.events.replaceChildren();
        for (const event of job.events || []) {
          const item = document.createElement('li');
          const time = document.createElement('time');
          time.textContent = clockLabel(event.time);
          const text = document.createElement('p');
          text.textContent = event.message || '';
          item.append(time, text);
          entry.events.append(item);
        }
      }
    }
    byId('active-panel').hidden = !visibleJobs.length;
    byId('active-summary').textContent = `${visibleJobs.length} / ${batch?.concurrency || 2} 个处理名额`;
  }

  function clockLabel(value) {
    const date = new Date(typeof value === 'number' ? value * 1000 : value);
    return Number.isNaN(date.getTime()) ? '' : date.toLocaleTimeString('zh-CN', { hour12: false });
  }

  function badgeClass(status) {
    if (status === 'awaiting_code') return 'waiting';
    if (activeStates.has(status)) return 'active';
    if (status === 'success') return 'success';
    if (status === 'error') return 'error';
    return '';
  }

  function clearRt() {
    rtGeneration += 1;
    rtAccount = null;
    byId('rt-value').value = '';
    byId('rt-value-field').hidden = true;
    byId('rt-view-email').textContent = '';
    byId('rt-view-status').textContent = '';
    byId('rt-view-status').classList.remove('failed');
    byId('rt-copy-button').disabled = true;
    byId('rt-copy-button').textContent = '复制 RT';
    byId('rt-retry-button').hidden = true;
    byId('rt-dialog').removeAttribute('aria-busy');
  }

  function hideRt() {
    clearRt();
    if (byId('rt-dialog').open) byId('rt-dialog').close();
  }

  async function viewRt(account) {
    clearRt();
    rtAccount = { id: account.id, email: account.email };
    const dialog = byId('rt-dialog');
    byId('rt-view-email').textContent = account.email;
    if (!dialog.open) dialog.showModal();
    const record = resultStore.record(account.id);
    if (!record) {
      byId('rt-view-status').textContent = '结果尚未接收到本浏览器，或浏览器缓存已过期。';
      byId('rt-view-status').classList.add('failed');
      return;
    }
    byId('rt-value').value = record.raw.refresh_token;
    byId('rt-value-field').hidden = false;
    byId('rt-copy-button').disabled = false;
    byId('rt-view-status').textContent = '从本浏览器缓存读取完整 RT。';
  }

  function renderRows(rows) {
    const recordIds = new Set(resultStore.read()?.records.map((record) => record.id));
    const signature = JSON.stringify([rows, [...recordIds]]);
    if (signature === lastRows) return;
    lastRows = signature;
    const fragment = document.createDocumentFragment();
    for (const row of rows) {
      const tr = document.createElement('tr');
      const line = document.createElement('td');
      line.className = 'line-number';
      line.textContent = String(row.line);
      const account = document.createElement('td');
      account.className = 'account-cell';
      const email = document.createElement('div');
      email.className = 'account-email';
      email.textContent = row.email;
      account.append(email);
      if (row.error || (row.message && row.status !== 'queued')) {
        const message = document.createElement('div');
        message.className = `row-message${row.status === 'error' ? ' failed' : ''}`;
        message.textContent = row.status === 'success' && recordIds.has(row.id)
          ? '已接收到本浏览器，下载文件由浏览器生成' : row.error || row.message;
        account.append(message);
      }
      const status = document.createElement('td');
      status.className = 'table-status';
      const badge = document.createElement('span');
      badge.className = `job-badge ${badgeClass(row.status)}`;
      badge.textContent = rowLabels[row.status] || row.status;
      status.append(badge);
      const result = document.createElement('td');
      if (row.status === 'success' && recordIds.has(row.id)) {
        const actions = document.createElement('div');
        actions.className = 'row-actions';
        const view = document.createElement('button');
        view.type = 'button';
        view.className = 'row-download';
        view.textContent = '查看 RT';
        view.setAttribute('aria-label', `查看 ${row.email} 的 RT`);
        view.setAttribute('aria-haspopup', 'dialog');
        view.addEventListener('click', () => viewRt(row));
        const download = document.createElement('button');
        download.type = 'button';
        download.className = 'row-download';
        download.textContent = '原始 JSON ↓';
        download.setAttribute('aria-label', `下载 ${row.email} 的原始 Token JSON`);
        download.addEventListener('click', () => downloadLocal('raw', download, row.id));
        const cpa = document.createElement('button');
        cpa.type = 'button';
        cpa.className = 'row-download';
        cpa.textContent = 'CPA JSON ↓';
        cpa.setAttribute('aria-label', `下载 ${row.email} 的 CPA 认证 JSON`);
        cpa.addEventListener('click', () => downloadLocal('cpa', cpa, row.id));
        const sub2api = document.createElement('button');
        sub2api.type = 'button';
        sub2api.className = 'row-download';
        sub2api.textContent = 'Sub2API JSON ↓';
        sub2api.setAttribute('aria-label', `下载 ${row.email} 的 Sub2API JSON`);
        sub2api.addEventListener('click', () => downloadLocal('sub2api', sub2api, row.id));
        actions.append(view, cpa, sub2api, download);
        result.append(actions);
      } else {
        const empty = document.createElement('span');
        empty.className = 'result-placeholder';
        empty.textContent = '—';
        result.append(empty);
      }
      tr.append(line, account, status, result);
      fragment.append(tr);
    }
    byId('results-body').replaceChildren(fragment);
  }

  function render(currentBatch) {
    if (batch?.id !== currentBatch?.id) {
      hideRt();
      clearActiveJobs();
      if (currentBatch && activeStates.has(currentBatch.status)) byId('concurrency').value = String(currentBatch.concurrency || 2);
    }
    batch = currentBatch;
    document.dispatchEvent(new CustomEvent('rt:batch', { detail: batch }));
    updateControls();
    const counts = batch?.counts || { total: 0, queued: 0, running: 0, awaiting_code: 0, success: 0, error: 0, cancelled: 0 };
    const completed = counts.success + counts.error + counts.cancelled;
    byId('count-total').textContent = counts.total.toLocaleString('zh-CN');
    byId('count-success').textContent = counts.success.toLocaleString('zh-CN');
    byId('count-error').textContent = counts.error.toLocaleString('zh-CN');
    byId('count-pending').textContent = (counts.queued + counts.running + (counts.awaiting_code || 0)).toLocaleString('zh-CN');
    byId('count-progress').textContent = `${completed} / ${counts.total}`;
    byId('batch-progress').max = counts.total || 1;
    byId('batch-progress').value = completed;
    let summary = '等待导入账号';
    if (batch) {
      if (batch.status === 'completed') summary = `处理完成：${counts.success} 条成功，${counts.error} 条失败`;
      else if (batch.status === 'cancelled') summary = `已取消：保留 ${counts.success} 条成功结果，${counts.cancelled} 条未完成`;
      else if (batch.status === 'cancelling') summary = '正在停止所有运行中的账号，已成功结果会保留';
      else if (batch.cooldown_seconds > 0) summary = `服务限流，暂停启动新账号，约 ${batch.cooldown_seconds} 秒后继续`;
      else summary = `正在提取 ${counts.running} 个 · 等待验证 ${counts.awaiting_code || 0} 个 · 排队 ${counts.queued} 个`;
    }
    byId('batch-summary').textContent = batch?.error || summary;
    const badge = byId('batch-badge');
    badge.textContent = batch ? batchLabels[batch.status] || '处理中' : '待开始';
    badge.className = `job-badge ${batch ? badgeClass(batch.status) : ''}`;
    if (batch?.status === 'completed') badge.classList.add(counts.error ? 'waiting' : 'success');
    byId('empty-state').hidden = !!batch;
    byId('results-panel').hidden = !batch;
    renderRows(batch?.rows || []);

    renderActiveJobs(batch?.active_jobs || []);
    const cached = resultStore.read();
    const downloadable = !!cached && cached.batch.id === batch?.id && cached.records.length > 0;
    byId('downloads').hidden = !downloadable;
    const storageStatus = resultStore.status();
    byId('result-path').textContent = downloadable
      ? storageStatus.persistence === 'memory' ? '仅保留在当前页面内存；请立即下载。' : '仅保存在本浏览器缓存；下载由浏览器生成。' : '';
    byId('conversion-heading').textContent = '本浏览器中的成功结果';
    byId('conversion-badge').textContent = downloadable ? storageStatus.persistence === 'memory' ? '当前页面内存' : '浏览器缓存' : '等待接收';
    byId('conversion-badge').className = 'job-badge success';
    byId('conversion-status').textContent = downloadable
      ? `${cached.records.length} 个账号已接收到本浏览器，CPA / Sub2API 文件在本地生成。` : '';
    const notice = byId('browser-cache-status');
    if (notice) notice.textContent = storageStatus.notice || '结果在本浏览器缓存中有效期为 30 分钟；页面运行或下次读取时清理过期缓存。服务器确认交接后即清除临时结果。';
    const clearButton = byId('clear-browser-results');
    if (clearButton) {
      clearButton.disabled = clearing || submitting || (!cleanupRetry && !batch && !cached);
      clearButton.textContent = clearing ? '正在清除…' : cleanupRetry ? '重试清理临时任务' : activeStates.has(batch?.status) ? '结束任务并清除' : '清除本浏览器结果';
      clearButton.title = '清除本浏览器结果并结束本会话的临时任务，其他用户不受影响。';
    }
    clearTimeout(expiryTimer);
    if (cached) expiryTimer = setTimeout(() => {
      hideRt();
      if (!resultStore.read() && !activeStates.has(batch?.status)) { render(null); showError('本浏览器缓存已过期并清除。'); }
    }, Math.max(1, cached.expiresAt - Date.now()));
  }

  function schedulePoll(delay = 1000) {
    clearTimeout(pollTimer);
    if (!document.hidden && batch && (activeStates.has(batch.status) || !isBatchCollected(batch.id))) pollTimer = setTimeout(pollBatch, Math.max(delay, cooldowns.delay('poll')));
  }

  async function pollBatch() {
    if (!batch || pollPending || document.hidden) return;
    if (cooldowns.remaining('poll')) { schedulePoll(); return; }
    clearTimeout(pollTimer);
    pollPending = true;
    let nextDelay = 1000;
    let retry = true;
    const id = batch.id;
    const generation = ++pollRequest;
    try {
      const current = activeStates.has(batch.status)
        ? await jsonRequest(`/api/batches/${encodeURIComponent(id)}`) : batch;
      if (batch?.id !== id || generation !== pollRequest || clearing) return;
      const received = await collectBatchResults(current, jsonRequest);
      if (batch?.id !== id || generation !== pollRequest) return;
      if (batch?.id === id) {
        render(received || current);
        if (networkNotice) { showError(''); networkNotice = false; }
      }
    } catch (error) {
      if (batch?.id !== id || generation !== pollRequest) return;
      if (error.status === 404) {
        const cached = resultStore.read();
        if (cached?.batch.id === id) { render(cached.batch); retry = false; return; }
      }
      networkNotice = true;
      showError(error.status === 403 || error.status === 429 ? error.message : `${error.message} 正在重新获取批次状态。`);
      nextDelay = error.status === 429 ? error.retryAfter * 1000 : 3000;
      retry = error.status !== 403;
    } finally {
      pollPending = false;
      if (retry) schedulePoll(nextDelay);
    }
  }

  function downloadLocal(kind, button, jobId = null) {
    if (button.disabled) return;
    button.disabled = true;
    try {
      const { blob, filename } = resultDownload(resultStore.read(), kind, jobId);
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url; link.download = filename;
      document.body.append(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) { showError(error.message); }
    finally { button.disabled = false; }
  }

  byId('accounts-text').addEventListener('input', () => { updateCount(); byId('file-name').textContent = ''; });
  byId('rt-close-button').addEventListener('click', hideRt);
  byId('rt-dialog').addEventListener('cancel', (event) => { event.preventDefault(); hideRt(); });
  byId('rt-dialog').addEventListener('close', () => { if (!byId('rt-dialog').open) clearRt(); });
  byId('rt-retry-button').addEventListener('click', () => { if (rtAccount) viewRt(rtAccount); });
  byId('rt-copy-button').addEventListener('click', async () => {
    const field = byId('rt-value');
    if (!byId('rt-dialog').open || !field.value || byId('rt-copy-button').disabled) return;
    const generation = rtGeneration;
    byId('rt-copy-button').disabled = true;
    try {
      if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable');
      await navigator.clipboard.writeText(field.value);
      if (generation === rtGeneration && byId('rt-dialog').open) {
        byId('rt-view-status').textContent = 'RT 已复制。';
        byId('rt-view-status').classList.remove('failed');
      }
    } catch (_) {
      if (generation === rtGeneration && byId('rt-dialog').open) {
        field.focus();
        field.select();
        byId('rt-view-status').textContent = '无法自动复制，已选中 RT，请手动复制。';
        byId('rt-view-status').classList.add('failed');
      }
    } finally {
      if (generation === rtGeneration && byId('rt-dialog').open) byId('rt-copy-button').disabled = false;
    }
  });
  window.addEventListener('pagehide', hideRt);
  byId('import-button').addEventListener('click', () => byId('accounts-file').click());
  byId('accounts-file').addEventListener('change', async () => {
    const file = byId('accounts-file').files?.[0];
    if (!file) return;
    showError('');
    if (file.size > maxFileBytes) {
      showError('文件超过 2 MB，请拆分后导入。');
      byId('accounts-file').value = '';
      return;
    }
    importing = true;
    updateControls();
    try {
      const content = await file.text();
      byId('accounts-text').value = content.replace(/^\uFEFF/, '');
      byId('file-name').textContent = file.name;
      updateCount();
    } catch (_) { showError('无法读取该文本文件，请重新选择 UTF-8 编码的 .txt 文件。'); }
    finally { importing = false; byId('accounts-file').value = ''; updateControls(); }
  });

  byId('batch-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (submitting || importing || initializing || cooldowns.remaining('submit') || (batch && activeStates.has(batch.status))) return;
    const count = inputCount();
    if (!count) { showError('请粘贴账号列表或导入 .txt 文件。'); return; }
    if (count > maxAccounts) { showError('每批最多 1,000 条账号，请拆分后提交。'); return; }
    const body = { accounts_text: byId('accounts-text').value, timeout: 180, concurrency: Number(byId('concurrency').value) };
    if (byId('proxy').value.trim()) body.proxy = byId('proxy').value.trim();
    hideRt();
    submitting = true;
    networkNotice = false;
    showError('');
    updateControls();
    try {
      const accepted = await withBrowserResultLock(async () => {
        const value = await jsonRequest('/api/batches', { method: 'POST', body: JSON.stringify(body) });
        resultStore.save(value); return value;
      });
      byId('accounts-text').value = '';
      byId('accounts-file').value = '';
      byId('file-name').textContent = '';
      updateCount();
      resetResultCollection();
      render(accepted);
      schedulePoll();
    } catch (error) { showError(error.message, error.errors || []); }
    finally { body.accounts_text = ''; submitting = false; updateControls(); }
  });

  byId('cancel-button').addEventListener('click', async () => {
    if (!batch || cancelling || batch.status === 'cancelling') return;
    const id = batch.id;
    cancelling = true;
    updateControls();
    try {
      const cancelled = await jsonRequest(`/api/batches/${encodeURIComponent(id)}/cancel`, { method: 'POST', body: '{}' });
      if (batch?.id === id) { pollRequest += 1; render(cancelled); }
      schedulePoll();
    } catch (error) { showError(error.message); }
    finally { cancelling = false; updateControls(); }
  });

  for (const [id, kind] of [['sub2api-button', 'sub2api'], ['cpa-button', 'cpa'], ['zip-button', 'raw'], ['rt-button', 'rt']]) {
    byId(id).addEventListener('click', () => downloadLocal(kind, byId(id)));
  }
  byId('clear-browser-results')?.addEventListener('click', async () => {
    if (clearing || submitting) return;
    clearing = true; clearTimeout(pollTimer); clearTimeout(initializeTimer); pollRequest += 1;
    hideRt(); clearActiveJobs(); resetResultCollection(); render(null);
    initializing = false;
    byId('accounts-text').value = ''; byId('proxy').value = ''; updateCount();
    try {
      await withBrowserResultLock(async () => {
        resetResultCollection(); resultStore.clear();
        document.dispatchEvent(new CustomEvent('rt:results-cleared'));
        await jsonRequest('/api/workspace', { method: 'DELETE' });
      });
      cleanupRetry = false;
      showError('');
      document.dispatchEvent(new CustomEvent('rt:results-cleared'));
    } catch (error) {
      cleanupRetry = true;
      showError(`本浏览器结果已清除；服务器临时任务清理未确认。${error.message} 请重试清理。`);
    } finally { clearing = false; render(batch); }
  });

  async function initialize() {
    if (initializePending || document.hidden) return;
    clearTimeout(initializeTimer);
    if (cooldowns.remaining('poll')) {
      initializeTimer = setTimeout(initialize, cooldowns.delay('poll'));
      return;
    }
    initializePending = true;
    const generation = pollRequest;
    const previousId = resultStore.read()?.batch.id ?? null;
    try {
      const payload = await jsonRequest('/api/batches/current');
      if (generation !== pollRequest || clearing) return;
      let current = payload.batch || cachedBatchWithoutServer(resultStore, previousId);
      if (payload.batch) current = await collectBatchResults(payload.batch, jsonRequest) || current;
      if (generation !== pollRequest || clearing) return;
      render(current);
      showError('');
      initializing = false;
      updateControls();
      schedulePoll();
    } catch (error) {
      if (generation !== pollRequest || clearing) return;
      showError(error.status === 403 || error.status === 429 ? error.message : `${error.message} 正在重新连接。`);
      if (error.status !== 403) initializeTimer = setTimeout(initialize, error.status === 429 ? error.retryAfter * 1000 : 3000);
    } finally {
      initializePending = false;
    }
  }
  document.addEventListener('rt:browser-results', () => {
    hideRt();
    if (clearing || submitting) return;
    const cached = resultStore.read();
    if (!cached) { pollRequest += 1; initializing = false; clearTimeout(pollTimer); resetResultCollection(); render(null); return; }
    const snapshot = cached.batch;
    if (batch?.id === snapshot.id && activeStates.has(snapshot.status)) snapshot.active_jobs = batch.active_jobs || [];
    render(snapshot); schedulePoll();
  });
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      refreshCooldownControls();
      if (initializing) initialize(); else schedulePoll(0);
    } else clearTimeout(pollTimer);
  });
  updateCount();
  const initialCache = resultStore.read();
  if (initialCache) render(initialCache.batch);
  initialize();
})();
