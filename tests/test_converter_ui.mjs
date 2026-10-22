/**
 * Exercise the actual converter page's registered input/click handlers with a
 * minimal DOM. This harness models text, controls, and events only; responsive
 * layout, browser downloads, and native dialogs are verified in the browser.
 */
import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import * as converter from '../rt_extractor/static/converter.mjs';
import * as archive from '../rt_extractor/static/archive.mjs';
import * as pasteParser from '../rt_extractor/static/paste-input.mjs';

class Element {
  constructor(tagName = 'div') {
    this.tagName = tagName.toUpperCase();
    this.value = '';
    this.disabled = false;
    this.hidden = false;
    this.open = false;
    this.children = [];
    this.listeners = new Map();
    this.attributes = new Map();
    this.classList = { add() {}, remove() {} };
    this.ownText = '';
  }

  set textContent(value) { this.ownText = String(value ?? ''); this.children = []; }
  get textContent() { return this.ownText + this.children.map((child) => child.textContent).join(''); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; this.ownText = ''; }
  setAttribute(name, value) { this.attributes.set(name, value); }
  getAttribute(name) { return this.attributes.get(name); }
  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }
  querySelectorAll(selector) {
    return this.children.flatMap((child) => [
      ...(child.tagName?.toLowerCase() === selector ? [child] : []),
      ...child.querySelectorAll(selector),
    ]);
  }
  async dispatch(type) {
    if (type === 'click' && this.disabled) return;
    const event = { target: this, preventDefault() {}, stopPropagation() {} };
    for (const listener of this.listeners.get(type) || []) await listener(event);
  }
  click() { return this.dispatch('click'); }
  showModal() { this.open = true; }
  close() { this.open = false; }
  remove() {}
}

function createPage(options = {}) {
  const elements = new Map();
  const byId = (id) => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const document = {
    getElementById: byId,
    createElement: (tag) => new Element(tag),
    createDocumentFragment: () => new Element('fragment'),
    addEventListener() {},
    querySelector: (selector) => selector === 'meta[name="rt-csrf"]' ? { content: 'synthetic-csrf' } : null,
    body: new Element('body'),
  };
  // Reflect the template's initially hidden source notice.
  byId('converter-origin').hidden = true;
  // Use real imported conversion functions, then run the UI unchanged apart
  // from resolving its ESM imports through the VM's explicit globals.
  const source = readFileSync(new URL('../rt_extractor/static/converter-ui.mjs', import.meta.url), 'utf8')
    .replace(/^import[^\n]*\n/gm, '');
  const context = vm.createContext({
    document, TextEncoder, Blob, URL, Error, setTimeout, crypto: webcrypto,
    fetch: options.fetch || (async () => { throw new Error('Unexpected network request'); }),
    ...converter, ...archive, ...pasteParser,
  });
  vm.runInContext(source, context, { filename: 'converter-ui.mjs' });
  return {
    byId,
    count(id) { return Number(byId(id).textContent.replace(/[,\s]/g, '')); },
    async paste(text) {
      byId('converter-json').value = text;
      await byId('converter-json').dispatch('input');
      await byId('convert-paste-button').click();
    },
    async importFile(text, name = 'synthetic.json') {
      byId('json-files').files = [{ name, size: new TextEncoder().encode(text).length, text: async () => text }];
      await byId('json-files').dispatch('change');
    },
    async previewFirst() {
      await byId('converter-results-body').querySelectorAll('button')[0].click();
      return JSON.parse(byId('converter-preview-value').textContent);
    },
  };
}

const account = (index, accessToken = `synthetic-access-${index}`) => ({
  type: 'claude', email: `synthetic-${index}@example.com`, access_token: accessToken,
});
const accountArray = (length) => JSON.stringify(Array.from({ length }, (_, index) => account(index)));

test('successful paste clears input and shows a downloadable account', async () => {
  const page = createPage();
  await page.paste(JSON.stringify(account(1)));
  assert.equal(page.count('convert-count-success'), 1);
  assert.equal(page.count('convert-count-error'), 0);
  assert.equal(page.byId('converter-json').value, '');
  assert.equal(page.byId('converter-downloads').hidden, false);
  assert.equal(page.byId('convert-paste-button').disabled, true);
});

test('paste at the 1000-account limit preserves every unprocessed input byte', async () => {
  const page = createPage();
  await page.paste(accountArray(1000));
  assert.equal(page.count('convert-count-success'), 1000);
  const additional = `\n${JSON.stringify(account('additional'))}\n`;
  await page.paste(additional);
  assert.equal(page.count('convert-count-success'), 1000);
  assert.equal(page.byId('converter-json').value, additional);
  assert.equal(page.byId('converter-error').hidden, false);
  assert.match(page.byId('converter-error').textContent, /1,000/);
});

