/* Local, dependency-free pixel atlas. Size mode uses background metadata scans,
   tracks coverage explicitly, and never substitutes file counts for bytes. */
(() => {
  const kinds = {
    code: ['代码', '#89c992'], image: ['图像', '#d59ab7'],
    media: ['影音', '#86bfc7'], document: ['文档', '#d8c286'],
    archive: ['压缩包', '#c59b73'], other: ['其他', '#a5b0a1']
  };
  let request = 0, controller, current, world = {outline:[],regions:[],zero:[],roads:[]}, resizeTimer, activityTimer, sizePoll, currentQuery = '', renderedLimit = 0;
  let lastRevision = '';
  const ocean = $('ocean'), canvas = $('atlas-canvas'), nodes = $('island-nodes'), regionList = $('world-regions');
  const mapLimit = () => Math.min(12, Math.max(1, Math.floor((ocean.clientWidth - 48) / 118)) * Math.max(1, Math.floor((ocean.clientHeight - 126) / 88)));
  const count = n => n.toLocaleString('zh-CN');
  const makeButton = (text, action) => {
    const button = document.createElement('button'); button.type = 'button';
    button.textContent = text; button.onclick = action; return button;
  };

  function crumbs(){
    const bar = $('atlas-crumbs'); bar.replaceChildren();
    bar.append(makeButton('⌂ 世界全图', () => navigate('')));
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
    bar.append(label, makeButton('返回地图', () => { searchCategory = ''; setView('islands'); }));
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
      message.hidden = false; message.textContent = '正在绘制世界地图…';
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
      $('atlas-mode').textContent = data.demo ? '演示地图 · 合成数据' : data.building ? '索引构建中' : '本机索引 · ' + (bySize ? '文件大小' : '文件数量');
      $('map-scale-label').textContent = bySize ? '以已知文件大小为尺度' : '以文件数量为尺度';
      $('atlas-note').textContent = bySize
        ? '地区面积与已知文件逻辑大小成正比；“+”表示仍有未知大小。零字节和未知大小不绘制陆地，仍保留可点击标签。像素边缘有取整误差；不代表实际磁盘占用。'
        : '地区面积与文件数量成正比；空目录不绘制陆地，仍保留可点击标签。概览基于当前可搜索索引，不代表磁盘空间占用。';
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
        (data.omitted_groups ? ' · 其余 ' + count(data.omitted_groups) + ' 个区域可在列表查看' : ' · 点击地区继续探索');
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
        message.hidden = false; message.textContent = data.size_state === 'error' ? '大小统计中断，请点击重新统计。' : scanning ? '正在后台统计，地图即将出现…' : data.building ? '正在建立索引，地图即将出现…' : $('q').value.trim() ? '当前地区没有匹配的文件，试试其他关键词。' : '当前地区暂时没有可探索的文件。';
      } else message.hidden = true;
      if (data.errors.length && data.nodes.length) $('atlas-caption').textContent += ' · 部分磁盘索引不可用';
      if (!sameNodes) layout();
    } catch(error){
      if (error.name === 'AbortError' || version !== request || view !== 'islands') return;
      current = null; nodes.replaceChildren(); regionList.replaceChildren(); draw();
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
  function openRegion(node){
    if (node.kind === 'directory') navigate(node.path);
    else { searchScope = node.path; searchCategory = node.category; setView('list'); }
  }
  function nodeSummary(node){
    const sizeLabel = !node.known_files && node.files ? '未知大小' : fmtSize(node.bytes) + (node.unknown_files ? '+' : '');
    const value = current.mode === 'size' ? sizeLabel : count(node.files) + ' 文件';
    return {value, summary: count(node.files) + ' 文件 · ' + count(node.directories) + ' 目录 · ' + value};
  }
  function terrainColor(node){
    const palettes = {
      code:['#6d8c64','#82966c','#6c8470','#92a679'], image:['#a18b9c','#b29ba8','#958c9f'],
      media:['#789591','#8ba29a','#6c8988'], document:['#b5a37a','#c2b488','#a99f75'],
      archive:['#9c896b','#ac9b79','#96896f'], other:['#8d9982','#a1a98d','#7e9077']
    };
    const choices=palettes[node.dominant] || palettes.other;
    return choices[hash(node.key)%choices.length];
  }
  function layout(){
    if (!current) return;
    const width = ocean.clientWidth, height = ocean.clientHeight;
    world = WorldLayout.layout(current.nodes, width, height, current.mode);
    nodes.replaceChildren(); regionList.replaceChildren();
    world.regions.forEach(p => {
      const node=p.node, {value,summary}=nodeSummary(node);
      const button=makeButton('',()=>openRegion(node));
      button.className='world-region'; button.dataset.path=node.path;
      button.dataset.bytes=String(node.bytes); button.dataset.landArea=String(p.area);
      const w=p.x1-p.x0,h=p.y1-p.y0;
      button.style.left=p.x0/width*100+'%'; button.style.top=p.y0/height*100+'%';
      button.style.width=w/width*100+'%'; button.style.height=h/height*100+'%';
      button.style.clipPath='polygon('+p.poly.map(([x,y])=>((x-p.x0)/w*100)+'% '+((y-p.y0)/h*100)+'%').join(',')+')';
      button.title=node.path+'\n'+summary;
      button.setAttribute('aria-label',node.name+'，'+summary+(node.kind==='directory'?'，进入目录':'，查看文件'));
      if(p.labelVisible){
        const label=document.createElement('span');label.className='world-label';
        label.style.left=(p.x-p.x0)/w*100+'%';label.style.top=(p.y-p.y0)/h*100+'%';label.style.width=p.labelWidth+'px';
        const title=document.createElement('strong');title.textContent=node.name;
        const detail=document.createElement('small');detail.textContent=value;
        label.append(title,detail);button.append(label);
      }
      nodes.append(button);
    });
    current.nodes.forEach(node=>{
      const {value,summary}=nodeSummary(node),button=makeButton('',()=>openRegion(node));
      const swatch=document.createElement('i');swatch.className='legend-mark';swatch.style.background=terrainColor(node);
      const name=document.createElement('span');name.textContent=node.name;
      const size=document.createElement('small');size.textContent=value;
      button.append(swatch,name,size);button.title=node.path+'\n'+summary;
      button.setAttribute('aria-label',node.name+'，'+summary+'，地区清单');
      regionList.append(button);
    });
    if(current.nodes.length && !world.regions.length && !['counting','scanning'].includes(current.size_state)){
      $('atlas-message').hidden=false;
      $('atlas-message').textContent='当前没有可分配面积的地区，可从下方清单继续探索。';
    }
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
    if (!world.outline.length) return;
    const sx=width/ocean.clientWidth,sy=height/ocean.clientHeight;
    const path=poly=>{ctx.beginPath();poly.forEach(([x,y],i)=>i?ctx.lineTo(x*sx,y*sy):ctx.moveTo(x*sx,y*sy));ctx.closePath();};
    // A single coast surrounds the entire connected landmass.
    path(world.outline);ctx.fillStyle='#82966c';ctx.fill();ctx.lineJoin='round';
    ctx.strokeStyle=light?'#9dbfc0':'#2c5864';ctx.lineWidth=6;ctx.stroke();
    ctx.strokeStyle='#d9bf8c';ctx.lineWidth=3;ctx.stroke();
    world.regions.forEach(p=>{
      const color=terrainColor(p.node),seed=hash(p.node.key);
      path(p.poly);ctx.fillStyle=color;ctx.fill();
      ctx.save();path(p.poly);ctx.clip();
      for(let y=Math.floor(p.y0*sy);y<p.y1*sy;y++) for(let x=Math.floor(p.x0*sx);x<p.x1*sx;x++){
        const pattern=(Math.imul(x,31)^Math.imul(y,73)^seed)>>>0;
        if(pattern%37===0){ctx.fillStyle='rgba(240,235,190,.16)';ctx.fillRect(x,y,2,1);}
        if(pattern%113===0){ctx.fillStyle='rgba(31,55,42,.16)';ctx.fillRect(x,y,1,2);}
      }
      // Small tree clusters and mountain ridges share the same terrain.
      for(let y=Math.ceil(p.y0*sy)+8;y<p.y1*sy-6;y+=14) for(let x=Math.ceil(p.x0*sx)+7;x<p.x1*sx-6;x+=13){
        const pattern=(x*31+y*17+seed)>>>0;
        if(pattern%4 || !WorldLayout.contains(p.poly,x/sx,y/sy)) continue;
        if(p.node.dominant==='archive' || pattern%3===0){
          sprite(ctx,['...a...','..aba..','.abbba.','abcccba'],x-3,y-3,{a:'#6b7563',b:'#b6b59b',c:'#829079'});
        }else{
          sprite(ctx,['..g..','.ggg.','gglgg','.ggg.','..t..'],x-2,y-3,{g:'#44634f',l:'#668261',t:'#6e6046'});
        }
      }
      ctx.restore();
      // Borders are ink on shared land, never water between territories.
      path(p.poly);ctx.strokeStyle='rgba(51,67,46,.65)';ctx.lineWidth=.8;ctx.stroke();
    });
    ctx.strokeStyle='#d6bc84';ctx.lineWidth=.8;ctx.setLineDash([2,1]);
    world.roads.forEach(([a,b])=>{ctx.beginPath();ctx.moveTo(a[0]*sx,a[1]*sy);ctx.lineTo(b[0]*sx,b[1]*sy);ctx.stroke();});
    ctx.setLineDash([]);
    world.regions.forEach(p=>{
      ctx.fillStyle='#445442';ctx.fillRect(Math.round(p.x*sx)-2,Math.round(p.y*sy)-2,5,5);
      ctx.fillStyle='#e6d6a2';ctx.fillRect(Math.round(p.x*sx)-1,Math.round(p.y*sy)-1,3,3);
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
