import assert from 'node:assert/strict';
import test from 'node:test';
import { buildZipArchive, makeUniqueFileNames } from '../rt_extractor/static/archive.mjs';

async function readZip(blob) {
  const bytes = new Uint8Array(await blob.arrayBuffer());
  const view = new DataView(bytes.buffer);
  const decoder = new TextDecoder();
  const entries = [];
  const centralNames = [];
  let offset = 0;
  while (view.getUint32(offset, true) === 0x04034b50) {
    const nameLength = view.getUint16(offset + 26, true);
    const extraLength = view.getUint16(offset + 28, true);
    const size = view.getUint32(offset + 18, true);
    const start = offset + 30;
    const bodyStart = start + nameLength + extraLength;
    entries.push({
      name: decoder.decode(bytes.slice(start, start + nameLength)),
      text: decoder.decode(bytes.slice(bodyStart, bodyStart + size)),
      crc: view.getUint32(offset + 14, true),
      flags: view.getUint16(offset + 6, true),
    });
    offset = bodyStart + size;
  }
  while (view.getUint32(offset, true) === 0x02014b50) {
    const length = view.getUint16(offset + 28, true);
    centralNames.push(decoder.decode(bytes.slice(offset + 46, offset + 46 + length)));
    offset += 46 + length + view.getUint16(offset + 30, true) + view.getUint16(offset + 32, true);
  }
  assert.equal(view.getUint32(offset, true), 0x06054b50);
  assert.equal(view.getUint16(offset + 10, true), entries.length);
  assert.deepEqual(centralNames, entries.map(({ name }) => name));
  return entries;
}

test('ZIP exports UTF-8 JSON files and correct CRC32', async () => {
  const entries = await readZip(buildZipArchive([
    { fileName: 'test.json', text: '123456789' },
    { fileName: '用户.json', text: JSON.stringify({ email: '用户@example.com' }) },
  ]));
  assert.equal(entries[0].crc, 0xcbf43926);
  assert.equal(entries[0].text, '123456789');
  assert.equal(entries[1].name, '用户.json');
  assert.deepEqual(JSON.parse(entries[1].text), { email: '用户@example.com' });
  assert.equal(entries[1].flags & 0x0800, 0x0800);
});

test('duplicate and already suffixed names never overwrite another ZIP member', async () => {
  const names = ['a.json', 'a.json', 'a-2.json', 'A.JSON', 'a.json'];
  const result = makeUniqueFileNames(names);
  assert.equal(new Set(result.map((name) => name.toLowerCase())).size, names.length);
  const entries = await readZip(buildZipArchive(names.map((fileName, index) => ({ fileName, text: String(index) }))));
  assert.deepEqual(entries.map(({ name }) => name), result);
  assert.deepEqual(entries.map(({ text }) => text), ['0', '1', '2', '3', '4']);
});

test('archive names remove traversal, absolute paths, controls and reserved Windows names', async () => {
  const names = ['../../outside.json', '/tmp/absolute.json', '..\\..\\windows.json', '.', '..', 'bad\u0000:name?.json', 'CON.json'];
  const entries = await readZip(buildZipArchive(names.map((fileName) => ({ fileName, text: '{}' }))));
  for (const { name } of entries) {
    assert.ok(!/[\\/\x00-\x1f:*?"<>|]/.test(name));
    assert.ok(!name.startsWith('.'));
    assert.ok(!/^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(name));
  }
  assert.equal(entries[0].name, 'outside.json');
  assert.equal(entries[1].name, 'absolute.json');
  assert.equal(entries[2].name, 'windows.json');
});

test('Unicode normalization prevents visually identical filenames from colliding', () => {
  assert.deepEqual(makeUniqueFileNames(['é.json', 'e\u0301.json']), ['é.json', 'e\u0301-2.json']);
});

test('empty archive is valid and ZIP limits fail explicitly', async () => {
  assert.deepEqual(await readZip(buildZipArchive([])), []);
  assert.throws(() => buildZipArchive(new Array(65536)), /65535/);
  assert.throws(() => buildZipArchive([{ fileName: 'x'.repeat(65536), text: '' }]), /大小限制/);
});
