// Includes upstream CPA2sub2API tests (MIT); see THIRD_PARTY_NOTICES.md.
import assert from "node:assert/strict";
import test from "node:test";

import {
  convertCPARecord,
  convertSub2ApiDocument,
  buildMergedSub2ApiDocument,
  parseJwtPayload,
} from "../rt_extractor/static/converter.mjs";

function jwt(payload) {
  const encode = (value) => Buffer.from(JSON.stringify(value)).toString("base64url");
  return `${encode({ alg: "none", typ: "JWT" })}.${encode(payload)}.`;
}

test("CPA Codex input accepts missing refresh_token", () => {
  const converted = convertCPARecord({
    type: "codex",
    access_token: jwt({
      exp: 4_102_444_800,
      "https://api.openai.com/profile": {
        email: "codex@example.com",
      },
    }),
    id_token: jwt({
      email: "codex@example.com",
    }),
  }, {
    now: new Date("2026-05-25T00:00:00.000Z"),
  });

  assert.equal(converted.account.credentials.refresh_token, undefined);
  assert.equal(converted.document.accounts[0].credentials.refresh_token, undefined);
});

test("sub2api OpenAI input emits blank CPA refresh_token when missing", () => {
  const result = convertSub2ApiDocument({
    accounts: [
      {
        name: "Codex Account",
        platform: "openai",
        type: "oauth",
        credentials: {
          access_token: jwt({
            exp: 4_102_444_800,
          }),
          id_token: jwt({
            email: "codex@example.com",
          }),
          email: "codex@example.com",
        },
      },
    ],
  }, {
    now: new Date("2026-05-25T00:00:00.000Z"),
  });

  assert.equal(result.skipped.length, 0);
  assert.equal(result.converted.length, 1);
  assert.equal(result.converted[0].document.refresh_token, "");
  assert.ok(Object.hasOwn(result.converted[0].document, "refresh_token"));
});

const FIXED_NOW = new Date('2026-10-22T00:00:00Z');
const EXPIRY = '2027-01-15T08:00:00.000Z';
const EXPIRY_SECONDS = 1800000000;

for (const [provider, platform] of [['claude', 'anthropic'], ['antigravity', 'antigravity'], ['gemini', 'gemini']]) {
  test(`${provider} survives CPA → Sub2API → CPA with OAuth fields`, () => {
    const token = { access_token: 'synthetic-access', refresh_token: 'synthetic-refresh', token_type: 'Bearer' };
    const input = {
      type: provider, email: 'person@example.com', expired: EXPIRY,
      last_refresh: FIXED_NOW.toISOString(), project_id: 'synthetic-project',
      auto: false, checked: true, plan_type: 'test-plan',
      ...(provider === 'gemini' ? { token: { ...token, expiry: EXPIRY, scopes: ['scope-a', 'scope-b'] } } : token),
    };
    const forward = convertCPARecord(input, { now: FIXED_NOW });
    assert.equal(forward.account.platform, platform);
    assert.equal(forward.document.type, 'sub2api-data');
    assert.equal(forward.document.version, 1);
    assert.equal(forward.account.concurrency, 10);
    assert.equal(forward.account.priority, 1);
    const reverse = convertSub2ApiDocument(forward.document, { now: FIXED_NOW });
    assert.equal(reverse.skipped.length, 0);
    const output = reverse.converted[0].document;
    assert.equal(output.type, provider);
    assert.equal(output.email, input.email);
    assert.equal(output.last_refresh, FIXED_NOW.toISOString());
    const outputToken = provider === 'gemini' ? output.token : output;
    assert.equal(outputToken.access_token, token.access_token);
    assert.equal(outputToken.refresh_token, token.refresh_token);
    assert.equal(provider === 'gemini' ? outputToken.expiry : output.expired, EXPIRY);
    if (provider === 'gemini') {
      assert.equal(output.auto, false);
      assert.equal(output.checked, true);
      assert.equal(output.token.scope, 'scope-a scope-b');
      assert.equal(forward.account.credentials.oauth_type, 'code_assist');
    }
  });
}

