import assert from 'node:assert/strict';
import test from 'node:test';
import {
  NOTICE_STORAGE_KEY, NOTICE_INTERVAL_MS, NOTICE_QQ_GROUP,
  noticeIsDue, createNoticeMemory, initSiteNotice,
} from '../rt_extractor/static/site-notice.mjs';

const NOW = Date.parse('2026-10-22T12:00:00Z');

function storage(initial) {
  const values = new Map(initial === undefined ? [] : [[NOTICE_STORAGE_KEY, initial]]);
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    entries: () => [...values.entries()],
  };
}

test('a rolling seven-day window suppresses repeated entry and expires at the exact boundary', () => {
  assert.equal(noticeIsDue(null, NOW), true);
  assert.equal(noticeIsDue(String(NOW), NOW), false);
  assert.equal(noticeIsDue(String(NOW), NOW + NOTICE_INTERVAL_MS - 1), false);
  assert.equal(noticeIsDue(String(NOW), NOW + NOTICE_INTERVAL_MS), true);
});

test('corrupt, missing, non-integer, and future timestamps cannot hide the declaration', () => {
  for (const value of [undefined, '', ' ', 'bad', 'null', '{}', '[]', '1e12', true, {}, [], -1, 0, NaN, Infinity, 1.5, String(NOW + 1)]) {
    assert.equal(noticeIsDue(value, NOW), true, `invalid value: ${String(value)}`);
  }
});

test('acknowledgement persists only a timestamp and is reused by other tools in the same browser', () => {
  const localStorage = storage();
  const firstTool = createNoticeMemory({ localStorage, now: () => NOW });
  assert.equal(firstTool.isDue(), true);
  assert.equal(firstTool.acknowledge(), 'local');
  assert.deepEqual(localStorage.entries(), [[NOTICE_STORAGE_KEY, String(NOW)]]);
  const anotherTool = createNoticeMemory({ localStorage, now: () => NOW + 1000 });
  assert.equal(anotherTool.isDue(), false);
  const nextWeek = createNoticeMemory({ localStorage, now: () => NOW + NOTICE_INTERVAL_MS });
  assert.equal(nextWeek.isDue(), true);
});

test('an acknowledgement of the old server-file policy cannot suppress the new privacy declaration', () => {
  const localStorage = storage();
  localStorage.setItem('bob-ai-site-notice-acknowledged-at-v1', String(NOW));
  const memory = createNoticeMemory({ localStorage, now: () => NOW + 1000 });
  assert.equal(memory.isDue(), true);
  memory.acknowledge();
  assert.equal(memory.isDue(), false);
});

test('storage read errors fall back to session acknowledgement without blocking the page', () => {
  const blocked = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); } };
  const sessionStorage = storage();
  const current = createNoticeMemory({ localStorage: blocked, sessionStorage, now: () => NOW });
  assert.equal(current.isDue(), true);
  assert.equal(current.acknowledge(), 'session');
  assert.equal(createNoticeMemory({ localStorage: blocked, sessionStorage, now: () => NOW + 1 }).isDue(), false);
});

test('a fresh session acknowledgement takes precedence over a readable but expired local record', () => {
  const localStorage = storage(String(NOW - NOTICE_INTERVAL_MS));
  localStorage.setItem = () => { throw new Error('quota exceeded'); };
  const sessionStorage = storage();
  const current = createNoticeMemory({ localStorage, sessionStorage, now: () => NOW });
  assert.equal(current.isDue(), true);
  assert.equal(current.acknowledge(), 'session');
  assert.equal(createNoticeMemory({ localStorage, sessionStorage, now: () => NOW + 1 }).isDue(), false);
});

test('when all writes fail the notice can still be acknowledged for the current page', () => {
  const full = { getItem() { return null; }, setItem() { throw new Error('quota exceeded'); } };
  const memory = createNoticeMemory({ localStorage: full, sessionStorage: full, now: () => NOW });
  assert.equal(memory.isDue(), true);
  assert.equal(memory.acknowledge(), 'memory');
  assert.equal(memory.isDue(), false);
});

