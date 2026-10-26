/* Shared by the browser and geometry regression checks. All radius adjustments
   use ONE scale for the whole map, so area / weight stays constant. */
(function(root, factory){
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.IslandLayout = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function(){
  const ASPECT = .82;
  function layout(items, width, height, mode){
    if (!items.length) return [];
    const weights = items.map(n => Math.max(0, Number(mode === 'size' ? n.bytes : n.files) || 0));
    const maximum = Math.max(0, ...weights);
    const packed = [];
    items.forEach((node, i) => {
      const r = maximum ? 50 * Math.sqrt(weights[i] / maximum) : 0;
      let x = 0, y = 0;
      for (let step = 0; step < 25000; step++){
        const angle = step * .29, distance = Math.sqrt(step) * 2.5;
        x = Math.cos(angle) * distance; y = Math.sin(angle) * distance;
        if (packed.every(p => Math.hypot(x - p.x, y - p.y) > r + p.r + 14)) break;
      }
      packed.push({node, weight: weights[i], r, x, y});
    });
    const left = Math.min(...packed.map(p => p.x - p.r * 1.2));
    const right = Math.max(...packed.map(p => p.x + p.r * 1.2));
    const top = Math.min(...packed.map(p => p.y - p.r));
    const bottom = Math.max(...packed.map(p => p.y + p.r));
    const scale = Math.max(0, Math.min((width - 48) / Math.max(1, right - left),
      (height - 126) / Math.max(1, bottom - top), 3));
    const label = p => ({...p, labelX: p.x,
      labelY: p.r < 30 ? p.y + p.r * ASPECT + 36 : p.y + p.r * .15,
      labelWidth: Math.min(140, Math.max(96, p.r * 1.55))});
    let circles = packed.map(p => label({...p, x: width / 2 + (p.x - (left + right) / 2) * scale,
      y: height / 2 + (p.y - (top + bottom) / 2) * scale, r: p.r * scale}));
    const crowded = circles.some((a, i) => circles.slice(i + 1).some(b =>
      Math.abs(a.labelX - b.labelX) < (a.labelWidth + b.labelWidth) / 2 + 8 &&
      Math.abs(a.labelY - b.labelY) < 65));
    const clipped = circles.some(p => p.labelX - p.labelWidth / 2 < 12 ||
      p.labelX + p.labelWidth / 2 > width - 12 || p.labelY - 29 < 48 || p.labelY + 29 > height - 30);
    if (crowded || clipped || !maximum){
      const cols = Math.max(1, Math.min(items.length, Math.floor((width - 48) / 118)));
      const rows = Math.ceil(items.length / cols), cellW = (width - 48) / cols, cellH = (height - 106) / rows;
      // Reserving room for labels changes the common scale, never an individual
      // island's radius. Zero bytes still means exactly zero land area.
      const maxRadius = Math.max(0, Math.min(cellW * .4, (cellH - 65) / (2 * ASPECT)));
      circles = packed.map((p, i) => {
        const row = Math.floor(i / cols), column = i % cols;
        const rowCount = Math.min(cols, items.length - row * cols);
        const r = p.r / 50 * maxRadius;
        const x = width / 2 + (column - (rowCount - 1) / 2) * cellW;
        const blockHeight = 2 * maxRadius * ASPECT + 66;
        const blockTop = 58 + row * cellH + (cellH - blockHeight) / 2;
        const y = blockTop + maxRadius * ASPECT;
        return {...p, x, y, r, labelX: x, labelY: blockTop + blockHeight - 29,
          labelWidth: Math.min(130, cellW - 12)};
      });
    }
    return circles.map(p => ({...p, area: Math.PI * ASPECT * p.r * p.r}));
  }
  return {layout};
});