test('current extractor raw Token JSON preserves explicit account metadata and expiry', () => {
  const raw = {
    email: 'raw@example.com', access_token: jwt({ exp: EXPIRY_SECONDS + 1000 }),
    id_token: jwt({ email: 'claim@example.com' }), refresh_token: 'synthetic-refresh',
    expires_at: EXPIRY, obtained_at: FIXED_NOW.toISOString(),
    chatgpt_account_id: 'explicit-account', chatgpt_user_id: 'explicit-user', plan_type: 'plus',
    password: 'never-export-password', totp_secret: 'never-export-secret',
  };
  const result = convertCPARecord(raw, { now: FIXED_NOW });
  assert.equal(result.expiresAt, EXPIRY);
  assert.equal(result.account.credentials.expires_in, EXPIRY_SECONDS - FIXED_NOW.getTime() / 1000);
  assert.equal(result.account.credentials.chatgpt_account_id, 'explicit-account');
  assert.equal(result.account.credentials.chatgpt_user_id, 'explicit-user');
  assert.equal(result.account.extra.last_refresh, FIXED_NOW.toISOString());
  assert.ok(!JSON.stringify(result.document).includes('never-export'));
  const reverse = convertSub2ApiDocument(result.document, { now: FIXED_NOW });
  assert.equal(reverse.converted[0].document.account_id, 'explicit-account');
  assert.equal(reverse.converted[0].document.expired, EXPIRY);
});

test('OpenAI JWT claims decode UTF-8 and derive identity, plan, and organization', () => {
  const record = {
    type: 'codex',
    access_token: jwt({
      exp: EXPIRY_SECONDS, 'https://api.openai.com/profile': { email: '用户@example.com' },
      'https://api.openai.com/auth': { chatgpt_account_id: 'account', chatgpt_user_id: 'user', chatgpt_plan_type: 'plus' },
    }),
    id_token: jwt({ 'https://api.openai.com/auth': { organizations: [{ id: 'other' }, { id: 'preferred', is_default: true }] } }),
  };
  const result = convertCPARecord(record, { now: FIXED_NOW });
  assert.equal(result.email, '用户@example.com');
  assert.equal(result.account.credentials.organization_id, 'preferred');
  assert.equal(result.account.credentials.chatgpt_account_id, 'account');
  assert.equal(result.account.credentials.chatgpt_user_id, 'user');
  assert.equal(result.planType, 'plus');
});

for (const expiry of [EXPIRY_SECONDS, String(EXPIRY_SECONDS), EXPIRY_SECONDS * 1000, String(EXPIRY_SECONDS * 1000), EXPIRY]) {
  test(`timestamp representation ${JSON.stringify(expiry)} keeps OAuth expiry`, () => {
    const forward = convertCPARecord({ type: 'claude', access_token: 'synthetic', expired: expiry }, { now: FIXED_NOW });
    assert.equal(forward.expiresAt, EXPIRY);
    assert.equal(forward.account.credentials.expires_at, String(EXPIRY_SECONDS));
    const reverse = convertSub2ApiDocument({ accounts: [{ platform: 'anthropic', credentials: { access_token: 'synthetic', expires_at: expiry } }] }, { now: FIXED_NOW });
    assert.equal(reverse.converted[0].document.expired, EXPIRY);
  });
}

test('invalid and out-of-range dates are omitted without aborting other fields', () => {
  for (const expiry of [1e100, -1e100, 'not-a-date', new Date('invalid')]) {
    const result = convertCPARecord({ type: 'claude', access_token: 'synthetic', expired: expiry }, { now: new Date('invalid') });
    assert.equal(result.expiresAt, undefined);
    assert.ok(Number.isFinite(new Date(result.document.exported_at).getTime()));
  }
  const openai = convertCPARecord({ access_token: jwt({ exp: 1e100 }), id_token: jwt({}) });
  assert.equal(openai.expiresAt, undefined);
});

