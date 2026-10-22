// Only this acknowledgement timestamp is retained; no account or Token data.
export const NOTICE_STORAGE_KEY = 'bob-ai-site-notice-acknowledged-at-v1';
export const NOTICE_INTERVAL_MS = 7 * 24 * 60 * 60 * 1000;
export const NOTICE_QQ_GROUP = '916444032';

function timestamp(value) {
  if (typeof value !== 'number' && (typeof value !== 'string' || !/^\d+$/.test(value))) return null;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null;
}

export function noticeIsDue(value, currentTime = Date.now()) {
  const acknowledgedAt = timestamp(value);
  return acknowledgedAt === null || !Number.isSafeInteger(currentTime)
    || acknowledgedAt > currentTime || currentTime - acknowledgedAt >= NOTICE_INTERVAL_MS;
}

function read(storage) {
  try { return storage?.getItem(NOTICE_STORAGE_KEY) ?? null; } catch { return null; }
}

function write(storage, value) {
  try {
    if (!storage) return false;
    storage.setItem(NOTICE_STORAGE_KEY, value);
    return true;
  } catch { return false; }
}

export function createNoticeMemory({ localStorage, sessionStorage, now = Date.now } = {}) {
  let acknowledgedInPage = null;
  return {
    isDue() {
      const currentTime = now();
      return [acknowledgedInPage, read(localStorage), read(sessionStorage)]
        .every((value) => noticeIsDue(value, currentTime));
    },
    acknowledge() {
      acknowledgedInPage = now();
      const value = String(acknowledgedInPage);
      if (write(localStorage, value)) return 'local';
      if (write(sessionStorage, value)) return 'session';
      return 'memory';
    },
  };
}

function browserStorage(windowRef, name) {
  try { return windowRef?.[name]; } catch { return undefined; }
}

const initialized = new WeakMap();

export function initSiteNotice({ documentRef = globalThis.document, windowRef = globalThis.window, now = Date.now } = {}) {
  const dialog = documentRef?.getElementById('site-notice-dialog');
  if (!dialog || typeof dialog.showModal !== 'function') return null;
  if (initialized.has(dialog)) return initialized.get(dialog);

  const memory = createNoticeMemory({
    localStorage: browserStorage(windowRef, 'localStorage'),
    sessionStorage: browserStorage(windowRef, 'sessionStorage'),
    now,
  });
  const note = documentRef.getElementById('site-notice-storage-note');
  let previousFocus;
  let acknowledgementHandled = false;

  function remember() {
    const persistence = memory.acknowledge();
    if (note && persistence !== 'local') {
      note.textContent = persistence === 'session'
        ? '浏览器未允许长期保存确认时间；本次标签页会话内不再提示。'
        : '浏览器未允许保存确认时间；关闭后仍可正常使用，下次进入可能再次提示。';
    }
    acknowledgementHandled = true;
  }

  function dismiss(rememberChoice = true) {
    if (!dialog.open) return;
    if (rememberChoice) remember();
    else acknowledgementHandled = true;
    dialog.close();
  }

  dialog.addEventListener('close', () => {
    if (!acknowledgementHandled) remember();
    const target = previousFocus?.isConnected && previousFocus !== documentRef.body
      ? previousFocus : documentRef.getElementById('main-content');
    target?.focus({ preventScroll: true });
  });
  dialog.addEventListener('cancel', (event) => { event.preventDefault(); dismiss(); });
  for (const id of ['site-notice-confirm', 'site-notice-close']) {
    documentRef.getElementById(id)?.addEventListener('click', () => dismiss());
  }

  // Acknowledging in another tab closes an existing notice without extending its
  // seven-day period or interrupting any other dialog on the page.
  windowRef?.addEventListener('storage', (event) => {
    if (event.key === NOTICE_STORAGE_KEY && !noticeIsDue(event.newValue, now())) dismiss(false);
  });
  documentRef.addEventListener('visibilitychange', () => {
    if (documentRef.visibilityState === 'visible' && dialog.open && !memory.isDue()) dismiss(false);
  });

  const copyButton = documentRef.getElementById('site-notice-copy-group');
  const copyStatus = documentRef.getElementById('site-notice-copy-status');
  copyButton?.addEventListener('click', async () => {
    copyButton.disabled = true;
    if (copyStatus) copyStatus.textContent = '';
    try {
      await windowRef.navigator.clipboard.writeText(NOTICE_QQ_GROUP);
      if (copyStatus) copyStatus.textContent = '群号已复制';
    } catch {
      if (copyStatus) copyStatus.textContent = `请手动复制群号 ${NOTICE_QQ_GROUP}`;
    } finally { copyButton.disabled = false; }
  });

  function showInitialNotice() {
    if (!memory.isDue() || dialog.open) return;
    previousFocus = documentRef.activeElement;
    acknowledgementHandled = false;
    dialog.showModal();
  }

  const controller = { dismiss, isDue: () => memory.isDue() };
  initialized.set(dialog, controller);
  // Do not stack this announcement over an existing credentials dialog.
  if (documentRef.querySelector('dialog[open]')) {
    const afterOtherDialog = () => {
      if (documentRef.querySelector('dialog[open]')) return;
      documentRef.removeEventListener('close', afterOtherDialog, true);
      showInitialNotice();
    };
    documentRef.addEventListener('close', afterOtherDialog, true);
  } else showInitialNotice();
  return controller;
}

if (typeof document !== 'undefined') initSiteNotice();
