import { activeTask } from './eval-ui.mjs';
const badge = document.getElementById('pelican-task-badge');
const csrf = document.querySelector('meta[name="rt-csrf"]')?.content;
let timer;
function show(tasks) {
  const active = tasks.find(activeTask);
  badge.hidden = !active;
  badge.textContent = active ? `${active.status === 'waiting' ? '等待' : '进行中'} · ${active.completed_requests} 次` : '';
  badge.dataset.active = String(Boolean(active));
}
async function refresh() {
  clearTimeout(timer);
  if (document.body.dataset.page === 'pelican' || document.hidden || !badge || !csrf) return;
  let delay = 15000;
  try {
    const response = await fetch('/api/pelican/tasks', { headers: { 'X-RT-CSRF': csrf }, credentials: 'same-origin', cache: 'no-store' });
    if (response.ok) show((await response.json()).tasks);
    else if (response.status === 429) delay = Number(response.headers.get('Retry-After') || 60) * 1000;
  } catch { /* The owning tool displays actionable connection errors. */ }
  timer = setTimeout(refresh, delay);
}
document.addEventListener('pelican:tasks', e => show(e.detail));
document.addEventListener('visibilitychange', refresh);
refresh();
