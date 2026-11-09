/** Exercise the real extraction UI with synthetic batches and no network. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import * as requestLimits from '../rt_extractor/static/request-limits.mjs';

const settle = () => new Promise(setImmediate);
const response = (status, payload) => ({ ok: status >= 200 && status < 300, status,
  headers: { get: () => null }, json: async () => structuredClone(payload) });
const deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; };
const waitingBatch = (kind = 'email') => {
  const job = { id: 'synthetic-job', line: 1, email: 'synthetic@example.com', status: 'awaiting_code',
    challenge: { id: 'synthetic-challenge', kind }, events: [] };
  return { id: 'synthetic-batch', status: 'awaiting_code', concurrency: 1, skip_email_verification: false,
    counts: { total: 1, queued: 0, running: 0, awaiting_code: 1, success: 0, error: 0, cancelled: 0, skipped: 0 },
    rows: [job], active_jobs: [job] };
};
const skippedBatch = () => {
  const batch = waitingBatch(); batch.status = 'completed'; batch.counts.awaiting_code = 0; batch.counts.skipped = 1;
  batch.rows = [{ ...batch.rows[0], status: 'skipped', challenge: null, message: '已跳过需要邮箱验证的账号' }];
  batch.active_jobs = []; return batch;
};

async function createPage(initial = null, handlePost = async () => response(200, {})) {
  const ids = new Map(), created = [], requests = [], timers = new Map();
  let timerId = 0;
  let current = initial, cached = null;
  class Element {
    constructor(tag = 'div') {
      this.tagName = tag.toUpperCase(); this.value = ''; this.checked = false; this.disabled = false;
      this.hidden = false; this.open = false; this.children = []; this.ownText = ''; this.dataset = {};
      this.attributes = new Map(); this.listeners = new Map(); this.classList = { add() {}, remove() {}, toggle() {} };
    }
    set textContent(value) { this.ownText = String(value ?? ''); this.children = []; }
    get textContent() { return this.ownText + this.children.map((child) => child.textContent ?? child).join(''); }
    append(...children) { for (const child of children) { child.parentElement = this; this.children.push(child); } }
    replaceChildren(...children) { this.children = []; this.ownText = ''; this.append(...children); }
    remove() { if (this.parentElement) this.parentElement.children = this.parentElement.children.filter((child) => child !== this); }
    setAttribute(name, value) { this.attributes.set(name, value); }
    removeAttribute(name) { this.attributes.delete(name); }
    addEventListener(type, listener, options = {}) {
      const list = this.listeners.get(type) || []; list.push({ listener, once: !!options.once }); this.listeners.set(type, list);
    }
    removeEventListener(type, listener) { this.listeners.set(type, (this.listeners.get(type) || []).filter((entry) => entry.listener !== listener)); }
    async dispatch(type) {
      if (this.disabled && ['click', 'change'].includes(type)) return;
      for (const entry of [...(this.listeners.get(type) || [])]) {
        if (entry.once) this.removeEventListener(type, entry.listener);
        await entry.listener({ type, target: this, currentTarget: this, preventDefault() {} });
      }
    }
    focus() {} select() {}
    closest(selector) { return selector === 'details' ? byId('proxy-details') : null; }
    showModal() { this.open = true; }
    close(value) { this.open = false; if (value !== undefined) this.returnValue = value; return this.dispatch('close'); }
  }
  const byId = (id) => { if (!ids.has(id)) { const element = new Element(); element.id = id; ids.set(id, element); } return ids.get(id); };
  const document = { hidden: false, body: new Element('body'), getElementById: byId,
    createElement(tag) { const element = new Element(tag); created.push(element); return element; },
    createDocumentFragment: () => new Element(), addEventListener() {}, dispatchEvent() {},
    querySelector(selector) {
      if (selector === 'meta[name="rt-csrf"]') return { content: 'synthetic-csrf' };
      if (selector === 'dialog[open]') return [...ids.values()].find((element) => element.id.endsWith('-dialog') && element.open) || null;
      return null;
    } };
  byId('concurrency').value = '1';
  const store = { read: () => cached, save(value) { cached = { batch: structuredClone(value), records: [], expiresAt: Date.now() + 1_800_000 }; },
    status: () => ({ persistence: 'memory', notice: '' }), record: () => null, clear() { cached = null; } };
  const context = vm.createContext({ document, window: { addEventListener() {} }, navigator: {}, console, Date, Error, URL, Blob,
    CustomEvent: class { constructor(type, options = {}) { this.type = type; this.detail = options.detail; } },
    setTimeout(callback) { const id = ++timerId; timers.set(id, callback); return id; },
    clearTimeout(id) { timers.delete(id); }, ...requestLimits,
    getBrowserResults: () => store, collectBatchResults: async (batch) => batch, isBatchCollected: () => true,
    resetResultCollection() {}, resultDownload() { throw new Error('No synthetic Tokens'); },
    cachedBatchWithoutServer: () => cached?.batch || null, withBrowserResultLock: (operation) => operation(),
    async fetch(path, options = {}) {
      requests.push({ path, method: options.method || 'GET', body: options.body && JSON.parse(options.body) });
      if (path === '/api/batches/current') return response(200, { batch: current });
      if (path === '/api/batches/synthetic-batch' && !options.method) return response(200, current);
      return handlePost(path, options);
    },
  });
  const source = readFileSync(new URL('../rt_extractor/static/app.js', import.meta.url), 'utf8').replace(/^import[^\n]*\n/gm, '');
  vm.runInContext(source, context, { filename: 'app.js' }); await settle();
  return { byId, created, requests, setBatch(value) { current = value; },
    async poll() {
      const timer = [...timers.entries()].find(([, callback]) => callback.name === 'pollBatch');
      assert.ok(timer, 'the UI must schedule a state refresh'); timers.delete(timer[0]); await timer[1](); await settle();
    },
    find: (className) => created.find((element) => element.className?.split(' ').includes(className)),
    posts: () => requests.filter((request) => request.method === 'POST') };
}

test('skip preference defaults off, stays outside the locked credential fields, and submits with a new batch', async () => {
  const html = readFileSync(new URL('../rt_extractor/templates/index.html', import.meta.url), 'utf8');
  const fieldsetEnd = html.indexOf('</fieldset>', html.indexOf('id="credentials-fields"'));
  const settingPosition = html.indexOf('id="skip-email-verification"');
  assert.ok(settingPosition > fieldsetEnd, 'the live setting must remain operable during extraction');
  const page = await createPage(null, async (path) => {
    assert.equal(path, '/api/batches'); return response(202, skippedBatch());
  });
  assert.equal(page.byId('skip-email-verification').checked, false);
  page.byId('skip-email-verification').checked = true;
  await page.byId('skip-email-verification').dispatch('change');
  assert.equal(page.posts().length, 0);
  page.byId('accounts-text').value = 'synthetic@example.com----synthetic-password----JBSWY3DPEHPK3PXP';
  const pending = page.byId('batch-form').dispatch('submit');
  await page.byId('proxy-confirm-dialog').close('start'); await pending;
  assert.equal(page.posts().length, 1);
  assert.equal(page.posts()[0].body.skip_email_verification, true);
});

test('a running batch setting saves once, disables while pending, and rolls back after a failed update', async () => {
  const gate = deferred();
  const page = await createPage(waitingBatch(), () => gate.promise);
  const toggle = page.byId('skip-email-verification');
  assert.equal(page.byId('credentials-fields').disabled, true);
  assert.equal(toggle.disabled, false);
  toggle.checked = true; const pending = toggle.dispatch('change');
  assert.equal(toggle.disabled, true);
  assert.deepEqual(page.posts(), [{ path: '/api/batches/synthetic-batch/settings', method: 'POST', body: { skip_email_verification: true } }]);
  gate.resolve(response(409, { error: '设置更新失败' })); await pending;
  assert.equal(toggle.checked, false); assert.equal(toggle.disabled, false);
  assert.match(page.byId('form-error-message').textContent, /设置更新失败/);
});

test('enabling the active preference accepts the server state and shows skipped completion separately from failure', async () => {
  const finished = skippedBatch(); finished.skip_email_verification = true;
  const page = await createPage(waitingBatch(), async () => response(200, finished));
  page.byId('skip-email-verification').checked = true;
  await page.byId('skip-email-verification').dispatch('change');
  assert.equal(page.byId('skip-email-verification').checked, true);
  assert.equal(page.byId('count-skipped').textContent, '1');
  assert.equal(page.byId('count-error').textContent, '0');
  assert.equal(page.byId('count-progress').textContent, '1 / 1');
  assert.match(page.byId('results-body').textContent, /已跳过/);
});

test('manual email skip needs no code and prevents simultaneous verification while it is pending', async () => {
  const gate = deferred();
  const page = await createPage(waitingBatch(), () => gate.promise);
  const skip = page.find('skip-email-button');
  const input = page.created.find((element) => element.id === 'challenge-code-synthetic-job');
  const form = page.find('challenge');
  const verify = page.created.find((element) => element.type === 'submit');
  assert.equal(skip.textContent, '跳过此账号'); assert.equal(input.value, ''); assert.equal(skip.type, 'button');
  const pending = skip.dispatch('click');
  assert.equal(skip.disabled, true); assert.equal(verify.disabled, true); assert.equal(input.disabled, true);
  input.value = '123456'; await form.dispatch('submit');
  assert.deepEqual(page.posts(), [{ path: '/api/jobs/synthetic-job/skip', method: 'POST', body: { challenge_id: 'synthetic-challenge' } }]);
  page.setBatch(skippedBatch()); gate.resolve(response(200, { ...waitingBatch().rows[0], status: 'running', challenge: null }));
  await pending;
  assert.equal(form.hidden, true, 'the consumed challenge must disappear before the next poll');
  await page.poll();
  assert.equal(page.byId('count-skipped').textContent, '1');
});

test('TOTP challenges cannot be skipped and submitting a code disables the email skip action', async () => {
  const totp = await createPage(waitingBatch('totp'));
  const hiddenSkip = totp.find('skip-email-button');
  assert.ok(hiddenSkip.hidden || hiddenSkip.parentElement.hidden, 'skip must not be offered for Authenticator verification');
  await hiddenSkip.dispatch('click'); assert.equal(totp.posts().length, 0);
  const gate = deferred();
  const page = await createPage(waitingBatch(), () => gate.promise);
  const input = page.created.find((element) => element.id === 'challenge-code-synthetic-job');
  input.value = '123456';
  const pending = page.find('challenge').dispatch('submit');
  assert.equal(page.find('skip-email-button').disabled, true);
  await page.find('skip-email-button').dispatch('click'); assert.equal(page.posts().length, 1);
  assert.equal(page.posts()[0].path, '/api/jobs/synthetic-job/code');
  gate.resolve(response(409, { error: '验证码请求已失效' })); await pending;
});

test('skip and settings share the control cooldown without repeated requests', async () => {
  assert.equal(requestLimits.requestLimitAction('/api/jobs/synthetic-job/skip', 'POST'), 'cancel');
  assert.equal(requestLimits.requestLimitAction('/api/batches/synthetic-batch/settings', 'POST'), 'cancel');
  const page = await createPage(waitingBatch(), async () => response(429, { retry_after: 30 }));
  await page.find('skip-email-button').dispatch('click');
  assert.equal(page.find('skip-email-button').disabled, true);
  assert.equal(page.byId('skip-email-verification').disabled, true);
  await page.find('skip-email-button').dispatch('click');
  assert.equal(page.posts().length, 1);
});
