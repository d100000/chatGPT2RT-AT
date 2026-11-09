import { createRequestCooldowns, rateLimitError, requestLimitAction } from './request-limits.mjs';
import { getBrowserResults, collectBatchResults, isBatchCollected, resetResultCollection, resultDownload, cachedBatchWithoutServer, withBrowserResultLock } from './browser-results.mjs';
import { createBatchQueue } from './batch-queue.mjs';

(() => {
  'use strict';
  const byId = (id) => document.getElementById(id);
  const csrf = document.querySelector('meta[name="rt-csrf"]').content;
  const activeStates = new Set(['queued', 'running', 'awaiting_code', 'cancelling']);
  const batchLabels = { queued: '等待启动', running: '批量提取中', awaiting_code: '等待验证码', cancelling: '正在取消', completed: '处理完成', cancelled: '已取消' };
  const rowLabels = { queued: '待处理', running: '提取中', awaiting_code: '等待验证', success: '成功', error: '失败', skipped: '已跳过', cancelled: '已取消' };
  const maxAccounts = 1000;
  const maxFileBytes = 2 * 1024 * 1024;
  let batch = null;
  let initializing = true;
  let submitting = false;
  let confirmingProxy = false;
  let importing = false;
  let cancelling = false;
  let savingSkipSetting = false;
  let pollTimer = null;
  let pollRequest = 0;
  let pollPending = false;
  let initializePending = false;
  let initializeTimer = null;
  let cooldownTimer = null;
  const cooldowns = createRequestCooldowns({ now: () => Date.now() });
  const resultStore = getBrowserResults();
  let workspaceRevision = resultStore.clearRevision?.() || '';
  let clearing = false;
  let cleanupRetry = false;
  let expiryTimer = null;
  const activeEntries = new Map();
  let lastRows = '';
  let networkNotice = false;
  let rtAccount = null;
  let rtGeneration = 0;
  let sequence = null;
  let sequenceTimer = null;
  let sequenceGeneration = 0;
  let sequenceArchiveSignature = '';

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
    byId('import-count').textContent = `已输入 ${count.toLocaleString('zh-CN')} 条${count > maxAccounts ? `，将分为 ${Math.ceil(count / maxAccounts)} 组依次处理` : ''}`;
    byId('import-count').classList.remove('over-limit');
  }

  function sequenceActive() { return !!sequence?.active(); }
  function canPollInBackground() { return sequenceActive(); }

  function renderSequence() {
    const panel = byId('batch-queue-panel');
    if (!panel) return;
    panel.hidden = !sequence;
    if (!sequence) return;
    const plan = sequence.snapshot(batch);
    const labels = { ready: '准备下一组', submitting: '正在提交', running: '分组处理中', waiting: '等待服务额度', completed: '全部分组结束', stopped: '分组已停止', cleared: '已清空' };
    byId('queue-status').textContent = labels[plan.state];
    byId('queue-progress-label').textContent = `第 ${plan.group} / ${plan.groupCount} 组 · 已处理 ${plan.processed.toLocaleString('zh-CN')} / ${plan.total.toLocaleString('zh-CN')} 条`;
    byId('queue-progress').max = plan.total || 1;
    byId('queue-progress').value = plan.processed;
    byId('queue-message').textContent = plan.state === 'waiting'
      ? `${plan.message} ${plan.retrySeconds} 秒后自动提交第 ${plan.group} 组。可随时停止后续分组。`
      : plan.message || (plan.state === 'completed' ? '所有分组已完成结果交接，可按组下载。' : '当前组结束并完整接收结果后，才会提交下一组。');
    byId('queue-retention').textContent = plan.expiresAt
      ? `分组归档仅在本页内存中，还剩 ${Math.max(0, Math.ceil((plan.expiresAt - Date.now()) / 60000))} 分钟；刷新或关闭页面会丢失，请及时按组下载。`
      : '后续账号与分组归档仅在本页内存中。刷新、关闭或设备休眠可能中断计划；请按组及时下载。归档最多 20 MiB，计划结束或停止后保留 30 分钟。';
    const signature = JSON.stringify(plan.archives);
    if (signature !== sequenceArchiveSignature) {
      sequenceArchiveSignature = signature;
      const list = byId('queue-archives');
      list.replaceChildren();
      for (const archive of plan.archives) {
        const row = document.createElement('div'); row.className = 'queue-archive';
        const label = document.createElement('p'); label.className = 'field-help';
        label.textContent = `第 ${archive.number} 组 · ${archive.count} 条 · 成功 ${archive.counts.success} · 失败 ${archive.counts.error} · 跳过 ${archive.counts.skipped || 0}${archive.available ? '' : ' · 归档已过期'}`;
        row.append(label);
        if (archive.available && archive.counts.success) {
          const actions = document.createElement('div'); actions.className = 'download-actions';
          for (const [kind, text] of [['raw', '原始 ZIP'], ['cpa', 'CPA ZIP'], ['sub2api', 'Sub2API JSON'], ['rt', 'RT 文本']]) {
            const button = document.createElement('button'); button.type = 'button'; button.className = 'button button-secondary'; button.textContent = text;
            button.setAttribute('aria-label', `下载第 ${archive.number} 组 ${text}`);
            button.addEventListener('click', () => {
              const result = sequence?.archive(archive.number);
              if (!result) { renderSequence(); showError('该分组归档已过期或被清除。'); return; }
              downloadSnapshot(result, kind, button);
            });
            actions.append(button);
          }
          row.append(actions);
        }
        list.append(row);
      }
    }
  }

  function scheduleSequence() {
    clearTimeout(sequenceTimer);
    if (!sequence) return;
    const plan = sequence.snapshot(batch);
    if (plan.state === 'waiting') sequenceTimer = setTimeout(async () => {
      renderSequence(); updateControls();
      if (sequence?.snapshot(batch).retrySeconds === 0) await submitNextGroup();
      else scheduleSequence();
    }, Math.min(1000, Math.max(1, plan.retryAt - Date.now())));
    else if (plan.expiresAt > Date.now()) sequenceTimer = setTimeout(() => {
      renderSequence(); scheduleSequence();
    }, Math.min(60_000, plan.expiresAt - Date.now()));
  }

  function stopSequence(message) {
    if (!sequenceActive()) return;
    sequence.stop(message);
    sequenceGeneration += 1;
    renderSequence(); scheduleSequence(); updateControls();
  }

  function handleExternalClear() {
    const revision = resultStore.clearRevision?.() || '';
    if (revision === workspaceRevision) return;
    // A lock-time read can see the marker before its storage/channel event.
    // Consume it now so that delayed notification cannot clear a later plan.
    resultStore.receiveClear?.(revision);
    workspaceRevision = revision;
    pollRequest += 1; sequenceGeneration += 1;
    clearTimeout(pollTimer); clearTimeout(initializeTimer); clearTimeout(sequenceTimer);
    sequence?.clear(); sequence = null; sequenceArchiveSignature = '';
    initializing = false; hideRt(); clearActiveJobs(); resetResultCollection();
    byId('accounts-text').value = ''; byId('accounts-file').value = ''; byId('file-name').textContent = ''; byId('proxy').value = '';
    updateCount(); render(null); renderSequence();
    document.dispatchEvent(new CustomEvent('rt:results-cleared'));
    showError('另一个标签页已清空本会话，本页分组计划与内存归档已清除。请重新导入账号后开始。');
  }

  function workspaceWasCleared() {
    if ((resultStore.clearRevision?.() || '') === workspaceRevision) return false;
    handleExternalClear(); return true;
  }

  function stopReplacedSequence(cached) {
    if (!sequenceActive() || !cached || cached.batch.id === (sequence.currentId() || batch?.id)) return false;
    stopSequence('另一个标签页已切换到新的批次，本页后续分组已停止。已完成分组仍可下载，请核对未处理账号后重新导入。');
    return true;
  }

  async function submitNextGroup() {
    if (!sequenceActive() || submitting || clearing) return;
    const selected = sequence;
    const body = selected.take();
    if (!body) return;
    const generation = sequenceGeneration;
    let accepted = null;
    submitting = true; networkNotice = false; showError(''); updateControls(); renderSequence();
    try {
      await withBrowserResultLock(async () => {
        if (workspaceWasCleared() || stopReplacedSequence(resultStore.read())) return;
        if (selected !== sequence || generation !== sequenceGeneration) return;
        accepted = await jsonRequest('/api/batches', { method: 'POST', body: JSON.stringify(body) });
        if (workspaceWasCleared()) return;
        if (selected !== sequence || generation !== sequenceGeneration) return;
        selected.accept(accepted);
        resultStore.save(accepted);
      });
      if (selected !== sequence || generation !== sequenceGeneration || !accepted) return;
      resetResultCollection(); render(accepted); schedulePoll();
    } catch (error) {
      if (selected !== sequence || generation !== sequenceGeneration) return;
      if (error.status === 429 && !accepted) {
        selected.wait(error.retryAfter, error.message);
      } else {
        stopSequence(accepted
          ? '本组已提交，但浏览器无法保存接收记录。已停止后续分组，请检查当前任务。'
          : error.status ? `分组提交失败，已停止后续分组。${error.message}`
            : '本组提交结果未确认，已停止自动提交。请先检查服务器任务，勿直接重复提交本组。');
        if (accepted) { resetResultCollection(); render(accepted); schedulePoll(); }
        showError(error.message, error.errors || []);
      }
    } finally {
      body.accounts_text = ''; body.proxy = '';
      submitting = false; updateControls(); renderSequence(); scheduleSequence();
    }
  }

  async function advanceSequence(current) {
    if (!sequence || sequence.currentId() !== current?.id || activeStates.has(current.status)) return;
    if (!isBatchCollected(current.id)) return;
    sequence.finish(current, resultStore.read(), true);
    renderSequence(); scheduleSequence(); updateControls();
    if (sequence.snapshot(batch).state === 'ready') await submitNextGroup();
  }

  function updateControls() {
    const active = !!batch && activeStates.has(batch.status);
    const stopping = cancelling || batch?.status === 'cancelling';
    const busy = active || sequenceActive() || submitting || confirmingProxy || importing || initializing || clearing || savingSkipSetting;
    byId('credentials-fields').disabled = busy;
    byId('clear-accounts-button').disabled = busy || (!byId('accounts-text').value && !byId('file-name').textContent);
    const submitRemaining = cooldowns.remaining('submit');
    const cancelRemaining = cooldowns.remaining('cancel');
    byId('start-button').disabled = busy || submitRemaining > 0;
    byId('start-label').textContent = importing ? '正在读取文件…' : submitting ? '正在提交…' : sequenceActive() ? '分组计划进行中…' : active ? '批次进行中…' : submitRemaining ? `${submitRemaining} 秒后可重试` : batch ? '开始新批次' : '开始批量提取';
    byId('cancel-button').hidden = !active && !sequenceActive();
    byId('cancel-button').disabled = stopping || submitting || savingSkipSetting || (active && cancelRemaining > 0);
    byId('cancel-button').textContent = stopping ? '正在取消…' : active && cancelRemaining ? `${cancelRemaining} 秒后可取消` : sequenceActive() ? '停止分组计划' : '取消整批';
    const clear = byId('clear-browser-results');
    if (clear) clear.disabled = clearing || submitting || confirmingProxy || initializing || savingSkipSetting;
    const skipSetting = byId('skip-email-verification');
    skipSetting.disabled = submitting || confirmingProxy || initializing || clearing || stopping || savingSkipSetting || (active && cancelRemaining > 0);
    byId('skip-email-help').textContent = savingSkipSetting ? '正在保存设置…'
      : active && cancelRemaining ? `${cancelRemaining} 秒后可更改设置。开启后也会跳过当前等待邮箱验证的账号。`
      : skipSetting.checked ? '已开启：需要邮箱验证码的账号会标记为已跳过，继续下一个，不生成 Token。'
      : '开启后跳过需要邮箱验证码的账号，继续处理下一个；当前等待中的账号也会跳过。';
    for (const entry of activeEntries.values()) updateCodeControls(entry);
  }

  function canSubmitBatch() {
    return !submitting && !confirmingProxy && !importing && !initializing && !clearing && !savingSkipSetting
      && !sequenceActive() && !cooldowns.remaining('submit') && !(batch && activeStates.has(batch.status));
  }

  async function confirmProxy() {
    const dialog = byId('proxy-confirm-dialog');
    if (document.querySelector('dialog[open]')) return false;
    const hasProxy = !!byId('proxy').value.trim();
    byId('proxy-confirm-description').textContent = hasProxy
      ? '已填写代理，本批次将通过该代理获取 Token。你可以修改代理，或确认后开始获取。'
      : '当前未填写代理，将使用直连获取 Token。是否需要先添加代理？';
    byId('proxy-confirm-edit').textContent = hasProxy ? '修改代理' : '先添加代理';
    byId('proxy-confirm-start').textContent = hasProxy ? '确认并开始' : '不添加，继续获取';
    dialog.returnValue = 'cancel';
    confirmingProxy = true;
    updateControls();
    let onClose;
    let choice;
    try {
      choice = await new Promise((resolve) => {
        onClose = () => resolve(dialog.returnValue);
        dialog.addEventListener('close', onClose, { once: true });
        dialog.showModal();
      });
    } catch (_) {
      showError('无法打开代理确认，请刷新页面后重试。');
    } finally {
      dialog.removeEventListener('close', onClose);
      confirmingProxy = false;
      updateControls();
    }
    if (choice === 'edit') {
      byId('proxy').closest('details').open = true;
      byId('proxy').focus();
    } else byId('start-button').focus({ preventScroll: true });
    return choice === 'start';
  }

  function updateCodeControls(entry) {
    const disabled = entry.submitting || entry.skipping || savingSkipSetting || clearing || cancelling || batch?.status === 'cancelling' || !entry.challengeId;
    const remaining = cooldowns.remaining('code');
    entry.input.disabled = disabled;
    entry.button.disabled = disabled || remaining > 0;
    entry.button.textContent = entry.submitting ? '验证中…' : remaining ? `${remaining} 秒后验证` : '验证';
    const skipRemaining = cooldowns.remaining('cancel');
    entry.skipButton.disabled = disabled || skipRemaining > 0 || entry.job.challenge?.kind !== 'email';
    entry.skipButton.textContent = entry.skipping ? '正在跳过…' : skipRemaining ? `${skipRemaining} 秒后可跳过` : '跳过此账号';
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
    const skipActions = document.createElement('div');
    skipActions.className = 'challenge-actions';
    skipActions.hidden = true;
    const skipButton = document.createElement('button');
    skipButton.type = 'button';
    skipButton.className = 'button button-secondary skip-email-button';
    skipButton.textContent = '跳过此账号';
    skipButton.setAttribute('aria-label', `跳过 ${job.email}，继续下一个账号`);
    const skipHelp = document.createElement('p');
    skipHelp.className = 'field-help';
    skipHelp.textContent = '结束此账号的提取，继续下一个。';
    skipActions.append(skipButton, skipHelp);
    const error = document.createElement('p');
    error.className = 'code-error';
    error.setAttribute('role', 'alert');
    error.hidden = true;
    form.append(title, description, label, row, skipActions, error);
    const details = document.createElement('details');
    details.className = 'progress-details';
    const summary = document.createElement('summary');
    summary.textContent = '运行记录';
    const events = document.createElement('ol');
    events.className = 'timeline';
    events.setAttribute('aria-label', `${job.email} 的运行记录`);
    details.append(summary, events);
    section.append(heading, message, form, details);
    const entry = { section, email, badge, message, form, title, description, input, button, skipButton, skipActions, error, events, job, challengeId: null, submitting: false, skipping: false, eventsSignature: '' };
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (entry.submitting || entry.skipping || savingSkipSetting || clearing || cancelling || batch?.status === 'cancelling' || !entry.challengeId || cooldowns.remaining('code')) return;
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
    skipButton.addEventListener('click', async () => {
      if (skipButton.disabled || entry.job.challenge?.kind !== 'email' || !entry.challengeId) return;
      const batchId = batch?.id;
      const challengeId = entry.challengeId;
      entry.skipping = true;
      error.hidden = true;
      updateCodeControls(entry);
      try {
        const skipped = await jsonRequest(`/api/jobs/${encodeURIComponent(job.id)}/skip`, { method: 'POST', body: JSON.stringify({ challenge_id: challengeId }) });
        if (batch?.id === batchId && !clearing) {
          pollRequest += 1;
          input.value = '';
          // Consume the challenge immediately; polling will update final counts after cleanup.
          batch.active_jobs = batch.active_jobs.map((current) => current.id === job.id ? skipped : current);
          renderActiveJobs(batch.active_jobs);
          schedulePoll(0);
        }
      } catch (failure) {
        if (activeEntries.get(job.id) === entry && entry.challengeId === challengeId) {
          error.textContent = failure.message;
          error.hidden = false;
          if (failure.status === 409) schedulePoll(0);
        }
      } finally {
        entry.skipping = false;
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
      entry.skipActions.hidden = challenge?.kind !== 'email';
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

  function refreshMissingResults(message) {
    if (resultStore.read()) return false;
    if (sequenceActive() && sequence.currentId() === batch?.id) stopSequence('当前组结果缓存已丢失，已停止后续分组，避免覆盖尚未完整接收的结果。');
    hideRt();
    // Cache loss does not establish whether the server task is still running.
    const active = activeStates.has(batch?.status);
    render(active ? batch : null);
    if (message) showError(message);
    if (active) schedulePoll(0);
    return true;
  }

  async function viewRt(account) {
    clearRt();
    rtAccount = { id: account.id, email: account.email };
    const dialog = byId('rt-dialog');
    byId('rt-view-email').textContent = account.email;
    if (!dialog.open) dialog.showModal();
    const record = resultStore.record(account.id);
    if (!record) {
      if (refreshMissingResults('本浏览器结果缓存已过期或被清除，当前没有可查看的 RT。')) return;
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
    const changedBatch = batch?.id !== currentBatch?.id;
    if (changedBatch) {
      hideRt();
      clearActiveJobs();
      if (currentBatch && activeStates.has(currentBatch.status)) byId('concurrency').value = String(currentBatch.concurrency || 2);
    }
    batch = currentBatch;
    if (batch && !savingSkipSetting && (changedBatch || activeStates.has(batch.status))) {
      byId('skip-email-verification').checked = batch.skip_email_verification === true;
    }
    document.dispatchEvent(new CustomEvent('rt:batch', { detail: batch }));
    updateControls();
    const counts = batch?.counts || { total: 0, queued: 0, running: 0, awaiting_code: 0, success: 0, error: 0, cancelled: 0 };
    const skipped = counts.skipped || 0;
    const completed = counts.success + counts.error + counts.cancelled + skipped;
    byId('count-total').textContent = counts.total.toLocaleString('zh-CN');
    byId('count-success').textContent = counts.success.toLocaleString('zh-CN');
    byId('count-error').textContent = counts.error.toLocaleString('zh-CN');
    byId('count-skipped').textContent = skipped.toLocaleString('zh-CN');
    byId('count-pending').textContent = (counts.queued + counts.running + (counts.awaiting_code || 0)).toLocaleString('zh-CN');
    byId('count-progress').textContent = `${completed} / ${counts.total}`;
    byId('batch-progress').max = counts.total || 1;
    byId('batch-progress').value = completed;
    let summary = '等待导入账号';
    if (batch) {
      if (batch.status === 'completed') summary = `处理完成：${counts.success} 条成功，${counts.error} 条失败，${skipped} 条跳过`;
      else if (batch.status === 'cancelled') summary = `已取消：保留 ${counts.success} 条成功结果，${counts.cancelled} 条未完成，${skipped} 条跳过`;
      else if (batch.status === 'cancelling') summary = '正在停止所有运行中的账号，已成功结果会保留';
      else if (batch.cooldown_seconds > 0) summary = `服务限流，暂停启动新账号，约 ${batch.cooldown_seconds} 秒后继续`;
      else summary = `正在提取 ${counts.running} 个 · 等待验证 ${counts.awaiting_code || 0} 个 · 排队 ${counts.queued} 个`;
    }
    byId('batch-summary').textContent = batch?.error || summary;
    const badge = byId('batch-badge');
    badge.textContent = batch ? batchLabels[batch.status] || '处理中' : '待开始';
    badge.className = `job-badge ${batch ? badgeClass(batch.status) : ''}`;
    if (batch?.status === 'completed') badge.classList.add(counts.error || skipped ? 'waiting' : 'success');
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
    if (notice) notice.textContent = !cached && counts.success > 0
      ? '成功数量是任务处理记录；当前浏览器结果缓存已过期或被清除，暂时没有可下载的结果。'
      : storageStatus.notice || '结果在本浏览器缓存中有效期为 30 分钟；页面运行或下次读取时清理过期缓存。服务器确认交接后即清除临时结果。';
    const clearButton = byId('clear-browser-results');
    if (clearButton) {
      clearButton.disabled = clearing || submitting || confirmingProxy || initializing || savingSkipSetting;
      clearButton.textContent = clearing ? '正在清空…' : cleanupRetry ? '重试清理临时任务' : sequenceActive() ? '停止分组并清空' : activeStates.has(batch?.status) ? '结束任务并清空' : '清空结果';
      clearButton.title = '清空本浏览器缓存并结束本会话的临时任务，其他用户不受影响。';
    }
    clearTimeout(expiryTimer);
    if (cached) expiryTimer = setTimeout(() => {
      if (!refreshMissingResults('本浏览器结果缓存已过期或被清除，当前没有可下载的结果。')) render(batch);
    }, Math.max(1, cached.expiresAt - Date.now()));
    renderSequence();
  }

  function schedulePoll(delay = 1000) {
    clearTimeout(pollTimer);
    if ((!document.hidden || canPollInBackground()) && batch && (activeStates.has(batch.status) || !isBatchCollected(batch.id))) {
      pollTimer = setTimeout(pollBatch, Math.max(delay, document.hidden ? 30_000 : 0, cooldowns.delay('poll')));
    }
  }

  async function pollBatch() {
    if (!batch || pollPending || (document.hidden && !canPollInBackground())) return;
    if (savingSkipSetting) { schedulePoll(); return; }
    if (cooldowns.remaining('poll')) { schedulePoll(); return; }
    clearTimeout(pollTimer);
    pollPending = true;
    let nextDelay = 1000;
    let retry = true;
    let collectingResults = false;
    const id = batch.id;
    const generation = ++pollRequest;
    try {
      const current = activeStates.has(batch.status)
        ? await jsonRequest(`/api/batches/${encodeURIComponent(id)}`) : batch;
      if (batch?.id !== id || generation !== pollRequest || clearing) return;
      collectingResults = true;
      const received = await collectBatchResults(current, jsonRequest);
      if (batch?.id !== id || generation !== pollRequest) return;
      if (batch?.id === id) {
        render(received || current);
        if (networkNotice) { showError(''); networkNotice = false; }
        await advanceSequence(received || current);
      }
    } catch (error) {
      if (batch?.id !== id || generation !== pollRequest || clearing) return;
      if (error.status === 404 && collectingResults) {
        // A failed result handoff does not prove the live task has disappeared.
        try {
          const current = await jsonRequest(`/api/batches/${encodeURIComponent(id)}`);
          if (batch?.id !== id || generation !== pollRequest || clearing) return;
          render(current);
          networkNotice = true;
          showError('账号进度已更新，但结果暂未同步，正在重试。');
          nextDelay = 3000;
          return;
        } catch (failure) {
          if (batch?.id !== id || generation !== pollRequest || clearing) return;
          error = failure;
        }
      }
      if (error.status === 404) {
        if (sequenceActive() && sequence.currentId() === id) stopSequence('当前组服务器任务已不存在，已停止后续分组。请下载已接收结果，核对未处理账号后重新导入。');
        const cached = resultStore.read();
        const lostActive = activeStates.has(batch.status)
          && (!cached || (cached.batch.id === id && activeStates.has(cached.batch.status)));
        const recovered = cachedBatchWithoutServer(resultStore, id);
        if (recovered?.id === id && !activeStates.has(recovered.status)) resultStore.markCollected(id);
        resetResultCollection();
        render(recovered);
        networkNotice = false;
        if (lostActive && (!recovered || recovered.id === id)) {
          showError(recovered
            ? '服务器已找不到该批次，可能已结束或过期。已接收的成功结果仍可下载；请重新导入未完成的账号。'
            : '服务器已找不到该批次，可能已结束或过期。当前浏览器没有可恢复的结果，请重新导入账号。');
        }
        retry = !!recovered && activeStates.has(recovered.status);
        return;
      }
      networkNotice = true;
      if (sequenceActive() && (error.status === 403 || (!error.status && /缓存|容量|不匹配/.test(error.message)))) {
        stopSequence(`当前组结果无法继续同步，已停止后续分组。${error.message}`);
      }
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
    const cached = resultStore.read();
    if (!cached) {
      refreshMissingResults('本浏览器结果缓存已过期或被清除，当前没有可下载的结果。');
      return;
    }
    downloadSnapshot(cached, kind, button, jobId);
  }

  function downloadSnapshot(cached, kind, button, jobId = null) {
    if (button.disabled) return;
    button.disabled = true;
    try {
      const { blob, filename } = resultDownload(cached, kind, jobId);
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url; link.download = filename;
      document.body.append(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) { render(batch); showError(error.message); }
    finally { button.disabled = false; }
  }

  byId('accounts-text').addEventListener('input', () => { updateCount(); byId('file-name').textContent = ''; updateControls(); });
  byId('skip-email-verification').addEventListener('change', async () => {
    const setting = byId('skip-email-verification');
    if (setting.disabled) return;
    sequence?.setSkip(setting.checked);
    if (!batch || !activeStates.has(batch.status)) { updateControls(); return; }
    const id = batch.id;
    const previous = batch.skip_email_verification === true;
    const enabled = setting.checked;
    savingSkipSetting = true;
    pollRequest += 1;
    showError('');
    updateControls();
    try {
      const updated = await jsonRequest(`/api/batches/${encodeURIComponent(id)}/settings`, { method: 'POST', body: JSON.stringify({ skip_email_verification: enabled }) });
      if (batch?.id === id && !clearing) {
        pollRequest += 1;
        setting.checked = updated.skip_email_verification === true;
        render(updated);
      }
    } catch (error) {
      if (batch?.id === id && !clearing) {
        setting.checked = previous;
        sequence?.setSkip(previous);
        showError(error.message);
      }
    } finally {
      savingSkipSetting = false;
      updateControls();
      schedulePoll(0);
    }
  });
  byId('clear-accounts-button').addEventListener('click', () => {
    if (byId('clear-accounts-button').disabled || byId('credentials-fields').disabled) return;
    byId('accounts-text').value = '';
    byId('accounts-file').value = '';
    byId('file-name').textContent = '';
    showError('');
    updateCount();
    updateControls();
    byId('accounts-text').focus();
  });
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
  window.addEventListener('pagehide', () => {
    hideRt();
    stopSequence('已离开此页面，自动分组已停止；未提交的账号需要重新导入。');
    clearTimeout(sequenceTimer);
  });
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
    if (!canSubmitBatch()) return;
    const count = inputCount();
    if (!count) { showError('请粘贴账号列表或导入 .txt 文件。'); return; }
    if (new Blob([byId('accounts-text').value]).size > maxFileBytes) { showError('输入超过 2 MiB，请拆分文件后导入。'); return; }
    hideRt();
    showError('');
    if (!(await confirmProxy()) || !canSubmitBatch()) return;
    if (workspaceWasCleared()) return;
    const body = { accounts_text: byId('accounts-text').value, timeout: 180, concurrency: Number(byId('concurrency').value), skip_email_verification: byId('skip-email-verification').checked === true };
    if (byId('proxy').value.trim()) body.proxy = byId('proxy').value.trim();
    if (count > maxAccounts) {
      if (sequence?.hasArchives()) { showError('请先下载并清空已有分组归档，再开始新的分组计划。'); return; }
      try {
        const { accounts_text, ...settings } = body;
        sequence = createBatchQueue(accounts_text, settings);
        sequenceGeneration += 1; sequenceArchiveSignature = '';
        byId('accounts-text').value = ''; byId('accounts-file').value = ''; byId('file-name').textContent = '';
        updateCount(); renderSequence(); await submitNextGroup();
      } catch (error) { showError(error.message); }
      finally { body.accounts_text = ''; body.proxy = ''; }
      return;
    }
    submitting = true;
    const generation = sequenceGeneration;
    networkNotice = false;
    showError('');
    updateControls();
    try {
      const accepted = await withBrowserResultLock(async () => {
        if (workspaceWasCleared() || generation !== sequenceGeneration) return null;
        const value = await jsonRequest('/api/batches', { method: 'POST', body: JSON.stringify(body) });
        if (workspaceWasCleared() || generation !== sequenceGeneration) return null;
        resultStore.save(value); return value;
      });
      if (!accepted || generation !== sequenceGeneration) return;
      byId('accounts-text').value = '';
      byId('accounts-file').value = '';
      byId('file-name').textContent = '';
      updateCount();
      resetResultCollection();
      render(accepted);
      schedulePoll();
    } catch (error) { if (generation === sequenceGeneration) showError(error.message, error.errors || []); }
    finally { body.accounts_text = ''; submitting = false; updateControls(); }
  });

  byId('cancel-button').addEventListener('click', async () => {
    if (submitting || cancelling || savingSkipSetting || batch?.status === 'cancelling') return;
    stopSequence('已停止分组计划，后续账号不会提交；已接收结果仍可按组下载。');
    if (!batch || !activeStates.has(batch.status)) { updateControls(); return; }
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
    if (clearing || submitting || confirmingProxy || savingSkipSetting) return;
    clearing = true; clearTimeout(pollTimer); clearTimeout(initializeTimer); pollRequest += 1;
    sequenceGeneration += 1; clearTimeout(sequenceTimer); sequence?.clear(); sequence = null; sequenceArchiveSignature = '';
    renderSequence();
    hideRt(); clearActiveJobs(); resetResultCollection(); render(null);
    initializing = false;
    byId('accounts-text').value = ''; byId('proxy').value = ''; updateCount();
    try {
      await withBrowserResultLock(async () => {
        resetResultCollection(); resultStore.clear();
        workspaceRevision = resultStore.clearRevision?.() || '';
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
  document.addEventListener('rt:browser-results-cleared', handleExternalClear);
  document.addEventListener('rt:browser-results', () => {
    hideRt();
    if (clearing || workspaceWasCleared()) return;
    const cached = resultStore.read();
    const replaced = stopReplacedSequence(cached);
    if (submitting && !replaced) return;
    if (!cached) {
      pollRequest += 1; initializing = false; clearTimeout(pollTimer); resetResultCollection();
      refreshMissingResults(batch ? '本浏览器结果缓存已过期或被清除，当前没有可下载的结果。' : '');
      return;
    }
    const snapshot = cached.batch;
    if (batch?.id === snapshot.id && activeStates.has(snapshot.status)) snapshot.active_jobs = batch.active_jobs || [];
    render(snapshot); schedulePoll();
  });
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      if (workspaceWasCleared()) return;
      if (batch) {
        if (!refreshMissingResults('本浏览器结果缓存已过期或被清除，当前没有可下载的结果。')) render(batch);
      }
      refreshCooldownControls();
      if (initializing) initialize(); else schedulePoll(0);
    } else if (canPollInBackground()) schedulePoll(30_000);
    else clearTimeout(pollTimer);
  });
  updateCount();
  const initialCache = resultStore.read();
  if (initialCache) render(initialCache.batch);
  initialize();
})();