test('Antigravity timestamp accepts Unix seconds and milliseconds', () => {
  for (const timestamp of [EXPIRY_SECONDS - 3600, (EXPIRY_SECONDS - 3600) * 1000]) {
    const result = convertCPARecord({ type: 'antigravity', access_token: 'synthetic', timestamp, expires_in: 3600 });
    assert.equal(result.expiresAt, EXPIRY);
  }
  assert.doesNotThrow(() => convertCPARecord({ type: 'antigravity', access_token: 'synthetic', timestamp: 1e100, expires_in: 3600 }));
});

test('invalid JWT object types and malformed payloads fail explicitly', () => {
  for (const payload of [null, [], 'string', 42]) {
    assert.throws(() => convertCPARecord({ access_token: jwt(payload), id_token: jwt({}) }), /JWT/);
  }
  assert.throws(() => convertCPARecord({ access_token: 'not-a-jwt', id_token: jwt({}) }), /JWT/);
});

test('Sub2API wrappers, platform aliases and mixed invalid accounts keep valid results', () => {
  const account = { name: 'example', platform: 'claude', type: 'oauth', credentials: { access_token: 'synthetic' } };
  for (const wrapper of [account, [account], { accounts: [account] }]) {
    const result = convertSub2ApiDocument(wrapper);
    assert.equal(result.converted.length, 1);
    assert.equal(result.skipped.length, 0);
  }
  const result = convertSub2ApiDocument({ accounts: [null, { platform: 'unsupported' }, { ...account, type: 'api-key' }, account] });
  assert.equal(result.converted.length, 1);
  assert.equal(result.skipped.length, 3);
  assert.throws(() => convertSub2ApiDocument({ accounts: [] }), /为空/);
  assert.throws(() => convertSub2ApiDocument({}), /accounts/);
});

test('Gemini accepts camelCase token fields and absent optional fields are omitted', () => {
  const result = convertCPARecord({ type: 'gemini', token: { accessToken: 'synthetic', refreshToken: 'refresh', tokenType: 'Bearer', expiry: EXPIRY_SECONDS } });
  assert.equal(result.account.credentials.access_token, 'synthetic');
  assert.equal(result.account.credentials.refresh_token, 'refresh');
  assert.equal(result.account.credentials.token_type, 'Bearer');
  assert.equal(result.account.credentials.expires_at, String(EXPIRY_SECONDS));
  assert.ok(!Object.hasOwn(result.account.credentials, 'project_id'));
});


test('merged Sub2API document preserves ordered results and emits v1 envelope', () => {
  const records = [
    convertCPARecord({ type: 'claude', access_token: 'first' }, { sourceName: 'one.json', now: FIXED_NOW }),
    convertCPARecord({ type: 'gemini', token: { access_token: 'second' } }, { sourceName: 'two.json', now: FIXED_NOW }),
  ];
  const merged = buildMergedSub2ApiDocument(records, { now: FIXED_NOW });
  assert.equal(merged.type, 'sub2api-data');
  assert.equal(merged.version, 1);
  assert.equal(merged.exported_at, FIXED_NOW.toISOString());
  assert.deepEqual(merged.proxies, []);
  assert.deepEqual(merged.accounts.map((account) => account.credentials.access_token), ['first', 'second']);
  assert.equal(convertSub2ApiDocument(merged).converted.length, 2);
});

test('JWT helper never returns scalar or malformed payloads', () => {
  for (const token of [jwt(null), jwt('text'), jwt(7), jwt([]), '', null, 'bad', 'a.@@.b']) {
    assert.equal(parseJwtPayload(token), undefined);
  }
  assert.deepEqual(parseJwtPayload(jwt({ email: '用户@example.com' })), { email: '用户@example.com' });
});
