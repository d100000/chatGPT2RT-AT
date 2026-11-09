// Only the current page owns queued credentials and completed-group archives.
export const GROUP_SIZE = 1000;
export const INPUT_BYTES = 2 * 1024 * 1024;
export const ARCHIVE_BYTES = 20 * 1024 * 1024;
export const ARCHIVE_TTL = 30 * 60 * 1000;
const terminal = (batch) => ['completed', 'cancelled'].includes(batch?.status);
const processed = (counts = {}) => (counts.success || 0) + (counts.error || 0) + (counts.cancelled || 0) + (counts.skipped || 0);
const bytes = (value) => new TextEncoder().encode(value).length;

export function splitBatchInput(text) {
  if (typeof text !== 'string' || !text.trim()) throw new Error('请粘贴账号列表或导入 .txt 文件。');
  if (bytes(text) > INPUT_BYTES) throw new Error('输入超过 2 MiB，请拆分文件后导入。');
  const lines = text.replace(/^\uFEFF/, '').split(/\r\n|\n|\r/);
  const valid = [], seen = new Set();
  for (let index = 0; index < lines.length; index++) {
    const line = lines[index];
    if (!line.trim()) continue;
    const fields = line.split('----');
    if (fields.length !== 3 || fields.some((value) => !value.trim())) throw new Error(`第 ${index + 1} 行须包含邮箱、密码和 2FA 密钥，使用四个短横线分隔。`);
    const email = fields[0].trim().toLowerCase();
    if (!/^[^\s@]+@[^\s@]+$/.test(email)) throw new Error(`第 ${index + 1} 行的邮箱格式无效。`);
    if (seen.has(email)) throw new Error(`第 ${index + 1} 行的邮箱在本次导入中重复，请移除后重试。`);
    seen.add(email); valid.push(line);
  }
  if (!valid.length) throw new Error('请粘贴账号列表或导入 .txt 文件。');
  const groups = [];
  for (let index = 0; index < valid.length; index += GROUP_SIZE) {
    const selected = valid.slice(index, index + GROUP_SIZE);
    groups.push({ text: selected.join('\n'), count: selected.length, first: index + 1 });
  }
  return groups;
}

export function createBatchQueue(text, settings, { now = Date.now, maxArchiveBytes = ARCHIVE_BYTES } = {}) {
  let groups = splitBatchInput(text), configuration = { ...settings };
  const total = groups.reduce((sum, group) => sum + group.count, 0), groupCount = groups.length;
  let state = 'ready', next = 0, current = null, batchId = null, retryAt = 0, message = '';
  let archivedBytes = 0, expiresAt = 0;
  const archives = [];
  const active = () => ['ready', 'submitting', 'running', 'waiting'].includes(state);
  const eraseInput = () => { for (const group of groups) group.text = ''; configuration = {}; };
  const stop = (reason = '已停止自动分组，未提交的账号需要重新导入。') => {
    state = 'stopped'; message = reason; retryAt = 0; eraseInput(); expiresAt = now() + ARCHIVE_TTL;
  };
  function expireArchives() {
    if (!expiresAt || now() < expiresAt) return false;
    let changed = false;
    for (const archive of archives) if (archive.result) { archive.result = null; changed = true; }
    archivedBytes = 0;
    if (changed) message = '分组归档已到期并从本页内存清除，请使用此前下载的文件。';
    return changed;
  }
  return {
    active,
    currentId: () => batchId,
    hasArchives: () => { expireArchives(); return archives.some((archive) => archive.result); },
    setSkip(value) { if (active()) configuration.skip_email_verification = value === true; },
    take() {
      if (!['ready', 'waiting'].includes(state) || now() < retryAt || next >= groupCount || batchId) return null;
      current = next; state = 'submitting'; retryAt = 0;
      return { ...configuration, accounts_text: groups[current].text };
    },
    accept(batch) {
      if (state !== 'submitting' || !batch?.id || batch.counts?.total !== groups[current].count) throw new Error('提交结果与当前分组不匹配，已停止自动提交。');
      batchId = batch.id; groups[current].text = ''; next += 1; state = 'running'; message = '';
    },
    wait(seconds, reason) {
      if (state !== 'submitting') return;
      state = 'waiting'; retryAt = now() + Math.max(1, seconds) * 1000;
      message = reason || '服务额度暂不可用，正在按服务器要求等待。';
    },
    finish(batch, result, collected) {
      if (batch?.id !== batchId || !terminal(batch) || !collected) return false;
      const successIds = new Set(batch.rows.filter((row) => row.status === 'success').map((row) => row.id));
      if (result?.batch.id !== batchId || result.records.length !== batch.counts.success
          || successIds.size !== batch.counts.success || new Set(result.records.map((record) => record.id)).size !== successIds.size
          || result.records.some((record) => !successIds.has(record.id))) {
        stop('本组成功结果未完整接收，已停止后续分组。请先检查并下载当前可用结果。');
        return false;
      }
      const archived = { batch: result.batch, records: result.records };
      const size = bytes(JSON.stringify(archived));
      if (archivedBytes + size > maxArchiveBytes) {
        stop('分组归档达到 20 MiB 上限，已停止后续分组。当前组结果仍在下方，请下载全部可用结果后重新导入未处理账号。');
        return false;
      }
      archives.push({ number: current + 1, count: groups[current].count, processed: processed(batch.counts),
        counts: { ...batch.counts }, result: archived });
      archivedBytes += size; batchId = null; current = null;
      if (state === 'stopped' || batch.status === 'cancelled') stop(message || '当前组已取消，后续分组不会继续。');
      else if (next === groupCount) { state = 'completed'; eraseInput(); expiresAt = now() + ARCHIVE_TTL; }
      else state = 'ready';
      return true;
    },
    stop,
    clear() { eraseInput(); groups = []; archives.length = 0; archivedBytes = 0; state = 'cleared'; batchId = null; expiresAt = 0; },
    archive(number) { expireArchives(); return archives.find((archive) => archive.number === number)?.result || null; },
    snapshot(batch) {
      expireArchives();
      const done = archives.reduce((sum, archive) => sum + archive.processed, 0)
        + (batchId && batch?.id === batchId ? processed(batch.counts) : 0);
      return { state, total, groupCount, group: (current ?? Math.min(next, groupCount - 1)) + 1,
        processed: done, submitted: next, remainingGroups: groupCount - next,
        retryAt, retrySeconds: Math.max(0, Math.ceil((retryAt - now()) / 1000)), message,
        expiresAt, archiveBytes: archivedBytes,
        archives: archives.map(({ result, ...summary }) => ({ ...summary, available: !!result, batchId: result?.batch.id || null })) };
    },
  };
}
