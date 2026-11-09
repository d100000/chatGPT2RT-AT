import test from 'node:test';
import assert from 'node:assert/strict';
import { modelIds, requestCount, numberText, activeTask, svgDataURL, reviewImage, filtersFrom } from '../rt_extractor/static/eval-ui.mjs';

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
test('random animation plans count draws instead of the catalog or hidden fixed selection', () => {
  assert.equal(requestCount(101, 1, 'simple', 3, true), 1);
  assert.equal(requestCount(0, 2, 'batch', 4, true), 8);
  assert.equal(requestCount(30, 3, 'batch', 100, true), 300);
  assert.equal(requestCount(2, 3, 'batch', 3, false), 18);
  assert.equal(requestCount(0, 1, 'simple', 3, false), 0);
});
test('SVG data URL preserves Unicode source byte for byte without DOM injection', () => {
  const svg = '<svg><text>鹈鹕 &amp; 自行车</text></svg>';
  const encoded = svgDataURL(svg);
  assert.equal(Buffer.from(encoded.split(',')[1], 'base64').toString(), svg);
});
test('animation review never falls back to a still frame or an unsafe SVG', () => {
  const svg = '<svg xmlns="http://www.w3.org/2000/svg"><circle r="2"/></svg>';
  const png = 'data:image/png;base64,first-frame';
  const animated = { question: { animation: true }, technical: { safe: true }, svg, render_evidence: { data_url: png } };
  assert.equal(reviewImage(animated), svgDataURL(svg));
  assert.equal(reviewImage({ ...animated, svg: null }), null);
  assert.equal(reviewImage({ ...animated, svg: ' ' }), null);
  assert.equal(reviewImage({ ...animated, technical: { safe: false } }), null);
  assert.equal(reviewImage({ question: animated.question, image: svgDataURL(svg) }, true), svgDataURL(svg));
  assert.equal(reviewImage({ question: animated.question, image: png }, true), null);
  assert.equal(reviewImage({ ...animated, question: {} }), png);
  assert.equal(reviewImage({ question: {}, image: png }, true), png);
});
test('history filters use full local-date boundaries instead of UTC date prefixes', () => {
  const result = filtersFrom({ from: '2026-11-01', to: '2026-11-01', model: 'm', mode: '' });
  assert.equal(new Date(result.from).getHours(), 0);
  assert.equal(new Date(result.to).getHours(), 23);
  assert.equal(result.model, 'm');
  assert.equal(result.mode, undefined);
});
