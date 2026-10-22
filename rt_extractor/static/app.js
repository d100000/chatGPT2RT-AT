import { createRequestCooldowns, rateLimitError, requestLimitAction } from './request-limits.mjs';

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
  const coolingDownloads = new Set();
  const activeEntries = new Map();
  let lastRows = '';
  let networkNotice = false;
  let rtAccount = null;
  let rtRequest = null;
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
    byId('connection-label').textContent = online ? '本地服务已连接' : '本地服务连接中断';
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
      throw new Error('无法连接本地服务，请确认服务仍在运行。');
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
    const downloadRemaining = cooldowns.remaining('download');
    retry.disabled = downloadRemaining > 0;
    retry.textContent = downloadRemaining ? `${downloadRemaining} 秒后重试` : '重试';
    for (const button of coolingDownloads) {
      button.disabled = downloadRemaining > 0;
      if (!downloadRemaining) coolingDownloads.delete(button);
    }
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
    const busy = active || submitting || importing || initializing;
    byId('credentials-fields').disabled = busy;
    const submitRemaining = cooldowns.remaining('submit');
    const cancelRemaining = cooldowns.remaining('cancel');
    byId('start-button').disabled = busy || submitRemaining > 0;
    byId('start-label').textContent = importing ? '正在读取文件…' : submitting ? '正在提交…' : active ? '批次进行中…' : submitRemaining ? `${submitRemaining} 秒后可重试` : batch ? '开始新批次' : '开始批量提取';
    byId('cancel-button').hidden = !active;
    byId('cancel-button').disabled = stopping || cancelRemaining > 0;
    byId('cancel-button').textContent = stopping ? '正在取消…' : cancelRemaining ? `${cancelRemaining} 秒后可取消` : '取消整批';
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
    rtRequest?.abort();
    rtRequest = null;
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
    const generation = rtGeneration;
    const dialog = byId('rt-dialog');
    const controller = new AbortController();
    rtRequest = controller;
    byId('rt-view-email').textContent = account.email;
    byId('rt-view-status').textContent = '正在读取 RT…';
    dialog.setAttribute('aria-busy', 'true');
    if (!dialog.open) dialog.showModal();
    try {
      const result = await jsonRequest(`/api/jobs/${encodeURIComponent(account.id)}/rt`, { signal: controller.signal });
      if (generation !== rtGeneration || !dialog.open) return;
      if (typeof result.refresh_token !== 'string' || !result.refresh_token.trim()) throw new Error('结果中没有可查看的 RT。');
      byId('rt-value').value = result.refresh_token;
      byId('rt-value-field').hidden = false;
      byId('rt-copy-button').disabled = false;
      byId('rt-view-status').textContent = '已显示完整 RT，可复制或选中内容。';
    } catch (error) {
      if (generation !== rtGeneration || !dialog.open || error.name === 'AbortError') return;
      byId('rt-view-status').textContent = error.message;
      byId('rt-view-status').classList.add('failed');
      byId('rt-retry-button').hidden = false;
    } finally {
      if (generation === rtGeneration) {
        rtRequest = null;
        dialog.removeAttribute('aria-busy');
      }
    }
  }

  function renderRows(rows) {
    const signature = JSON.stringify(rows);
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
        message.textContent = row.error || row.message;
        account.append(message);
      }
      const status = document.createElement('td');
      status.className = 'table-status';
      const badge = document.createElement('span');
      badge.className = `job-badge ${badgeClass(row.status)}`;
      badge.textContent = rowLabels[row.status] || row.status;
      status.append(badge);
      const result = document.createElement('td');
      if (row.status === 'success' && row.result) {
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
        download.addEventListener('click', () => downloadFile(`/api/jobs/${encodeURIComponent(row.id)}/download`, row.result.filename || 'tokens.json', download));
        const cpa = document.createElement('button');
        cpa.type = 'button';
        cpa.className = 'row-download';
        cpa.textContent = 'CPA JSON ↓';
        cpa.setAttribute('aria-label', `下载 ${row.email} 的 CPA 认证 JSON`);
        cpa.addEventListener('click', () => downloadFile(`/api/jobs/${encodeURIComponent(row.id)}/cpa-download`, `codex-${row.id}.json`, cpa));
        const sub2api = document.createElement('button');
        sub2api.type = 'button';
        sub2api.className = 'row-download';
        sub2api.textContent = 'Sub2API JSON ↓';
        sub2api.setAttribute('aria-label', `下载 ${row.email} 的 Sub2API JSON`);
        sub2api.addEventListener('click', () => downloadFile(`/api/jobs/${encodeURIComponent(row.id)}/sub2api-download`, `sub2api-${row.id}.json`, sub2api));
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
    const downloadable = !!batch?.result && counts.success > 0 && ['completed', 'cancelled'].includes(batch.status);
    byId('downloads').hidden = !downloadable;
    byId('result-path').textContent = downloadable ? batch.result.directory || '' : '';
    const conversionStatus = byId('conversion-status');
    const conversionState = batch?.result?.conversion_status;
    const conversionHeading = byId('conversion-heading');
    const conversionBadge = byId('conversion-badge');
    if (conversionHeading) conversionHeading.textContent = conversionState === 'ready'
      ? 'CPA / Sub2API 已自动生成' : conversionState === 'error' ? '自动保存未完成' : '成功结果与导出';
    if (conversionBadge) {
      conversionBadge.textContent = conversionState === 'ready' ? '已保存' : conversionState === 'error' ? '可按需下载' : '可导出';
      conversionBadge.className = `job-badge ${conversionState === 'error' ? 'error' : 'success'}`;
    }
    if (conversionStatus) {
      conversionStatus.textContent = !downloadable ? '' : batch.result.conversion_status === 'ready'
        ? 'CPA 和 Sub2API 已自动生成并保存，可直接下载。'
        : batch.result.conversion_status === 'error'
          ? batch.result.conversion_error || '自动保存未完成，仍可从已成功账号按需下载。'
          : '这是此前保存的批次，可按需生成 CPA 和 Sub2API 下载。';
      conversionStatus.classList.toggle('failed', batch?.result?.conversion_status === 'error');
    }
  }

  function schedulePoll(delay = 1000) {
    clearTimeout(pollTimer);
    if (!document.hidden && batch && activeStates.has(batch.status)) pollTimer = setTimeout(pollBatch, Math.max(delay, cooldowns.delay('poll')));
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
      const current = await jsonRequest(`/api/batches/${encodeURIComponent(id)}`);
      if (batch?.id !== id || generation !== pollRequest) return;
      if (batch?.id === id) {
        render(current);
        if (networkNotice) { showError(''); networkNotice = false; }
      }
    } catch (error) {
      if (batch?.id !== id || generation !== pollRequest) return;
      networkNotice = true;
      showError(error.status === 403 || error.status === 429 ? error.message : `${error.message} 正在重新获取批次状态。`);
      nextDelay = error.status === 429 ? error.retryAfter * 1000 : 3000;
      retry = error.status !== 403;
    } finally {
      pollPending = false;
      if (retry) schedulePoll(nextDelay);
    }
  }

  async function downloadFile(path, filename, button) {
    if (button.disabled) return;
    button.disabled = true;
    try {
      const response = await request(path);
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement('a');
      link.href = url;
      link.download = filename;
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) { showError(error.message); }
    finally {
      if (cooldowns.remaining('download')) {
        coolingDownloads.add(button);
        refreshCooldownControls();
      } else button.disabled = false;
    }
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
      const accepted = await jsonRequest('/api/batches', { method: 'POST', body: JSON.stringify(body) });
      byId('accounts-text').value = '';
      byId('accounts-file').value = '';
      byId('file-name').textContent = '';
      updateCount();
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

  byId('sub2api-button').addEventListener('click', () => {
    if (!batch?.result) return;
    const selected = batch;
    downloadFile(`/api/batches/${encodeURIComponent(selected.id)}/sub2api-download`, `sub2api-${selected.id}.json`, byId('sub2api-button'));
  });
  byId('cpa-button').addEventListener('click', () => {
    if (!batch?.result) return;
    const selected = batch;
    downloadFile(`/api/batches/${encodeURIComponent(selected.id)}/cpa-download`, `cpa-${selected.id}.zip`, byId('cpa-button'));
  });
  byId('zip-button').addEventListener('click', () => {
    if (!batch?.result) return;
    const selected = batch;
    downloadFile(`/api/batches/${encodeURIComponent(selected.id)}/download`, `tokens-${selected.id}.zip`, byId('zip-button'));
  });
  byId('rt-button').addEventListener('click', () => {
    if (!batch?.result) return;
    const selected = batch;
    downloadFile(`/api/batches/${encodeURIComponent(selected.id)}/rt-download`, `refresh-tokens-${selected.id}.txt`, byId('rt-button'));
  });

  async function initialize() {
    if (initializePending || document.hidden) return;
    clearTimeout(initializeTimer);
    if (cooldowns.remaining('poll')) {
      initializeTimer = setTimeout(initialize, cooldowns.delay('poll'));
      return;
    }
    initializePending = true;
    try {
      const payload = await jsonRequest('/api/batches/current');
      render(payload.batch || null);
      showError('');
      initializing = false;
      updateControls();
      schedulePoll();
    } catch (error) {
      showError(error.status === 403 || error.status === 429 ? error.message : `${error.message} 正在重新连接。`);
      if (error.status !== 403) initializeTimer = setTimeout(initialize, error.status === 429 ? error.retryAfter * 1000 : 3000);
    } finally {
      initializePending = false;
    }
  }
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      refreshCooldownControls();
      if (initializing) initialize(); else schedulePoll(0);
    } else clearTimeout(pollTimer);
  });
  updateCount();
  initialize();
})();
