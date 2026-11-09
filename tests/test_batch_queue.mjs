import assert from 'node:assert/strict';
import test from 'node:test';
import { createBatchQueue, splitBatchInput, ARCHIVE_TTL, INPUT_BYTES } from '../rt_extractor/static/batch-queue.mjs';

const input = (count) => Array.from({ length: count }, (_, index) => `synthetic-${index}@example.com----synthetic-password----JBSWY3DPEHPK3PXP`).join('\n');
const settings = { concurrency: 10, timeout: 180, skip_email_verification: true, proxy: 'http://synthetic-secret@127.0.0.1:7890' };
function finished(id, count, successes = 1) {
  const rows = Array.from({ length: count }, (_, index) => ({ id: `${id}-${index}`, email: `synthetic-${index}@example.com`, status: index < successes ? 'success' : 'error' }));
  const batch = { id, status: 'completed', counts: { total: count, success: successes, error: count - successes, skipped: 0, cancelled: 0, running: 0, queued: 0, awaiting_code: 0 }, rows };
  return { batch, records: rows.filter((row) => row.status === 'success').map((row) => ({ id: row.id, raw: { refresh_token: 'synthetic-rt' } })) };
}

test('1001 and 2001 accounts split into ordered groups of at most 1000 without changing passwords', () => {
  assert.deepEqual(splitBatchInput(input(1001)).map((group) => group.count), [1000, 1]);
  assert.deepEqual(splitBatchInput(input(2001)).map((group) => group.count), [1000, 1000, 1]);
  const groups = splitBatchInput('\uFEFF\n test@example.com---- pass with spaces ----JBSWY3DPEHPK3PXP\r\n\n');
  assert.equal(groups[0].text, ' test@example.com---- pass with spaces ----JBSWY3DPEHPK3PXP');
});

test('all groups are checked for duplicate emails, basic field shape and the input byte limit before submission', () => {
  assert.throws(() => splitBatchInput(input(1000) + '\nSYNTHETIC-0@example.com----changed----JBSWY3DPEHPK3PXP'), /第 1001 行.*重复/);
  for (const line of ['bad line', 'x@example.com----pass----', 'x@example.com--------secret', 'not-email----pass----secret', 'x@example.com----pass----secret----extra']) {
    assert.throws(() => splitBatchInput(line), /第 1 行/);
  }
  assert.throws(() => splitBatchInput('a'.repeat(INPUT_BYTES + 1)), /2 MiB/);
});

test('only one group can be submitted at a time, and the next starts after complete records and ACK', () => {
  const queue = createBatchQueue(input(2001), settings);
  const first = queue.take();
  assert.equal(first.concurrency, 10); assert.equal(first.skip_email_verification, true);
  assert.equal(first.accounts_text.split('\n').length, 1000);
  assert.equal(queue.take(), null);
  const result = finished('first', 1000);
  queue.accept(result.batch);
  assert.equal(queue.finish(result.batch, result, false), false);
  assert.equal(queue.take(), null);
  assert.equal(queue.finish(result.batch, result, true), true);
  assert.equal(queue.archive(1).records[0].raw.refresh_token, 'synthetic-rt');
  const second = queue.take();
  assert.equal(second.accounts_text.split('\n')[0].startsWith('synthetic-1000@'), true);
  assert.equal(queue.snapshot().archives.length, 1);
  assert.doesNotMatch(JSON.stringify(queue.snapshot()), /synthetic-password|synthetic-secret|JBSWY3/);
});

test('quota retry waits exactly as instructed without consuming or advancing the group', () => {
  let now = 0;
  const queue = createBatchQueue(input(1001), settings, { now: () => now });
  const first = queue.take(); queue.wait(3600, '账号处理额度暂不可用');
  assert.equal(queue.snapshot().retrySeconds, 3600);
  assert.equal(queue.take(), null);
  now = 3_599_999; assert.equal(queue.take(), null);
  now = 3_600_000; assert.equal(queue.take().accounts_text, first.accounts_text);
  assert.equal(queue.snapshot().submitted, 0);
});

test('missing success records stop later groups instead of overwriting partial received results', () => {
  const queue = createBatchQueue(input(1001), settings);
  queue.take(); const result = finished('first', 1000, 2); queue.accept(result.batch);
  assert.equal(queue.finish(result.batch, { ...result, records: result.records.slice(0, 1) }, true), false);
  assert.equal(queue.snapshot().state, 'stopped');
  assert.match(queue.snapshot().message, /未完整接收/);
  assert.equal(queue.take(), null);
  assert.equal(queue.hasArchives(), false);
});

test('archives stay available during an hour quota wait and expire 30 minutes after stop', () => {
  let now = 0;
  const queue = createBatchQueue(input(1001), settings, { now: () => now });
  queue.take(); const first = finished('first', 1000); queue.accept(first.batch); queue.finish(first.batch, first, true);
  queue.take(); queue.wait(3600);
  now = 3_600_000;
  assert.ok(queue.archive(1));
  queue.stop('用户已停止');
  now += ARCHIVE_TTL - 1; assert.ok(queue.archive(1));
  now++; assert.equal(queue.archive(1), null);
  assert.equal(queue.snapshot().archives[0].available, false);
  assert.match(queue.snapshot().message, /已到期/);
  assert.equal(queue.take(), null);
});

test('archive capacity stops the queue and leaves the current group in the caller result store', () => {
  const queue = createBatchQueue(input(1001), settings, { maxArchiveBytes: 1 });
  queue.take(); const result = finished('first', 1000); queue.accept(result.batch);
  queue.finish(result.batch, result, true);
  assert.equal(queue.snapshot().state, 'stopped');
  assert.match(queue.snapshot().message, /20 MiB/);
  assert.equal(result.records.length, 1);
  assert.equal(queue.take(), null);
});

test('stop and clear erase queued submission access while preserving only requested archive lifetime', () => {
  const queue = createBatchQueue(input(1001), settings);
  queue.take(); const result = finished('first', 1000); queue.accept(result.batch);
  queue.stop(); queue.finish(result.batch, result, true);
  assert.ok(queue.archive(1)); assert.equal(queue.take(), null);
  queue.clear(); assert.equal(queue.archive(1), null); assert.equal(queue.take(), null);
  assert.equal(queue.snapshot().archives.length, 0);
});