function page({ localStorage = storage(), sessionStorage = storage(), now = () => NOW, clipboard = async () => {}, blockedStorageGetters = false } = {}) {
  class Target extends EventTarget {}
  class Element extends Target {
    open = false;
    isConnected = true;
    disabled = false;
    textContent = '';
    focusCalls = 0;
    focus() { this.focusCalls += 1; }
    showModal() { this.open = true; }
    close() { this.open = false; this.dispatchEvent(new Event('close')); }
    click() { this.dispatchEvent(new Event('click')); }
  }
  const ids = Object.fromEntries(['site-notice-dialog', 'site-notice-confirm', 'site-notice-close', 'site-notice-storage-note', 'site-notice-copy-group', 'site-notice-copy-status', 'main-content'].map((id) => [id, new Element()]));
  const documentRef = Object.assign(new Target(), {
    body: new Element(), visibilityState: 'visible',
    getElementById: (id) => ids[id] ?? null,
    querySelector: () => null,
  });
  documentRef.activeElement = documentRef.body;
  const windowRef = Object.assign(new Target(), { localStorage, sessionStorage, navigator: { clipboard: { writeText: clipboard } } });
  if (blockedStorageGetters) {
    Object.defineProperties(windowRef, {
      localStorage: { get() { throw new Error('denied'); } },
      sessionStorage: { get() { throw new Error('denied'); } },
    });
  }
  const controller = initSiteNotice({ documentRef, windowRef, now });
  return { ids, documentRef, windowRef, controller, localStorage };
}

test('confirm, close, and Escape each acknowledge only this notice and restore workspace focus', () => {
  for (const action of ['confirm', 'close', 'escape']) {
    const current = page();
    assert.equal(current.ids['site-notice-dialog'].open, true);
    if (action === 'escape') {
      const cancel = new Event('cancel', { cancelable: true });
      current.ids['site-notice-dialog'].dispatchEvent(cancel);
      assert.equal(cancel.defaultPrevented, true);
    } else current.ids[`site-notice-${action}`].click();
    assert.equal(current.ids['site-notice-dialog'].open, false);
    assert.equal(current.ids['main-content'].focusCalls, 1);
    assert.equal(current.localStorage.getItem(NOTICE_STORAGE_KEY), String(NOW));
  }
});

test('a fresh acknowledgement from another tab closes this dialog without rewriting its timestamp', () => {
  const current = page();
  const acknowledgedAt = NOW - 1000;
  current.localStorage.setItem(NOTICE_STORAGE_KEY, String(acknowledgedAt));
  const event = new Event('storage');
  Object.assign(event, { key: NOTICE_STORAGE_KEY, newValue: String(acknowledgedAt) });
  current.windowRef.dispatchEvent(event);
  assert.equal(current.ids['site-notice-dialog'].open, false);
  assert.equal(current.localStorage.getItem(NOTICE_STORAGE_KEY), String(acknowledgedAt));
});

test('returning to an already acknowledged tab never opens a surprise notice at seven days', () => {
  let currentTime = NOW;
  const localStorage = storage(String(NOW));
  const current = page({ localStorage, now: () => currentTime });
  assert.equal(current.ids['site-notice-dialog'].open, false);
  currentTime += NOTICE_INTERVAL_MS;
  current.documentRef.dispatchEvent(new Event('visibilitychange'));
  assert.equal(current.ids['site-notice-dialog'].open, false);
});

test('copying the public group number does not acknowledge the notice and handles clipboard denial', async () => {
  let copied;
  const current = page({ clipboard: async (value) => { copied = value; } });
  current.ids['site-notice-copy-group'].click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(copied, NOTICE_QQ_GROUP);
  assert.equal(current.ids['site-notice-copy-status'].textContent, '群号已复制');
  assert.equal(current.localStorage.getItem(NOTICE_STORAGE_KEY), null);
  assert.equal(current.ids['site-notice-dialog'].open, true);
  const denied = page({ clipboard: async () => { throw new Error('denied'); } });
  denied.ids['site-notice-copy-group'].click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.match(denied.ids['site-notice-copy-status'].textContent, /请手动复制群号 916444032/);
  assert.equal(denied.ids['site-notice-copy-group'].disabled, false);
});

test('unavailable storage getters do not prevent closing the global notice', () => {
  const current = page({ blockedStorageGetters: true });
  const dialog = current.ids['site-notice-dialog'];
  assert.equal(dialog.open, true);
  current.controller.dismiss();
  assert.equal(dialog.open, false);
  assert.match(current.ids['site-notice-storage-note'].textContent, /下次进入可能再次提示/);
});

test('initializing the same dialog twice does not register duplicate handlers', () => {
  const current = page();
  assert.equal(initSiteNotice({ documentRef: current.documentRef, windowRef: current.windowRef, now: () => NOW }), current.controller);
  current.ids['site-notice-close'].click();
  assert.equal(current.ids['main-content'].focusCalls, 1);
});
