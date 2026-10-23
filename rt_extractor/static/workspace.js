import { createRequestCooldowns, rateLimitError, requestLimitAction } from './request-limits.mjs';
import { getBrowserResults, collectBatchResults, isBatchCollected, resetResultCollection, cachedBatchWithoutServer } from './browser-results.mjs';

(() => {
  'use strict';
  const badge = document.getElementById('workspace-task-badge');
  if (!badge) return;
  const csrf = document.querySelector('meta[name="rt-csrf"]')?.content;
  const store = getBrowserResults();
  let timer, pending = false, generation = 0;
  const cooldowns = createRequestCooldowns({ now: () => Date.now() });
  const extractionPage = document.body.dataset.page === 'extractor';
  function show(batch) {
    const active = batch && !['completed', 'cancelled'].includes(batch.status);
    const count = store.read()?.records.length || 0;
    badge.hidden = !batch;
    badge.textContent = !batch ? '' : active ? '进行中' : `${count} 个${store.status().persistence === 'memory' ? '在本页' : '已缓存'}`;
    badge.dataset.active = String(Boolean(active));
    badge.title = store.status().notice;
  }
  async function requestJSON(path, options = {}) {
    const action = requestLimitAction(path, options.method || 'GET');
    if (cooldowns.remaining(action)) throw rateLimitError(null, { retry_after: cooldowns.remaining(action) });
    const response = await fetch(path, {
      credentials: 'same-origin', cache: 'no-store', ...options,
      headers: { 'X-RT-CSRF': csrf, 'Content-Type': 'application/json' },
    });
    const payload = await response.json().catch(() => ({}));
    if (response.status === 429) {
      const error = rateLimitError(response, payload);
      cooldowns.hold(action, error.retryAfter); throw error;
    }
    if (!response.ok) { const error = new Error(payload.error || '会话服务暂不可用'); error.status = response.status; throw error; }
    return payload;
  }
  async function refresh() {
    if (extractionPage || document.hidden || pending || !csrf) return;
    clearTimeout(timer);
    if (cooldowns.remaining('poll')) { timer = setTimeout(refresh, cooldowns.delay('poll')); return; }
    pending = true;
    const currentGeneration = generation;
    const previousId = store.read()?.batch.id ?? null;
    try {
      const { batch } = await requestJSON('/api/batches/current');
      if (currentGeneration !== generation) return;
      const received = batch ? await collectBatchResults(batch, requestJSON) : null;
      if (currentGeneration !== generation) return;
      const current = received || batch || cachedBatchWithoutServer(store, previousId);
      show(current);
      const active = current && !['completed', 'cancelled'].includes(current.status);
      if (active || (batch && !isBatchCollected(batch.id))) timer = setTimeout(refresh, 3000);
      else if (current) timer = setTimeout(refresh, 30_000);
    } catch (error) {
      if (currentGeneration !== generation) return;
      badge.hidden = false;
      badge.textContent = error.status === 429 ? '稍后更新' : '等待重连';
      badge.title = error.message; badge.dataset.active = '';
      if (error.status !== 403) timer = setTimeout(refresh, error.status === 429 ? error.retryAfter * 1000 : 5000);
    } finally { pending = false; }
  }
  document.addEventListener('visibilitychange', refresh);
  document.addEventListener('rt:browser-results', () => {
    const cached = store.read();
    if (!cached) { generation += 1; clearTimeout(timer); resetResultCollection(); }
    show(cached?.batch);
  });
  document.addEventListener('rt:batch', (event) => show(event.detail));
  document.addEventListener('rt:results-cleared', () => {
    generation += 1; clearTimeout(timer); resetResultCollection(); show(null);
  });
  show(store.read()?.batch);
  if (!extractionPage) refresh();
})();