test('paste that reaches capacity partway through preserves its original text', async () => {
  const page = createPage();
  await page.paste(accountArray(999));
  const additional = `${JSON.stringify(account('last-slot'))}\n${JSON.stringify(account('over-limit'))}`;
  await page.paste(additional);
  assert.equal(page.count('convert-count-success'), 1000);
  assert.equal(page.byId('converter-json').value, additional);
  assert.equal(page.byId('converter-error').hidden, false);
});

for (const [name, text] of [
  ['malformed JSON', '{"type":"claude","access_token":"synthetic",}'],
  ['missing required token', '{"type":"claude","email":"synthetic@example.com"}'],
]) {
  test(`${name} remains available for correction after conversion fails`, async () => {
    const page = createPage();
    await page.paste(text);
    assert.equal(page.byId('converter-json').value, text);
    assert.equal(page.count('convert-count-success'), 0);
    assert.equal(page.count('convert-count-error'), 1);
    assert.equal(page.byId('converter-downloads').hidden, true);
  });
}

test('identical content is skipped despite formatting and object-key ordering', async () => {
  const page = createPage();
  const input = account(1);
  await page.paste(JSON.stringify(input));
  const reordered = JSON.stringify({ access_token: input.access_token, email: input.email, type: input.type }, null, 2);
  await page.paste(reordered);
  assert.equal(page.count('convert-count-success'), 1);
  assert.equal(page.count('convert-count-error'), 1);
  assert.match(page.byId('converter-results-body').textContent, /重复导入/);
  assert.match(page.byId('converter-results-body').textContent, /已跳过/);
  assert.equal(page.byId('converter-json').value, reordered);
});

test('CPA array duplicates are skipped per account while token updates still convert', async () => {
  const page = createPage();
  await page.paste(JSON.stringify([account(1), account(1), account(1, 'synthetic-updated-access')]));
  assert.equal(page.count('convert-count-success'), 2);
  assert.equal(page.count('convert-count-error'), 1);
  assert.match(page.byId('converter-results-body').textContent, /重复导入/);
});

test('reimporting the same file cannot add another successful account', async () => {
  const page = createPage();
  const text = JSON.stringify(account(1));
  await page.importFile(text, 'first.json');
  await page.importFile(text, 'renamed-copy.json');
  assert.equal(page.count('convert-count-success'), 1);
  assert.equal(page.count('convert-count-error'), 1);
  assert.match(page.byId('converter-results-body').textContent, /重复导入/);
});

test('reset clears duplicate tracking so the same JSON can be imported again', async () => {
  const page = createPage();
  const input = JSON.stringify(account(1));
  await page.paste(input);
  await page.byId('converter-clear').click();
  assert.equal(page.count('convert-count-total'), 0);
  assert.equal(page.byId('converter-json').value, '');
  await page.paste(input);
  assert.equal(page.count('convert-count-success'), 1);
  assert.equal(page.count('convert-count-error'), 0);
  assert.equal(page.byId('converter-json').value, '');
});

test('Sub2API duplicate tracking applies to the complete input document', async () => {
  const page = createPage();
  await page.byId('mode-sub2api').click();
  const input = JSON.stringify({ accounts: [
    { name: 'first', platform: 'anthropic', type: 'oauth', credentials: { access_token: 'synthetic-first' } },
    { name: 'second', platform: 'gemini', type: 'oauth', credentials: { access_token: 'synthetic-second' } },
  ] });
  await page.paste(input);
  assert.equal(page.count('convert-count-success'), 2);
  await page.paste(input);
  assert.equal(page.count('convert-count-success'), 2);
  assert.equal(page.count('convert-count-error'), 1);
  assert.equal(page.byId('converter-json').value, input);
  assert.match(page.byId('converter-results-body').textContent, /重复导入/);
});

const jwt = (payload) => `eyJhbGciOiJub25lIn0.${Buffer.from(JSON.stringify(payload)).toString('base64url')}.synthetic`;
const fullCPA = {
  type: 'codex', email: 'synthetic-latest@example.com', account_id: 'synthetic-account',
  access_token: jwt({ exp: 1900000000, 'https://api.openai.com/auth': { chatgpt_plan_type: 'plus' } }),
  id_token: jwt({ email: 'synthetic-latest@example.com' }), refresh_token: 'synthetic-latest-refresh',
  plan_type: 'plus', expired: '2030-03-17T17:46:40+00:00', last_refresh: '2026-10-22T01:00:00+00:00',
};

