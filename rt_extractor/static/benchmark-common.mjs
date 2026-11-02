export const active = value => ['running', 'stopping'].includes(value?.status);
export const statusName = value => ({idle:'待开始',pending:'待执行',running:'运行中',stopping:'停止中',stopped:'已停止',completed:'已完成',error:'失败',interrupted:'异常中断',success:'成功',cancelled:'已取消'}[value] || value || '—');
export const modeName = value => value === 'unique' ? '随机输入' : '共享前缀';
export const esc = value => String(value ?? '—').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export const num = (value, digits=1) => value == null ? '—' : Number(value).toLocaleString('zh-CN',{maximumFractionDigits:digits});
export const pct = value => value == null ? '—' : `${num(value*100)}%`;
export const stamp = value => value ? new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}) : '—';
export async function api(path, options={}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), options.timeout || 35000);
  const signal = options.signal || controller.signal;
  try {
    const response = await fetch(path, {credentials:'same-origin', cache:'no-store', ...options, signal,
      headers:{'X-RT-CSRF':document.querySelector('meta[name="rt-csrf"]')?.content || '', 'Content-Type':'application/json', ...options.headers}});
    if (!response.ok) {
      const data = await response.json().catch(()=>({}));
      const error = new Error(data.error || `本地服务 HTTP ${response.status}`); error.status=response.status;
      error.retryAfter = Number(response.headers.get('Retry-After') || 0); throw error;
    }
    return options.download ? response.blob() : response.json();
  } finally { clearTimeout(timer); }
}
export function parseLevels(value) {
  const pieces = String(value).trim().split(/[,，\s]+/);
  if (!pieces.length || pieces.length>8 || pieces.some(x=>!/^\d+$/.test(x) || Number(x)<1 || Number(x)>200)) throw new Error('并发档位须为 1–200 的整数，最多输入 8 档。');
  return [...new Set(pieces.map(Number))].sort((a,b)=>a-b);
}
export function calculatePlan(config) {
  const m=config.models.length, k=config.modes.length, c=config.concurrencyLevels;
  const measured=m*k*c.reduce((sum,x)=>sum+Math.max(config.requestsPerStage,x),0);
  const warmup=config.modes.includes('shared-prefix') ? m*c.length : 0;
  return {models:m,modes:k,levels:c.length,stages:m*k*c.length,measuredPlanned:measured,warmupPlanned:warmup,planned:measured+warmup};
}
export function validURL(value) {
  let url;
  try { url = new URL(value); } catch { throw new Error('请输入有效的 HTTP(S) 接口 URL。'); }
  if (!['http:','https:'].includes(url.protocol) || url.username || url.password || value.includes('#') || /[\s\\]/.test(value)) throw new Error('URL 不得包含用户名、密码、片段或空白。');
  for (let name of url.searchParams.keys()) { try { name=decodeURIComponent(name); } catch {} if (/(key|token|auth|secret|credential|signature|password)/i.test(name.replace(/[^a-z0-9]/gi,'')) || /^(sig|jwt)$/i.test(name)) throw new Error('URL 查询参数不能包含鉴权信息。'); }
  return url;
}
