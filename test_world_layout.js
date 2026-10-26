const assert=require('node:assert/strict');
const {test}=require('node:test');
const {layout,area,contains}=require('./world-layout.js');
const nodes=values=>values.map((bytes,i)=>({key:String(i),bytes,files:i+1}));
function verify(world){
  const total=world.regions.reduce((sum,p)=>sum+p.weight,0);
  const sum=world.regions.reduce((sum,p)=>sum+p.area,0);
  assert.ok(Math.abs(sum-world.area)<Math.max(1,world.area)*1e-10,'the continent has gaps or overlapping area');
  world.regions.forEach(p=>{
    assert.ok(Math.abs(p.area/world.area-p.weight/total)<1e-10,'territory area is not proportional');
    assert.ok(contains(world.outline,p.x,p.y),'territory center is outside the continent');
    assert.ok(contains(p.poly,p.x,p.y),'village center is outside its own territory');
  });
}
test('one connected continent allocates four times the area for four times the bytes',()=>{
  const world=layout(nodes([1,4,16]),900,500,'size');
  verify(world);
  assert.ok(Math.abs(world.regions[1].area/world.regions[0].area-4)<1e-10);
});
test('territories tile the world without internal ocean, holes, or overlaps',()=>{
  const world=layout(nodes([8,7,6,5,4,3,2,1]),700,430,'size');
  verify(world);
  for(let x=46.37;x<654;x+=9.13) for(let y=64.71;y<366;y+=8.79){
    const covered=world.regions.filter(p=>contains(p.poly,x,y)).length;
    assert.equal(covered,contains(world.outline,x,y)?1:0);
  }
});
test('roads connect all territory centers and stay on the same landmass',()=>{
  const world=layout(nodes([100,90,20,10,5,2]),700,430,'size');
  assert.equal(world.roads.length,world.regions.length-1);
  const key=p=>p.join(','),visited=new Set([key([world.regions[0].x,world.regions[0].y])]);
  for(let round=0;round<world.regions.length;round++) world.roads.forEach(([a,b])=>{
    if(visited.has(key(a)) || visited.has(key(b))){visited.add(key(a));visited.add(key(b));}
    for(let t=.01;t<1;t+=.1) assert.ok(contains(world.outline,a[0]+(b[0]-a[0])*t,a[1]+(b[1]-a[1])*t));
  });
  assert.equal(visited.size,world.regions.length);
});
test('resizing and dense data preserve area ratios and bounds',()=>{
  for(const [w,h] of [[320,440],[700,430],[960,340],[1600,540]]){
    const world=layout(nodes([1000000,10000,1000,100,10,1]),w,h,'size');
    verify(world);
    world.regions.forEach(p=>p.poly.forEach(([x,y])=>assert.ok(x>=0&&x<=w&&y>=0&&y<=h)));
  }
});
test('zero and unknown bytes receive no territory; empty maps have no fake land',()=>{
  const world=layout(nodes([0,undefined,1]),700,430,'size');
  assert.equal(world.zero.length,2);world.zero.forEach(p=>assert.equal(p.area,0));verify(world);
  assert.equal(layout(nodes([0,undefined]),700,430,'size').outline.length,0);
  assert.equal(layout([],700,430,'size').area,0);
});
test('count mode tiles the same world by file count',()=>{
  const world=layout([{files:1},{files:4},{files:0}],700,430,'count');
  verify(world);assert.equal(world.zero.length,1);
});

test('perturbed borders and roads are deterministic, bent, and remain on land',()=>{
  const world=layout(nodes([100,90,20,10,5,2]),900,500,'size');
  assert.deepEqual(world,layout(nodes([100,90,20,10,5,2]),900,500,'size'));
  assert.ok(world.regions.some(p=>p.poly.length>15),'borders have no intermediate bends');
  assert.equal(world.roadPaths.length,world.roads.length);
  let bent=false;
  world.roadPaths.forEach((path,i)=>{
    const [a,b]=world.roads[i];
    assert.deepEqual(path[0],a);assert.deepEqual(path[path.length-1],b);
    path.forEach((p,j)=>{
      assert.ok(contains(world.outline,...p));
      const distance=Math.abs((b[0]-a[0])*(p[1]-a[1])-(b[1]-a[1])*(p[0]-a[0]))/Math.hypot(b[0]-a[0],b[1]-a[1]);
      if(distance>1) bent=true;
      if(j) for(let t=.1;t<1;t+=.2){
        const q=path[j-1]; assert.ok(contains(world.outline,q[0]+(p[0]-q[0])*t,q[1]+(p[1]-q[1])*t));
      }
    });
  });
  assert.ok(bent,'roads are still straight');
  verify(world);
});