function localSourceFetch(routes, requests) {
  return async (path, options) => {
    requests.push({ path, options });
    assert.equal(options.credentials, 'same-origin');
    assert.equal(options.headers['X-RT-CSRF'], 'synthetic-csrf');
    assert.ok(Object.hasOwn(routes, path), `Unexpected local request: ${path}`);
    const value = routes[path];
    if (value instanceof Error) throw value;
    return { ok: value?.httpError !== true, json: async () => value };
  };
}

test('latest batch keeps the selected direction and preserves CPA metadata in both formats', async () => {
  const requests = [];
  const page = createPage({ fetch: localSourceFetch({
    '/api/batches/current': { batch: { id: 'synthetic-batch', status: 'completed', counts: { success: 1 }, result: {} } },
    '/api/batches/synthetic-batch/conversion-source': { accounts: [fullCPA] },
  }, requests) });
  await page.byId('converter-use-latest').click();
  assert.equal(page.count('convert-count-success'), 1);
  const sub2api = await page.previewFirst();
  assert.equal(sub2api.accounts[0].credentials.plan_type, fullCPA.plan_type);
  assert.equal(sub2api.accounts[0].credentials.chatgpt_account_id, fullCPA.account_id);
  assert.equal(sub2api.accounts[0].credentials.expires_at, new Date(fullCPA.expired).toISOString());
  assert.equal(sub2api.accounts[0].extra.last_refresh, new Date(fullCPA.last_refresh).toISOString());
  await page.byId('converter-preview-close').click();
  await page.byId('mode-sub2api').click();
  await page.byId('converter-use-latest').click();
  assert.equal(page.count('convert-count-success'), 1);
  assert.equal(page.byId('mode-sub2api').getAttribute('aria-pressed'), 'true');
  const cpa = await page.previewFirst();
  for (const key of ['type', 'email', 'account_id', 'plan_type', 'access_token', 'refresh_token', 'id_token']) {
    assert.equal(cpa[key], fullCPA[key]);
  }
  assert.equal(cpa.last_refresh, new Date(fullCPA.last_refresh).toISOString());
  assert.equal(cpa.expired, new Date(fullCPA.expired).toISOString());
  assert.match(page.byId('converter-origin').textContent, /Sub2API → CPA/);
  assert.equal(requests.length, 4);
});

test('latest standalone extraction loads its CPA source without a lossy Sub2API intermediate', async () => {
  const requests = [];
  const page = createPage({ fetch: localSourceFetch({
    '/api/batches/current': { batch: null },
    '/api/jobs/current': { job: { id: 'synthetic-job', email: fullCPA.email, status: 'success' } },
    '/api/jobs/synthetic-job/cpa-download': fullCPA,
  }, requests) });
  await page.byId('mode-sub2api').click();
  await page.byId('converter-use-latest').click();
  const cpa = await page.previewFirst();
  assert.equal(cpa.plan_type, 'plus');
  assert.equal(cpa.last_refresh, new Date(fullCPA.last_refresh).toISOString());
  assert.deepEqual(requests.map((request) => request.path), [
    '/api/batches/current', '/api/jobs/current', '/api/jobs/synthetic-job/cpa-download',
  ]);
});

for (const [name, routes] of [
  ['source download fails', {
    '/api/batches/current': { batch: { id: 'synthetic-batch', status: 'completed', counts: { success: 1 }, result: {} } },
    '/api/batches/synthetic-batch/conversion-source': { httpError: true },
  }],
  ['batch is still running', {
    '/api/batches/current': { batch: { id: 'synthetic-batch', status: 'running', counts: { success: 1 } } },
  }],
  ['source has no accounts', {
    '/api/batches/current': { batch: { id: 'synthetic-batch', status: 'completed', counts: { success: 1 }, result: {} } },
    '/api/batches/synthetic-batch/conversion-source': { accounts: [] },
  }],
]) {
  test(`latest result rejection preserves current input and successful rows when ${name}`, async () => {
    const page = createPage({ fetch: localSourceFetch(routes, []) });
    await page.paste(JSON.stringify(account('existing')));
    const pendingInput = JSON.stringify(account('pending'));
    page.byId('converter-json').value = pendingInput;
    await page.byId('converter-json').dispatch('input');
    await page.byId('converter-use-latest').click();
    assert.equal(page.count('convert-count-success'), 1);
    assert.equal(page.byId('converter-json').value, pendingInput);
    assert.match(page.byId('converter-results-body').textContent, /synthetic-existing@example.com/);
    assert.equal(page.byId('converter-error').hidden, false);
    assert.equal(page.byId('converter-origin').hidden, true);
  });
}
