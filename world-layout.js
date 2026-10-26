/* A connected, area-proportional cartogram. Shared piecewise-linear shears
   bend borders without changing area or opening gaps between territories. */
(function(root, factory){
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.WorldLayout = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function(){
  function area(poly){
    return Math.abs(poly.reduce((sum,p,i) => {const q=poly[(i+1)%poly.length]; return sum+p[0]*q[1]-q[0]*p[1];},0))/2;
  }
  function clip(poly, nx, ny, offset, side){
    const out=[];
    poly.forEach((a,i) => {
      const b=poly[(i+1)%poly.length], da=side*(a[0]*nx+a[1]*ny-offset), db=side*(b[0]*nx+b[1]*ny-offset);
      if (da>=0) out.push(a);
      if ((da>=0)!==(db>=0)){
        const t=da/(da-db); out.push([a[0]+t*(b[0]-a[0]),a[1]+t*(b[1]-a[1])]);
      }
    });
    return out;
  }
  function centroid(poly){
    let twice=0,x=0,y=0;
    poly.forEach((p,i)=>{const q=poly[(i+1)%poly.length],cross=p[0]*q[1]-q[0]*p[1]; twice+=cross;x+=(p[0]+q[0])*cross;y+=(p[1]+q[1])*cross;});
    return Math.abs(twice)>1e-12 ? [x/(3*twice),y/(3*twice)] : poly[0] || [0,0];
  }
  function contains(poly,x,y){
    let inside=false;
    for(let i=0,j=poly.length-1;i<poly.length;j=i++){
      const a=poly[i],b=poly[j];
      if((a[1]>y)!==(b[1]>y) && x<(b[0]-a[0])*(y-a[1])/(b[1]-a[1])+a[0]) inside=!inside;
    }
    return inside;
  }
  function wave(value,step,amplitude,phase){
    const i=Math.floor(value/step),t=value/step-i;
    const offset=k=>amplitude*(.65*Math.sin(k*1.37+phase)+.35*Math.sin(k*2.31+phase*2));
    return offset(i)*(1-t)+offset(i+1)*t;
  }
  function shear(points,axis,step,amplitude,phase,closed){
    const result=[],other=1-axis;
    const mapped=p=>{const q=[...p];q[other]+=wave(p[axis],step,amplitude,phase);return q;};
    const edges=closed?points.length:points.length-1;
    for(let i=0;i<edges;i++){
      const a=points[i],b=points[(i+1)%points.length],delta=b[axis]-a[axis];
      result.push(mapped(a));
      // Split at every shear knot so each segment transforms affinely. Both
      // sides of a shared border therefore get exactly the same bend.
      if(Math.abs(delta)<1e-12) continue;
      const breaks=[];
      for(let k=Math.floor(Math.min(a[axis],b[axis])/step)+1;k*step<Math.max(a[axis],b[axis]);k++){
        const t=(k*step-a[axis])/delta;
        if(t>1e-10 && t<1-1e-10) breaks.push(t);
      }
      breaks.sort((x,y)=>x-y).forEach(t=>result.push(mapped([a[0]+t*(b[0]-a[0]),a[1]+t*(b[1]-a[1])])));
    }
    if(!closed && points.length) result.push(mapped(points[points.length-1]));
    return result;
  }
  function layout(items,width,height,mode){
    const positive=[],zero=[];
    items.forEach(node=>{
      const weight=Math.max(0,Number(mode==='size'?node.bytes:node.files)||0);
      (weight?positive:zero).push({node,weight});
    });
    const padX=Math.min(46,width*.1),padY=64,w=Math.max(1,width-2*padX),h=Math.max(1,height-2*padY);
    let outline=[[0,.32],[.08,.15],[.24,.04],[.53,0],[.81,.08],[.98,.3],[1,.62],[.87,.9],[.6,1],[.24,.93],[.04,.7]]
      .map(([x,y])=>[padX+x*w,padY+y*h]);
    const regions=[];
    function divide(entries,poly,depth){
      if(entries.length===1){
        regions.push({...entries[0],poly,anchor:centroid(poly)});
        return;
      }
      const total=entries.reduce((sum,e)=>sum+e.weight,0);
      let split=1,first=entries[0].weight;
      while(split<entries.length-1 && Math.abs(first+entries[split].weight-total/2)<Math.abs(first-total/2)) first+=entries[split++].weight;
      const xs=poly.map(p=>p[0]),ys=poly.map(p=>p[1]);
      const wide=Math.max(...xs)-Math.min(...xs)>Math.max(...ys)-Math.min(...ys);
      const angle=(wide?0:Math.PI/2)+(depth%2 ? -.23 : .19),nx=Math.cos(angle),ny=Math.sin(angle);
      const dots=poly.map(p=>p[0]*nx+p[1]*ny),target=area(poly)*first/total;
      let low=Math.min(...dots),high=Math.max(...dots);
      for(let i=0;i<64;i++){
        const mid=(low+high)/2;
        if(area(clip(poly,nx,ny,mid,-1))<target) low=mid; else high=mid;
      }
      const offset=(low+high)/2;
      divide(entries.slice(0,split),clip(poly,nx,ny,offset,-1),depth+1);
      divide(entries.slice(split),clip(poly,nx,ny,offset,1),depth+1);
    }
    if(positive.length) divide(positive,outline,0);
    // Each shear has determinant 1; their composition preserves every area's
    // byte/count ratio. Fixed waves also keep repeated layouts deterministic.
    const amplitude=Math.min(12,width*.018,height*.025),step=32;
    const deform=(points,closed)=>shear(shear(points,1,step,amplitude,.7,closed),0,step,amplitude,2.1,closed);
    const point=([x,y])=>{x+=wave(y,step,amplitude,.7);return [x,y+wave(x,step,amplitude,2.1)];};
    outline=deform(outline,true);
    regions.forEach(p=>{
      p.poly=deform(p.poly,true);
      const xs=p.poly.map(q=>q[0]),ys=p.poly.map(q=>q[1]),[x,y]=point(p.anchor);
      const x0=Math.min(...xs),x1=Math.max(...xs),y0=Math.min(...ys),y1=Math.max(...ys);
      const labelWidth=[124,108,92,76].find(w=>w<x1-x0-8 &&
        [-1,-.5,0,.5,1].every(a=>[-1,-.5,0,.5,1].every(b=>contains(p.poly,x+a*w/2,y+b*22)))) || 0;
      Object.assign(p,{area:area(p.poly),x,y,x0,x1,y0,y1,labelWidth,labelVisible:labelWidth>0});
    });
    const roads=[],roadPaths=[],visited=new Set(regions.length?[0]:[]);
    while(visited.size<regions.length){
      let best=null;
      visited.forEach(i=>regions.forEach((b,j)=>{
        if(visited.has(j)) return;
        const a=regions[i],distance=(a.x-b.x)**2+(a.y-b.y)**2;
        if(!best || distance<best.distance) best={i,j,distance};
      }));
      const a=regions[best.i],b=regions[best.j];roads.push([[a.x,a.y],[b.x,b.y]]);
      roadPaths.push(deform([a.anchor,b.anchor],false));visited.add(best.j);
    }
    return {outline:positive.length?outline:[],regions,zero:zero.map(e=>({...e,poly:[],area:0})),
      roads,roadPaths,area:positive.length?area(outline):0};
  }
  return {layout,area,contains};
});
