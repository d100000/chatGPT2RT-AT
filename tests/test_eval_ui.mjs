import test from 'node:test';
import assert from 'node:assert/strict';
import { modelIds, requestCount, numberText, activeTask, svgDataURL, filtersFrom } from '../rt_extractor/static/eval-ui.mjs';

test('plan counts, manual model IDs and zero vs missing values stay distinct', () => {
  assert.deepEqual(modelIds('a\nb,a， c\n'), ['a', 'b', 'c']);
  assert.equal(requestCount(1, 1, 'simple', 3), 1);
  assert.equal(requestCount(2, 3, 'batch', 3), 18);
  assert.equal(numberText(0), '0');
  assert.equal(numberText(null), '未知');
  assert.equal(numberText(undefined), '未知');
  assert.equal(activeTask({ status: 'stopping' }), true);
  assert.equal(activeTask({ status: 'interrupted' }), false);
});
test('SVG data URL preserves Unicode source byte for byte without DOM injection', () => {
  const svg = '<svg><text>鹈鹕 &amp; 自行车</text></svg>';
  const encoded = svgDataURL(svg);
  assert.equal(Buffer.from(encoded.split(',')[1], 'base64').toString(), svg);
});
test('history filters use full local-date boundaries instead of UTC date prefixes', () => {
  const result = filtersFrom({ from: '2026-11-01', to: '2026-11-01', model: 'm', mode: '' });
  assert.equal(new Date(result.from).getHours(), 0);
  assert.equal(new Date(result.to).getHours(), 23);
  assert.equal(result.model, 'm');
  assert.equal(result.mode, undefined);
});
