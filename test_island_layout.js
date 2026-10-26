const assert = require('node:assert/strict');
const {test} = require('node:test');
const {layout} = require('./island-layout.js');
const nodes = values => values.map((bytes, i) => ({key:String(i), bytes, files:i + 1}));
function proportional(circles){
  const positive = circles.filter(p => p.weight > 0);
  const factor = positive.length ? positive[0].area / positive[0].weight : 0;
  for (const p of circles){
    assert.ok(Number.isFinite(p.r) && Number.isFinite(p.x) && Number.isFinite(p.y));
    if (!p.weight) assert.equal(p.area, 0);
    else assert.ok(Math.abs(p.area / p.weight / factor - 1) < 1e-12, 'area per byte changed between islands');
  }
}

test('four times the bytes gives four times the area and twice the radius', () => {
  const out = layout(nodes([1,4,16]), 1000, 540, 'size');
  assert.equal(out[1].r / out[0].r, 2);
  assert.equal(out[2].area / out[0].area, 16);
  proportional(out);
});
test('dense layouts and viewport changes preserve ratios and readable labels', () => {
  for (const [width,height,total] of [[320,440,6],[700,430,12],[960,340,12],[1600,540,12]]){
    const out = layout(nodes(Array.from({length:total}, (_,i) => i ? 10 ** i : 0)), width, height, 'size');
    proportional(out);
    out.forEach((a,i) => {
      assert.ok(a.labelX - a.labelWidth / 2 >= 0 && a.labelX + a.labelWidth / 2 <= width);
      assert.ok(a.labelY - 29 >= 0 && a.labelY + 29 <= height);
      out.slice(i+1).forEach(b => assert.ok(
        Math.abs(a.labelX-b.labelX) >= (a.labelWidth+b.labelWidth)/2 || Math.abs(a.labelY-b.labelY) >= 58,
        'labels overlap'));
    });
  }
});
test('tiny, zero, and unknown sizes never receive a minimum land area', () => {
  const out = layout(nodes([0,undefined,1,1e12]), 700, 430, 'size');
  assert.equal(out[0].r, 0); assert.equal(out[1].r, 0);
  assert.ok(Math.abs(out[2].area / out[3].area - 1e-12) < 1e-24);
  proportional(out);
});
test('empty maps and maps with only unknown sizes remain navigable without land', () => {
  assert.deepEqual(layout([],700,430,'size'), []);
  const out = layout(nodes([0,undefined,0]),700,430,'size');
  assert.equal(out.length, 3);
  out.forEach(p => {assert.equal(p.r,0); assert.ok(Number.isFinite(p.labelX) && Number.isFinite(p.labelY));});
});
test('count mode also uses a common area scale, including empty directories', () => {
  const out = layout([{files:0, directories:10}, {files:1}, {files:4}],700,430,'count');
  assert.equal(out[0].area, 0);
  proportional(out);
});
