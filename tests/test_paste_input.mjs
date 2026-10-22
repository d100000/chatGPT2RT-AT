// Includes upstream CPA2sub2API tests (MIT); see THIRD_PARTY_NOTICES.md.
import assert from "node:assert/strict";
import test from "node:test";

import {
  buildPastedInputItems,
  parsePastedJsonDocuments,
} from "../rt_extractor/static/paste-input.mjs";

test("parsePastedJsonDocuments parses one expanded JSON object", () => {
  const result = parsePastedJsonDocuments(`
    {
      "type": "claude",
      "access_token": "token"
    }
  `);

  assert.equal(result.issues.length, 0);
  assert.deepEqual(result.documents, [
    {
      type: "claude",
      access_token: "token",
    },
  ]);
});

test("parsePastedJsonDocuments parses multiple expanded JSON documents", () => {
  const result = parsePastedJsonDocuments(`
    {
      "type": "codex",
      "access_token": "first"
    }

    {
      "type": "claude",
      "access_token": "second"
    }
  `);

  assert.equal(result.issues.length, 0);
  assert.deepEqual(result.documents, [
    {
      type: "codex",
      access_token: "first",
    },
    {
      type: "claude",
      access_token: "second",
    },
  ]);
});

test("parsePastedJsonDocuments parses JSONL style input", () => {
  const result = parsePastedJsonDocuments(`
{"type":"codex","access_token":"first"}
{"type":"claude","access_token":"second"}
  `);

  assert.equal(result.issues.length, 0);
  assert.deepEqual(result.documents, [
    {
      type: "codex",
      access_token: "first",
    },
    {
      type: "claude",
      access_token: "second",
    },
  ]);
});

test("parsePastedJsonDocuments ignores braces inside strings", () => {
  const result = parsePastedJsonDocuments(`
    {
      "type": "claude",
      "access_token": "token with } brace",
      "note": "escaped quote: \\" and { brace"
    }
    {
      "type": "gemini",
      "token": {
        "access_token": "second"
      }
    }
  `);

  assert.equal(result.issues.length, 0);
  assert.equal(result.documents.length, 2);
  assert.equal(result.documents[0].access_token, "token with } brace");
  assert.equal(result.documents[0].note, "escaped quote: \" and { brace");
});

test("parsePastedJsonDocuments keeps valid documents and reports trailing incomplete JSON", () => {
  const result = parsePastedJsonDocuments(`
    {"type":"claude","access_token":"ok"}
    {
      "type": "codex",
      "access_token": "unfinished"
  `);

  assert.deepEqual(result.documents, [
    {
      type: "claude",
      access_token: "ok",
    },
  ]);
  assert.equal(result.issues.length, 1);
  assert.equal(result.issues[0].label, "粘贴内容 #2");
  assert.match(result.issues[0].reason, /JSON 不完整/);
});

test("buildPastedInputItems expands arrays in CPA to sub2api mode", () => {
  const items = buildPastedInputItems([
    [
      { type: "claude", access_token: "first" },
      { type: "claude", access_token: "second" },
    ],
  ], "cpaToSub2Api");

  assert.deepEqual(items, [
    {
      document: { type: "claude", access_token: "first" },
      sourceName: "粘贴内容 #1.1",
    },
    {
      document: { type: "claude", access_token: "second" },
      sourceName: "粘贴内容 #1.2",
    },
  ]);
});

test("buildPastedInputItems keeps arrays as one document in sub2api to CPA mode", () => {
  const accounts = [
    {
      platform: "anthropic",
      credentials: {
        access_token: "token",
      },
    },
  ];
  const items = buildPastedInputItems([accounts], "sub2apiToCpa");

  assert.deepEqual(items, [
    {
      document: accounts,
      sourceName: "粘贴内容 #1",
    },
  ]);
});

test('JSON BOM and whitespace are accepted', () => {
  const result = parsePastedJsonDocuments('\uFEFF {"type":"claude","access_token":"synthetic"}');
  assert.equal(result.documents.length, 1);
  assert.equal(result.issues.length, 0);
});

test('malformed JSON reports a safe error without echoing credentials', () => {
  const secret = 'do-not-display-this-secret';
  for (const input of [`{"access_token":"${secret}",}`, `${secret}`]) {
    const result = parsePastedJsonDocuments(input);
    assert.equal(result.issues.length, 1);
    assert.ok(!JSON.stringify(result.issues).includes(secret));
  }
});

test('malformed complete document does not discard a subsequent valid document', () => {
  const result = parsePastedJsonDocuments('{"bad":} {"type":"claude","access_token":"synthetic"}');
  assert.equal(result.issues.length, 1);
  assert.equal(result.documents.length, 1);
  assert.equal(result.documents[0].type, 'claude');
});
