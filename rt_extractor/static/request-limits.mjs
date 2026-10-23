// Browser feedback only. The server remains responsible for enforcing limits.
export const DEFAULT_RETRY_SECONDS = 60;

export function retryAfterSeconds(response, payload = {}, now = Date.now()) {
  const values = [];
  const header = response?.headers?.get?.('Retry-After');
  if (typeof header === 'string' && /^\d+(?:\.\d+)?$/.test(header.trim())) {
    values.push(Number(header));
  } else if (header) {
    const time = Date.parse(header);
    if (Number.isFinite(time)) values.push((time - now) / 1000);
  }
  if (typeof payload?.retry_after === 'number' || typeof payload?.retry_after === 'string') {
    if (/^\d+(?:\.\d+)?$/.test(String(payload.retry_after))) values.push(Number(payload.retry_after));
  }
  const valid = values.filter((value) => Number.isFinite(value) && value > 0 && value <= Number.MAX_SAFE_INTEGER / 1000);
  return valid.length ? Math.ceil(Math.max(...valid)) : DEFAULT_RETRY_SECONDS;
}

export function rateLimitError(response, payload = {}, now) {
  const retryAfter = retryAfterSeconds(response, payload, now);
  const message = typeof payload.error === 'string' && payload.error.trim()
    ? payload.error.trim() : '请求过于频繁，请稍后重试。';
  const error = new Error(`${message} 请在 ${retryAfter} 秒后重试。`);
  error.status = 429;
  error.retryAfter = retryAfter;
  error.errors = [];
  return error;
}

export function createRequestCooldowns({ now = Date.now } = {}) {
  const deadlines = new Map();
  return {
    hold(action, seconds) {
      if (!Number.isFinite(seconds) || seconds <= 0) return;
      deadlines.set(action, Math.max(deadlines.get(action) || 0, now() + seconds * 1000));
    },
    delay(action) { return Math.max(0, (deadlines.get(action) || 0) - now()); },
    remaining(action) { return Math.ceil(this.delay(action) / 1000); },
    active() { return [...deadlines.values()].some((deadline) => deadline > now()); },
  };
}

export function requestLimitAction(path, method = 'GET') {
  if (/\/cancel$/.test(path) || (path === '/api/workspace' && method.toUpperCase() === 'DELETE')) return 'cancel';
  if (method.toUpperCase() === 'POST' && /^\/api\/(?:jobs|batches)$/.test(path)) return 'submit';
  if (/\/ack$/.test(path)) return 'handoff-ack';
  if (/\/code$/.test(path)) return 'code';
  if (/\/(?:results|result|rt|download|rt-download|cpa-download|sub2api-download|conversion-source)$/.test(path)) return 'download';
  return 'poll';
}
