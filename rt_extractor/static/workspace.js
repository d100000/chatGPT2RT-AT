import { createRequestCooldowns, rateLimitError } from './request-limits.mjs';

(() => {
  'use strict';
  // Job state stays in the local service when moving between the three tools.
  const badge = document.getElementById('workspace-task-badge');
  if (!badge) return;
  const csrf = document.querySelector('meta[name="rt-csrf"]')?.content;
  let timer;
  let pending = false;
  const cooldowns = createRequestCooldowns({ now: () => Date.now() });
  async function refresh() {
    if (document.hidden || pending || !csrf) return;
    clearTimeout(timer);
    if (cooldowns.remaining('poll')) {
      timer = setTimeout(refresh, cooldowns.delay('poll'));
      return;
    }
    pending = true;
    try {
      const response = await fetch('/api/batches/current', {
        credentials: 'same-origin', cache: 'no-store', headers: { 'X-RT-CSRF': csrf },
      });
      if (response.status === 429) {
        const error = rateLimitError(response, await response.json().catch(() => ({})));
        cooldowns.hold('poll', error.retryAfter);
        badge.hidden = false;
        badge.textContent = '稍后更新';
        badge.title = error.message;
        badge.dataset.active = '';
        timer = setTimeout(refresh, cooldowns.delay('poll'));
        return;
      }
      if (!response.ok) throw new Error('unavailable');
      const { batch } = await response.json();
      const active = batch && !['completed', 'cancelled'].includes(batch.status);
      badge.hidden = !batch;
      badge.textContent = !batch ? '' : active ? '进行中' : `${batch.counts.success} 个成功`;
      badge.dataset.active = String(Boolean(active));
      badge.title = '';
      if (active) timer = setTimeout(refresh, 3000);
    } catch {
      badge.hidden = false;
      badge.textContent = '服务离线';
    } finally { pending = false; }
  }
  document.addEventListener('visibilitychange', refresh);
  // The extraction screen already polls its batch. Reuse the published update.
  document.addEventListener('rt:batch', (event) => {
    const batch = event.detail;
    badge.hidden = !batch;
    const active = batch && !['completed', 'cancelled'].includes(batch.status);
    badge.textContent = !batch ? '' : active ? '进行中' : `${batch.counts.success} 个成功`;
    badge.dataset.active = String(Boolean(active));
  });
  if (document.body.dataset.page !== 'extractor') refresh();
})();
