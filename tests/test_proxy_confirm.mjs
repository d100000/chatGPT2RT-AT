/** Exercise the real extraction submit handler without a browser or network. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import * as requestLimits from '../rt_extractor/static/request-limits.mjs';

const ACCOUNTS = 'synthetic@example.com----synthetic-password-do-not-cache----JBSWY3DPEHPK3PXP';

async function createPage() {
  const elements = new Map();
  const posts = [];
  const saves = [];
  let cached = null;
  let document;

  class Element {
    constructor(id = '') {
      this.id = id;
      this.value = '';
      this.textContent = '';
      this.disabled = false;
      this.hidden = false;
      this.open = false;
      this.returnValue = '';
      this.children = [];
      this.isConnected = true;
      this.dataset = {};
      this.showCalls = 0;
      this.listeners = new Map();
      this.classList = { add() {}, remove() {}, toggle() {} };
    }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
    setAttribute() {}
    removeAttribute() {}
    addEventListener(type, callback, options = {}) {
      const entries = this.listeners.get(type) || [];
      entries.push({ callback, once: Boolean(options.once) });
      this.listeners.set(type, entries);
    }
    removeEventListener(type, callback) {
      this.listeners.set(type, (this.listeners.get(type) || []).filter((entry) => entry.callback !== callback));
    }
    async dispatch(type) {
      const event = {
        type, target: this, currentTarget: this, defaultPrevented: false,
        preventDefault() { this.defaultPrevented = true; },
      };
      for (const entry of [...(this.listeners.get(type) || [])]) {
        if (entry.once) this.removeEventListener(type, entry.callback);
        await entry.callback(event);
      }
      return event;
    }
    focus() { document.activeElement = this; }
    closest(selector) { return selector === 'details' && this.id === 'proxy' ? byId('proxy-details') : null; }
    showModal() { this.open = true; this.showCalls += 1; }
    close(value) {
      if (!this.open) return Promise.resolve();
      if (value !== undefined) this.returnValue = value;
      this.open = false;
      return this.dispatch('close');
    }
  }

  const byId = (id) => {
    if (!elements.has(id)) elements.set(id, new Element(id));
    return elements.get(id);
  };
  const documentEvents = new Element('document');
  document = {
    hidden: false,
    body: new Element('body'),
    getElementById: byId,
    createElement: () => new Element(),
    createDocumentFragment: () => new Element(),
    addEventListener: (...args) => documentEvents.addEventListener(...args),
    dispatchEvent: () => true,
    querySelector(selector) {
      if (selector === 'meta[name="rt-csrf"]') return { content: 'synthetic-csrf' };
      if (selector === 'dialog[open]') return [...elements.values()].find((element) => element.id.endsWith('-dialog') && element.open) || null;
      return null;
    },
  };
  document.activeElement = document.body;
  byId('concurrency').value = '2';
  const resultStore = {
    read: () => cached,
    save(value) {
      saves.push(JSON.parse(JSON.stringify(value)));
      cached = { batch: value, records: [], expiresAt: Date.now() + 1_800_000 };
    },
    status: () => ({ persistence: 'memory', notice: '' }),
    record: () => null,
    clear() { cached = null; },
  };
  const source = readFileSync(new URL('../rt_extractor/static/app.js', import.meta.url), 'utf8')
    .replace(/^import[^\n]*\n/gm, '');
  const context = vm.createContext({
    document,
    window: { addEventListener() {} },
    navigator: {},
    console, Date, Error, URL, Blob,
    CustomEvent: class { constructor(type, options = {}) { this.type = type; this.detail = options.detail; } },
    setTimeout: () => 1,
    clearTimeout() {},
    ...requestLimits,
    getBrowserResults: () => resultStore,
    collectBatchResults: async (batch) => batch,
    isBatchCollected: () => true,
    resetResultCollection() {},
    resultDownload() { throw new Error('Downloads are outside this test'); },
    cachedBatchWithoutServer: () => cached?.batch || null,
    withBrowserResultLock: async (callback) => callback(),
    async fetch(path, options = {}) {
      if (path === '/api/batches/current') return { ok: true, json: async () => ({ batch: null }) };
      if (path === '/api/batches' && options.method === 'POST') {
        posts.push(JSON.parse(options.body));
        return {
          ok: true,
          json: async () => ({
            id: `synthetic-batch-${posts.length}`, status: 'completed', concurrency: 2,
            rows: [], active_jobs: [],
            counts: { total: 0, success: 0, error: 0, cancelled: 0, queued: 0, running: 0 },
          }),
        };
      }
      throw new Error(`Unexpected request: ${path}`);
    },
  });
  vm.runInContext(source, context, { filename: 'app.js' });
  await new Promise(setImmediate);
  assert.equal(byId('start-button').disabled, false, 'page initialization must finish');
  byId('accounts-text').value = ACCOUNTS;

  return {
    byId, posts, saves, document,
    submit: () => byId('batch-form').dispatch('submit'),
    choose: (value) => byId('proxy-confirm-dialog').close(value),
    async escape() {
      const dialog = byId('proxy-confirm-dialog');
      const event = await dialog.dispatch('cancel');
      if (!event.defaultPrevented) await dialog.close();
    },
  };
}

test('opening or cancelling proxy confirmation never submits or caches credentials', async () => {
  const page = await createPage();
  const pending = page.submit();
  assert.equal(page.byId('proxy-confirm-dialog').open, true);
  assert.equal(page.posts.length, 0);
  assert.equal(page.saves.length, 0);
  await page.choose('cancel');
  await pending;
  assert.equal(page.posts.length, 0);
  assert.equal(page.saves.length, 0);
  assert.equal(page.byId('accounts-text').value, ACCOUNTS);
  assert.equal(page.byId('start-button').disabled, false);
});

test('editing proxy returns to its field, then explicit confirmation submits current input once', async () => {
  const page = await createPage();
  const first = page.submit();
  await page.choose('edit');
  await first;
  assert.equal(page.posts.length, 0);
  assert.equal(page.saves.length, 0);
  assert.equal(page.byId('proxy-details').open, true);
  assert.equal(page.document.activeElement, page.byId('proxy'));
  assert.equal(page.byId('proxy').disabled, false);
  assert.equal(page.byId('accounts-text').value, ACCOUNTS);

  const proxy = 'http://synthetic-user:synthetic-proxy-password@127.0.0.1:7890';
  page.byId('proxy').value = proxy;
  const pending = page.submit();
  assert.equal(page.byId('proxy-confirm-dialog').open, true, 'configured proxy still requires confirmation');
  const description = page.byId('proxy-confirm-description').textContent;
  assert.ok(description.length > 0);
  assert.equal(description.includes(proxy), false);
  assert.equal(description.includes('synthetic-proxy-password'), false);
  assert.equal(page.posts.length, 0);
  assert.equal(page.saves.length, 0);
  await page.choose('start');
  await pending;
  assert.equal(page.posts.length, 1);
  assert.equal(page.posts[0].accounts_text, ACCOUNTS);
  assert.equal(page.posts[0].proxy, proxy);
  assert.equal(page.posts[0].concurrency, 2);
  assert.equal(page.byId('accounts-text').value, '');
  assert.equal(JSON.stringify(page.saves).includes('synthetic-password-do-not-cache'), false);
  assert.equal(JSON.stringify(page.saves).includes('synthetic-proxy-password'), false);
});

test('repeated submit opens one dialog and Escape cannot reuse an earlier start decision', async () => {
  const page = await createPage();
  const first = page.submit();
  await page.choose('start');
  await first;
  assert.equal(page.posts.length, 1);
  assert.equal(Object.hasOwn(page.posts[0], 'proxy'), false);

  page.byId('accounts-text').value = ACCOUNTS;
  const pending = page.submit();
  await page.submit();
  assert.equal(page.byId('proxy-confirm-dialog').showCalls, 2);
  assert.equal(page.byId('proxy-confirm-dialog').returnValue, 'cancel');
  await page.escape();
  await pending;
  assert.equal(page.posts.length, 1);
  assert.equal(page.byId('accounts-text').value, ACCOUNTS);
  assert.equal(page.byId('start-button').disabled, false);
});
