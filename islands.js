/* Local, dependency-free pixel atlas. Size mode uses background metadata scans,
   tracks coverage explicitly, and never substitutes file counts for bytes. */
(() => {
  const kinds = {
    code: ['代码', '#89c992'], image: ['图像', '#d59ab7'],
    media: ['影音', '#86bfc7'], document: ['文档', '#d8c286'],
    archive: ['压缩包', '#c59b73'], other: ['其他', '#a5b0a1']
  };
  let request = 0, controller, current, circles = [], resizeTimer, activityTimer, sizePoll, currentQuery = '', renderedLimit = 0;
  let lastRevision = '';
  const ocean = $('ocean'), canvas = $('atlas-canvas'), nodes = $('island-nodes');
  const mapLimit = () => Math.min(12, Math.max(1, Math.floor((ocean.clientWidth - 48) / 118)) * Math.max(1, Math.floor((ocean.clientHeight - 126) / 88)));
  const count = n => n.toLocaleString('zh-CN');
  const makeButton = (text, action) => {
    const button = document.createElement('button'); button.type = 'button';
    button.textContent = text; button.onclick = action; return button;
  };

  function crumbs(){
    const bar = $('atlas-crumbs'); bar.replaceChildren();
    bar.append(makeButton('⌂ 全部群岛', () => navigate('')));
    if (!searchScope) return;
    const parts = searchScope.replace(/\\$/, '').split('\\');
    parts.forEach((part, i) => {
      const arrow = document.createElement('span'); arrow.textContent = '/'; bar.append(arrow);
      const path = i === 0 ? part + '\\' : parts.slice(0, i + 1).join('\\');
      const button = makeButton(part, () => navigate(path)); button.title = path;
      if (i === parts.length - 1) button.setAttribute('aria-current', 'location');
      bar.append(button);
    });
  }

  function navigate(path){
    searchScope = path; searchCategory = '';
    setView('islands');
  }

  window.showScopeBar = () => {
    let bar = $('scope-bar');
    if (!bar){ bar = document.createElement('div'); bar.id = 'scope-bar'; bar.className = 'scope-bar'; $('meta').after(bar); }
    bar.replaceChildren();
    const label = document.createElement('span');
    label.textContent = '当前位置：' + (searchScope || '全部磁盘') + (searchCategory ? ' · ' + kinds[searchCategory][0] : '');
    bar.append(label, makeButton('返回群岛', () => { searchCategory = ''; setView('islands'); }));
    if (searchScope || searchCategory) bar.append(makeButton('搜索全部磁盘', () => { searchScope = ''; searchCategory = ''; browseAll = true; window.showScopeBar(); search(); }));
    bar.hidden = false;
  };

  window.loadIslands = async (refresh = false) => {
    if (view !== 'islands') return;
    clearTimeout(sizePoll);
    const version = ++request;
    if (controller) controller.abort();
    controller = new AbortController();
    if ($('atlas-crumbs').dataset.scope !== searchScope || !$('atlas-crumbs').children.length){
      crumbs(); $('atlas-crumbs').dataset.scope = searchScope;
    }
    if ($('scope-bar')) $('scope-bar').hidden = true;
    ocean.setAttribute('aria-busy', 'true');
    const message = $('atlas-message');
    const p = new URLSearchParams({scope: searchScope, q: $('q').value.trim(), vol: $('vol').value,
      regex: $('opt-regex').checked ? '1' : '0', case: $('opt-case').checked ? '1' : '0',
      dirs: $('opt-dirs').checked ? '1' : '0', limit: String(mapLimit()),
      mode: $('atlas-weight').value, refresh: refresh === true ? '1' : '0'});
    if (!current || currentQuery !== p.toString()){
      message.hidden = false; message.textContent = '正在绘制群岛…';
    }
    try {
      const response = await fetch('/api/overview?' + p, {signal: controller.signal});
      const data = await response.json();
      if (version !== request || view !== 'islands') return;
      if (!response.ok || data.error) throw new Error(data.error || '概览读取失败');
      const sameNodes = current && currentQuery === p.toString() && JSON.stringify(current.nodes) === JSON.stringify(data.nodes);
      current = data; currentQuery = p.toString(); renderedLimit = Number(p.get('limit'));
      const bySize = data.mode === 'size';
      const scanning = bySize && ['counting', 'scanning'].includes(data.size_state);
      $('atlas-metric-label').textContent = bySize ? '已知文件大小' : '文件居民';
      $('atlas-metric-unit').textContent = bySize ? '' : '个文件';
      $('atlas-files').textContent = bySize ? (data.size_state === 'counting' || (!data.known_files && data.files) ? '—' : fmtSize(data.bytes)) : count(data.files);
      $('atlas-dirs').textContent = count(data.directories);
      $('atlas-groups').textContent = count(data.groups);
      $('atlas-mode').textContent = data.demo ? '演示海域 · 合成数据' : data.building ? '索引构建中' : '本机索引 · ' + (bySize ? '文件大小' : '文件数量');
      $('map-scale-label').textContent = bySize ? '以已知文件大小为尺度' : '以文件数量为尺度';
      $('atlas-note').textContent = bySize
        ? '岛屿面积参考已知文件逻辑大小；“+”表示仍有大小未知的文件。小文件、空目录和未知大小保留最小面积，不代表实际磁盘占用。'
        : '岛屿面积参考文件数量，空目录保留最小面积。概览基于当前可搜索索引，不代表磁盘空间占用。';
      $('size-status').hidden = !bySize;
      $('atlas-rescan').hidden = !bySize;
      if (bySize){
        const processed = data.scanned_files || 0, total = data.files || 0;
        $('size-progress').max = Math.max(1, total);
        $('size-progress').value = data.size_state === 'complete' ? Math.max(1, total) : processed;
        $('size-progress').hidden = !scanning;
        let status = data.size_state === 'counting' ? '正在整理当前范围的文件清单…'
          : (scanning ? '后台统计 ' : '已统计 ') + count(processed) + ' / ' + count(total) + ' 个文件';
        if (data.pending_files) status += ' · 待统计 ' + count(data.pending_files);
        if (data.failed_files) status += ' · 无法读取大小 ' + count(data.failed_files);
        if (data.stale) status += ' · 文件已变化，可点击重新统计';
        if (data.scan_error) status += ' · 统计中断，请重新统计';
        $('size-status-text').textContent = status;
      }
      const queryNote = $('q').value.trim() ? '搜索命中 · ' : '';
      $('atlas-caption').textContent = queryNote + '展示 ' + data.nodes.length + ' 个区域' +
        (data.omitted_groups ? ' · 其余 ' + count(data.omitted_groups) + ' 个区域可在列表查看' : ' · 点击岛屿继续探索');
      $('atlas-legend').replaceChildren();
      Object.entries(kinds).forEach(([key, [name, color]]) => {
        const item = document.createElement('span');
        const swatch = document.createElement('i'); swatch.className = 'legend-mark'; swatch.style.background = color;
        const value = bySize ? fmtSize(data.type_bytes[key]) + (data.types[key] && (data.pending_files || data.failed_files) ? '+' : '') : count(data.types[key]);
        item.append(swatch, document.createTextNode(name + ' ' + value)); $('atlas-legend').append(item);
      });
      if (data.errors.length && !data.nodes.length){
        message.hidden = false; message.textContent = data.errors.map(e => e.letter + ': 索引读取失败').join(' · ');
      } else if (!data.nodes.length){
        message.hidden = false; message.textContent = data.size_state === 'error' ? '大小统计中断，请点击重新统计。' : scanning ? '正在后台统计，群岛即将出现…' : data.building ? '正在建立索引，群岛即将出现…' : $('q').value.trim() ? '这片海域没有匹配的文件，试试其他关键词。' : '这片海域暂时没有可探索的文件。';
      } else message.hidden = true;
      if (data.errors.length && data.nodes.length) $('atlas-caption').textContent += ' · 部分磁盘索引不可用';
      if (!sameNodes) layout();
    } catch(error){
      if (error.name === 'AbortError' || version !== request || view !== 'islands') return;
      current = null; nodes.replaceChildren(); draw();
      ['atlas-files', 'atlas-dirs', 'atlas-groups'].forEach(id => $(id).textContent = '—');
      $('atlas-caption').textContent = '暂时无法读取概览'; $('atlas-legend').replaceChildren();
      message.hidden = false; message.textContent = error.message + '，请刷新重试。';
    } finally {
      if (version === request){
        ocean.setAttribute('aria-busy', 'false');
        if (view === 'islands' && current && current.mode === 'size' && ['counting', 'scanning'].includes(current.size_state))
          sizePoll = setTimeout(() => window.loadIslands(), 1000);
      }
    }
  };

  function hash(text){ let value = 2166136261; for (const c of text) value = Math.imul(value ^ c.charCodeAt(0), 16777619); return value >>> 0; }
  function pack(items){
    const weight = n => current.mode === 'size' ? n.bytes : n.files || n.directories * .2 || 1;
    const largest = Math.max(1, ...items.map(weight));
    const packed = [];
    items.forEach(node => {
      const r = 22 + Math.sqrt(weight(node) / largest) * 28;
      let x = 0, y = 0;
      for (let step = 0; step < 25000; step++){
        const angle = step * .29, radius = Math.sqrt(step) * 2.5;
        x = Math.cos(angle) * radius; y = Math.sin(angle) * radius;
        if (packed.every(p => Math.hypot(x - p.x, y - p.y) > r + p.r + 12)) break;
      }
      packed.push({node, r, x, y});
    });
    return packed;
  }

  function layout(){
    if (!current) return;
    const width = ocean.clientWidth, height = ocean.clientHeight;
    const packed = pack(current.nodes);
    if (!packed.length){ circles = []; nodes.replaceChildren(); draw(); return; }
    const left = Math.min(...packed.map(p => p.x - p.r)), right = Math.max(...packed.map(p => p.x + p.r));
    const top = Math.min(...packed.map(p => p.y - p.r)), bottom = Math.max(...packed.map(p => p.y + p.r));
    const scale = Math.min((width - 48) / (right - left), (height - 126) / (bottom - top), 3);
    const cx = width / 2, cy = height / 2 + 14;
    circles = packed.map(p => ({...p, x: cx + (p.x - (left + right) / 2) * scale,
      y: cy + (p.y - (top + bottom) / 2) * scale, r: p.r * scale}));
    const labelWidth = p => Math.min(150, Math.max(84, p.r * 1.55));
    const overlaps = circles.some((a, i) => circles.slice(i + 1).some(b =>
      Math.abs(a.x - b.x) < (labelWidth(a) + labelWidth(b)) / 2 + 8 &&
      Math.abs(a.y + a.r * .2 - b.y - b.r * .2) < 65));
    if (overlaps){
      // Dense directories keep a readable label for every island. Use spaced
      // rows when circle packing would put labels across each other.
      const cols = Math.max(1, Math.min(packed.length, Math.floor((width - 48) / 118)));
      const rows = Math.ceil(packed.length / cols), cellW = (width - 48) / cols, cellH = (height - 126) / rows;
      circles = packed.map((p, i) => {
        const row = Math.floor(i / cols), column = i % cols, rowCount = Math.min(cols, packed.length - row * cols);
        const radius = Math.min(cellW * .43, cellH * .44);
        return {...p, x: width / 2 + (column - (rowCount - 1) / 2) * cellW,
          y: 72 + cellH * (row + .5), r: radius * (.72 + .28 * (p.r - 22) / 28)};
      });
    }
    nodes.replaceChildren();
    circles.forEach(p => {
      const node = p.node;
      const button = makeButton('', () => {
        if (node.kind === 'directory') navigate(node.path);
        else { searchScope = node.path; searchCategory = node.category; setView('list'); }
      });
      button.className = 'island-node'; button.dataset.path = node.path;
      button.style.left = p.x / width * 100 + '%'; button.style.top = (p.y + p.r * .2) / height * 100 + '%';
      button.style.width = Math.min(150, Math.max(84, p.r * 1.55)) + 'px';
      const title = document.createElement('strong'); title.textContent = node.name;
      const sizeLabel = !node.known_files && node.files ? '未知大小' : fmtSize(node.bytes) + (node.unknown_files ? '+' : '');
      const detail = document.createElement('small'); detail.textContent = current.mode === 'size' ? sizeLabel : count(node.files) + ' 文件';
      const drive = document.createElement('small'); drive.className = 'island-drive'; drive.textContent = node.kind === 'directory' ? count(current.mode === 'size' ? node.files : node.directories) + (current.mode === 'size' ? ' 文件 · ' : ' 目录 · ') + node.path.slice(0, 2) : '查看文件 →';
      const summary = count(node.files) + ' 文件 · ' + count(node.directories) + ' 目录' + (current.mode === 'size' ? ' · ' + sizeLabel + (node.unknown_files ? '（' + count(node.unknown_files) + ' 个大小未知，含待统计）' : '') : '');
      button.append(title, detail, drive); button.title = node.path + '\n' + summary;
      button.setAttribute('aria-label', node.name + '，' + summary + (node.kind === 'directory' ? '，进入目录' : '，查看文件'));
      nodes.append(button);
    });
    draw();
  }

  function sprite(ctx, pattern, x, y, colors, size = 1){
    pattern.forEach((row, iy) => [...row].forEach((pixel, ix) => {
      if (colors[pixel]){ ctx.fillStyle = colors[pixel]; ctx.fillRect(Math.round(x + ix * size), Math.round(y + iy * size), size, size); }
    }));
  }

  function draw(){
    const width = Math.max(1, Math.floor(ocean.clientWidth / 3)), height = Math.max(1, Math.floor(ocean.clientHeight / 3));
    canvas.width = width; canvas.height = height;
    const ctx = canvas.getContext('2d'), styles = getComputedStyle(document.body);
    const water = styles.getPropertyValue('--atlas-water').trim(), grid = styles.getPropertyValue('--atlas-grid').trim();
    const light = document.body.classList.contains('light');
    ctx.fillStyle = water; ctx.fillRect(0, 0, width, height);
    ctx.fillStyle = grid;
    for (let x = 8; x < width; x += 14) for (let y = 6; y < height; y += 14) ctx.fillRect(x, y, 1, 1);
    // Tiny waves, rather than continuous animation, keep the atlas calm.
    for (let i = 0; i < 22; i++){
      const x = (i * 67 + 21) % width, y = (i * 31 + 26) % height;
      ctx.fillRect(x, y, 3, 1); ctx.fillRect(x + 3, y - 1, 2, 1);
    }
    if (!current) return;
    circles.forEach(p => {
      const x = Math.round(p.x / 3), y = Math.round(p.y / 3), r = p.r / 3;
      const seed = hash(p.node.key), color = kinds[p.node.dominant][1];
      for (let iy = -Math.ceil(r * 1.15); iy <= r * 1.15; iy++){
        for (let ix = -Math.ceil(r * 1.2); ix <= r * 1.2; ix++){
          const angle = Math.atan2(iy, ix);
          const edge = 1 + .045 * Math.sin(angle * 7 + seed % 17) + .025 * Math.cos(angle * 11 + seed % 9);
          const d = Math.hypot(ix / r, iy / (r * .82)) / edge;
          if (d > 1.14) continue;
          ctx.fillStyle = d > 1.04 ? (light ? '#9bcacc' : '#245364') : d > .94 ? '#b39668' : d > .82 ? '#e0c68f' : d > .72 ? '#4f7760' : '#73946a';
          if (d < .7 && ((ix * 13 + iy * 19 + seed) & 31) === 0) ctx.fillStyle = '#86a67a';
          ctx.fillRect(x + ix, y + iy, 1, 1);
        }
      }
      // Each island gets a small landmark above its native, keyboard-accessible label.
      const sy = y - Math.max(10, r * .5), sx = x - 4;
      if (p.node.dominant === 'image'){
        sprite(ctx, ['..ppp..','.ppppp.','pphpppp','.ppppp.','..tt...','..tt...','.gggg..'], sx, sy,
          {p:color,h:'#eed0dc',t:'#664d3f',g:'#4f775b'}, 1);
      } else if (p.node.dominant === 'document'){
        sprite(ctx, ['...rr...','..rrrr..','.rrrrrr.','bbbbbbbb','.wbwwbw.','.wbwwbw.','.bbddbb.'], sx, sy,
          {r:'#ae6d55',b:'#e2cc97',w:'#f0e1bb',d:'#66513f'}, 1);
      } else if (p.node.dominant === 'media'){
        sprite(ctx, ['...c....','..chc...','.cchcc..','.ccccc..','..ccc...','...c....','..ss....'], sx, sy,
          {c:color,h:'#d7eee7',s:'#4f7760'}, 1);
      } else if (p.node.dominant === 'archive'){
        sprite(ctx, ['bbbbbbb','bhhhhbb','bbhbhbb','bbbbbbb','bbhhhbb','bbbbbbb'], sx, sy,
          {b:'#886848',h:'#d1aa76'}, 1);
      } else {
        sprite(ctx, ['...g....','..ggg...','.gglgg..','..ggg...','.ggggg..','gggglggg','...t....','...t....'], sx, sy,
          {g:p.node.dominant === 'code' ? '#3b6753' : '#4b6d56',l:color,t:'#70533e'}, 1);
      }
    });
    sprite(ctx, ['..w...','..ww..','..www.','..t...','bbbbbb','.bbbb.'], width - 26, 23,
      {w:'#ddd5b4',t:'#ac8f67',b:'#b78562'}, 1);
  }

  $('atlas-list').onclick = () => { searchCategory = ''; browseAll = true;
    setView('list');
  };
  $('atlas-weight').onchange = () => window.loadIslands();
  $('atlas-rescan').onclick = () => window.loadIslands(true);
  window.atlasStatsChanged = stats => {
    const revision = JSON.stringify(stats.volumes.map(v => [v.letter, v.built_at, v.deltas, v.building]));
    if (lastRevision && revision !== lastRevision && view === 'islands') window.loadIslands();
    lastRevision = revision;
  };
  window.atlasActivity = event => {
    if (view !== 'islands') return;
    [...nodes.children].forEach(button => {
      const path = button.dataset.path.toLowerCase().replace(/\\$/, '') + '\\';
      if (event.path.toLowerCase().startsWith(path)){
        button.classList.add('active'); setTimeout(() => button.classList.remove('active'), 1300);
      }
    });
    if (!activityTimer) activityTimer = setTimeout(() => { activityTimer = null; if (view === 'islands') window.loadIslands(); }, 2500);
  };
  new ResizeObserver(() => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => {
    if (view !== 'islands' || !current) return;
    if (renderedLimit !== mapLimit()) window.loadIslands();
    else layout();
  }, 120); }).observe(ocean);
  new MutationObserver(() => { if (view === 'islands') draw(); }).observe(document.body, {attributes: true, attributeFilter: ['class']});
  setView('islands');
})();
