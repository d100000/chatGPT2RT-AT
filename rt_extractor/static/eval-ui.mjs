export const activeTask = (task) => ['running', 'waiting', 'stopping'].includes(task.status);
export const numberText = (value, digits = 2) => value == null ? '未知' : Number(value).toLocaleString('zh-CN', { maximumFractionDigits: digits });
export const percentText = (value) => value == null ? '未知' : `${numberText(value * 100, 1)}%`;
export const timeText = (value) => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '未知';
export const statusText = (value) => ({ running: '生成中', waiting: '等待下一轮', stopping: '正在停止', success: '请求成功', failed: '失败', timeout: '超时', cancelled: '已取消', completed: '已完成', interrupted: '服务重启中断', storage_error: '历史保存失败，已停止' })[value] || value;
export function requestCount(questionCount, modelCount, scope, repeats, randomQuestions = false) {
  return (randomQuestions ? 1 : questionCount) * modelCount * (scope === 'batch' ? repeats : 1);
}
export function modelIds(value) { return [...new Set(value.split(/[\n,，]/).map(s => s.trim()).filter(Boolean))]; }
export function svgDataURL(svg) {
  const bytes = new TextEncoder().encode(svg);
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return `data:image/svg+xml;base64,${btoa(binary)}`;
}
export function reviewImage(sample, blind = false) {
  if (sample.question.animation) {
    if (blind) return sample.image?.startsWith('data:image/svg+xml;base64,') ? sample.image : null;
    return sample.technical?.safe && typeof sample.svg === 'string' && sample.svg.trim() ? svgDataURL(sample.svg) : null;
  }
  return blind ? sample.image : sample.render_evidence?.data_url;
}
export function filtersFrom(values) {
  const filters = {};
  for (const [key, value] of Object.entries(values)) {
    if (!value) continue;
    filters[key] = ['from', 'to'].includes(key) ? new Date(`${value}T${key === 'from' ? '00:00:00.000' : '23:59:59.999'}`).toISOString() : value;
  }
  return filters;
}
