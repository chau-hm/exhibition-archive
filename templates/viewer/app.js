import { createViewer } from './vendor/index.js';

const $ = id => document.getElementById(id);
const ids = ['serverStatus','modelStatus','modelHash','viewerHost','selectionOverlay','graphics','cameraPosition','decision','mode','modeNote','speed','speedLabel','station','goStation','saveBookmark','restoreBookmark','returnExplore','tour','playTour','artwork','orbitArtwork','boundarySummary','boundaryMap','showCameras','showWalls','showCandidates','showNeighborGraph','exportBoundary','diagnosticScene','diagnosticPose','captureDiagnostic','sourcePhoto','annotationToggle','annotationClear','annotationSave','annotationExport','annotationLabel','annotationStatus','annotationList','acceptanceChecklist','exportAcceptance','resetAcceptance','runtimeSummary','exportCamera'];
const el = Object.fromEntries(ids.map(id => [id, $(id)]));
let health, boundary, generatedBoundary, stationsDoc, initialCameraDoc, tourDoc, candidateDoc, artifactEvidence, cameraDoc, footprintDoc, distributionDoc, annotationsDoc, handle;
let activeMode = 'visitor-explore';
let navArmed = false, pointerDragging = false, lastPointer = null;
let frameId = 0, lastFrameAt = 0, tourRaf = 0;
let routeAdvanceForAcceptance = null;
let keyState = new Set();
let orbitCenter = null;
let mapTransform=null, annotationActive=false, annotationDrawing=false, annotationDraft=[], annotationPose=null, annotationPreset='front', annotationSaved=[];
let lastBlock=null;
const clearanceDefault = 0.08;
const bookmarkKey = 'exhibition-archive-free-gallery-bookmark-v1';
const acceptanceKey = 'exhibition-archive-human-acceptance-v1';
const up = () => normalize(boundary.floor.normal);
const vAdd = (a,b) => a.map((x,i)=>x+b[i]);
const vSub = (a,b) => a.map((x,i)=>x-b[i]);
const vScale = (a,s) => a.map(x=>x*s);
const dot = (a,b) => a.reduce((sum,x,i)=>sum+x*b[i],0);
const cross = (a,b) => [a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]];
const length = a => Math.hypot(...a);
const normalize = a => { const d=length(a)||1; return a.map(x=>x/d); };
const clamp = (x,a,b) => Math.max(a,Math.min(b,x));
const xyz = p => [p[0],p[1],p[2]];
const floorDistance = p => dot(boundary.floor.normal,p)+boundary.floor.offset;
const ceilingDistance = p => floorDistance(p);
function horizontalCoordinates(p) {
  const basis=boundary.horizontalBoundary.planeBasis, projected=vSub(p,vScale(up(),floorDistance(p))), d=vSub(projected,basis.pointOnPlaneCommonViewer);
  return [dot(d,basis.basisU),dot(d,basis.basisV)];
}
function pointInPolygon(point,poly) {
  let inside=false;const [x,y]=point;
  for(let i=0,j=poly.length-1;i<poly.length;j=i++){
    const a=poly[i],b=poly[j];if(pointSegmentDistance(point,a,b)<1e-8)return true;
    if(((a[1]>y)!==(b[1]>y))&&x<(b[0]-a[0])*(y-a[1])/(b[1]-a[1])+a[0])inside=!inside;
  }
  return inside;
}
function pointSegmentDistance(p,a,b) {
  const dx=b[0]-a[0],dy=b[1]-a[1],den=dx*dx+dy*dy,t=clamp(((p[0]-a[0])*dx+(p[1]-a[1])*dy)/(den||1),0,1);
  return Math.hypot(p[0]-a[0]-t*dx,p[1]-a[1]-t*dy);
}
function nearestBoundaryEdge(point,poly=boundary.horizontalBoundary.vertices2d) {
  let best={edgeIndex:-1,distance:Infinity};
  for(let i=0;i<poly.length;i++){const d=pointSegmentDistance(point,poly[i],poly[(i+1)%poly.length]);if(d<best.distance)best={edgeIndex:i,distance:d};}
  return best;
}
function validPosition(p, margin=boundary.horizontalBoundary.cameraClearanceUnits) {
  if (!p||p.length!==3||!p.every(Number.isFinite)) return false;
  const h=floorDistance(p),low=boundary.floor.clearance,high=boundary.ceiling.heightAlongFloorNormal-boundary.ceiling.clearance;
  if(h<low-1e-6||h>high+1e-6)return false;
  const xy=horizontalCoordinates(p),poly=boundary.horizontalBoundary.vertices2d;
  return pointInPolygon(xy,poly)&&nearestBoundaryEdge(xy,poly).distance>=margin-1e-6;
}
function segmentAllowed(from,to) {
  if(!from||!to||from.length!==3||to.length!==3||![...from,...to].every(Number.isFinite)){lastBlock={reason:'invalid-pose',edgeIndex:null};return false;}
  const low=boundary.floor.clearance,high=boundary.ceiling.heightAlongFloorNormal-boundary.ceiling.clearance,h0=floorDistance(from),h1=floorDistance(to);
  if(Math.min(h0,h1)<low-1e-6||Math.max(h0,h1)>high+1e-6){lastBlock={reason:h1<low?'floor':'ceiling',attempted:[...to],accepted:[...from],edgeIndex:null};return false;}
  const a=horizontalCoordinates(from),b=horizontalCoordinates(to),delta=Math.hypot(b[0]-a[0],b[1]-a[1]),poly=boundary.horizontalBoundary.vertices2d;
  const orient=(p,q,r)=>(q[0]-p[0])*(r[1]-p[1])-(q[1]-p[1])*(r[0]-p[0]);
  for(let e=0;e<poly.length;e++){const c=poly[e],d=poly[(e+1)%poly.length],o1=orient(a,b,c),o2=orient(a,b,d),o3=orient(c,d,a),o4=orient(c,d,b);if(o1*o2<-1e-12&&o3*o4<-1e-12){lastBlock={reason:'horizontal-enclosure',attempted:[...to],accepted:[...from],edgeIndex:e,boundarySource:boundary.horizontalBoundary.classification};return false;}}
  const steps=Math.max(1,Math.ceil(delta/Math.max(.012,Math.min(.04,(boundary.horizontalBoundary.cameraClearanceUnits||.02)*.5))));
  for(let i=1;i<=steps;i++){
    const t=i/steps,p=vAdd(from,vScale(vSub(to,from),t));
    if(!validPosition(p)){
      const edge=nearestBoundaryEdge(horizontalCoordinates(p));
      lastBlock={reason:'horizontal-enclosure',attempted:[...to],accepted:[...vAdd(from,vScale(vSub(to,from),Math.max(0,(i-1)/steps)))],edgeIndex:edge.edgeIndex,distanceToBoundary:edge.distance,boundarySource:boundary.horizontalBoundary.classification};return false;
    }
  }
  return true;
}
function clampVertical(point) {
  const n=boundary.floor.normal, h=floorDistance(point);
  const low=boundary.floor.clearance, high=boundary.ceiling.heightAlongFloorNormal-boundary.ceiling.clearance;
  if (h<low) return vAdd(point,vScale(n,low-h));
  if (h>high) return vAdd(point,vScale(n,high-h));
  return point;
}
function resolveMove(from, desired) {
  const target=clampVertical(desired);
  if(length(vSub(target,desired))>1e-8){const requestedHeight=floorDistance(desired),reason=requestedHeight<boundary.floor.clearance?'floor':'ceiling';lastBlock={reason,attempted:[...desired],accepted:[...target],edgeIndex:null,boundarySource:`accepted enclosure ${reason} constraint`};}
  if(segmentAllowed(from,target))return target;
  const reason=lastBlock?{...lastBlock}:null;
  let lo=0,hi=1;
  for(let i=0;i<22;i++){const mid=(lo+hi)/2,p=vAdd(from,vScale(vSub(target,from),mid));if(segmentAllowed(from,p))lo=mid;else hi=mid;}
  let hit=vAdd(from,vScale(vSub(target,from),lo));
  if(reason?.reason!=='horizontal-enclosure')return hit;
  const xy=horizontalCoordinates(hit),poly=boundary.horizontalBoundary.vertices2d,edge=nearestBoundaryEdge(xy,poly).edgeIndex;
  if(edge<0)return hit;
  const a=poly[edge],b=poly[(edge+1)%poly.length],dx=b[0]-a[0],dy=b[1]-a[1],len=Math.hypot(dx,dy)||1;
  let area=0;for(let i=0;i<poly.length;i++)area+=poly[i][0]*poly[(i+1)%poly.length][1]-poly[(i+1)%poly.length][0]*poly[i][1];
  const inward=area>=0?[-dy/len,dx/len]:[dy/len,-dx/len],remaining=vSub(target,hit),basis=boundary.horizontalBoundary.planeBasis;
  const r2=[dot(remaining,basis.basisU),dot(remaining,basis.basisV)],slide2=vSub(r2,vScale(inward,dot(r2,inward))),vertical=dot(remaining,up());
  const slide=vAdd(vAdd(vScale(basis.basisU,slide2[0]),vScale(basis.basisV,slide2[1])),vScale(up(),vertical)),slid=vAdd(hit,slide);
  if(segmentAllowed(hit,slid))hit=slid;
  lastBlock={...reason,attempted:[...desired],accepted:[...hit],edgeIndex:edge,boundarySource:boundary.horizontalBoundary.classification};
  return hit;
}
function policyValidator(from,to) { return segmentAllowed(from,to); }
function logDecision(message,ok=false) {
  el.decision.textContent=message;
  el.decision.style.borderColor=ok?'#5fa16f':'#d3a15d';
}
function runtimeAvailable() { return !!handle&&!!handle.state.loaded; }
function activePolicy() { return runtimeAvailable()?'architectural-envelope':'unavailable'; }
function getPose() { return runtimeAvailable()?handle.getCameraPose():null; }
function cameraForward(pose=getPose()) { return pose?normalize(vSub(pose.target,pose.position)):[0,0,-1]; }
function boundaryEventText() {
  if(!lastBlock)return 'Last movement limit: none recorded in this session.';
  const fmt=p=>Array.isArray(p)?`[${p.map(v=>Number(v).toFixed(3)).join(', ')}]`:'unknown';
  let segment='';
  if(lastBlock.reason==='horizontal-enclosure'&&Number.isInteger(lastBlock.edgeIndex)){
    const poly=boundary.horizontalBoundary.vertices2d,i=lastBlock.edgeIndex,a=poly[i],b=poly[(i+1)%poly.length];
    segment=` · edge ${i+1}/${poly.length} floor-plane UV ${fmt(a)} → ${fmt(b)}`;
  }
  return `Last movement limit: ${lastBlock.reason} · attempted ${fmt(lastBlock.attempted)} · accepted ${fmt(lastBlock.accepted)}${segment} · source ${lastBlock.boundarySource??boundary.horizontalBoundary.classification}`;
}
function poseFacing(position,forward,base=getPose(),fov=undefined) {
  const dir=normalize(forward),dist=base?Math.max(0.5,length(vSub(base.target,base.position))):2;
  return {position:[...position],target:vAdd(position,vScale(dir,dist)),fov:Number.isFinite(fov)?fov:(base?.fov??70.75)};
}
function applyPose(pose) {
  if (!runtimeAvailable()) return false;
  if (!segmentAllowed(getPose().position,pose.position)) return false;
  return handle.setCameraPose(pose);
}
function renderDpr() { return window.devicePixelRatio||1; }
function canvasMetadata() {
  const canvas=el.viewerHost.querySelector('canvas:not(#selectionOverlay)');
  return {css:[canvas?.clientWidth??0,canvas?.clientHeight??0],drawingBuffer:[canvas?.width??0,canvas?.height??0],devicePixelRatio:renderDpr(),renderer:handle?.app?.graphicsDevice?.deviceType??'unknown'};
}
function updatePoseHud() {
  if (!runtimeAvailable()) return;
  const p=getPose().position;
  el.cameraPosition.textContent=`Camera x ${p[0].toFixed(2)} · y ${p[1].toFixed(2)} · z ${p[2].toFixed(2)} · floor ${floorDistance(p).toFixed(2)} u`;
  el.runtimeSummary.textContent=`Mode: ${activeMode}\nShared policy: ${activePolicy()}\nPosition: [${p.map(x=>x.toFixed(4)).join(', ')}]\nSigned floor distance: ${floorDistance(p).toFixed(4)} u\nCeiling distance: ${(boundary.ceiling.heightAlongFloorNormal-floorDistance(p)).toFixed(4)} u\nRenderer: WebGL · archive viewer rendering settings\nHorizontal boundary: ${boundary.horizontalBoundary.algorithm} · ${boundary.horizontalBoundary.classification} · closed polygon · clearance ${boundary.horizontalBoundary.cameraClearanceUnits.toFixed(2)} u\n${boundaryEventText()}\nCanvas CSS: ${canvasMetadata().css.join(' × ')} · drawing buffer: ${canvasMetadata().drawingBuffer.join(' × ')} · DPR ${renderDpr()}\nLegacy finite wall collisions: 0 · furniture collision: none · camera-station/raster limits: none`;
  drawBoundaryMap();
}
function clearTour() { if(tourRaf) cancelAnimationFrame(tourRaf); tourRaf=0; routeAdvanceForAcceptance=null; }
function setMode(mode) {
  if(!runtimeAvailable()) return;
  if(!['visitor-explore','guided-tour','artwork-orbit'].includes(mode)) throw new Error('This published viewer mode is unavailable.');
  clearTour();
  const current=getPose();
  if(!validPosition(current.position)) {
    const spawn=spawnPose();
    if(!handle.setCameraPose(spawn)) throw new Error('Could not restore the safe Visitor Explore spawn.');
  }
  handle.setNavigationPolicy('architectural-envelope',getPose(),policyValidator);
  activeMode=mode;
  if(mode==='visitor-explore') logDecision('Visitor Explore: free XYZ movement inside the closed camera-derived boundary, floor, and ceiling. Internal exhibits do not collide.',true);
  else if(mode==='guided-tour') logDecision('Guided Tour is constrained by the same finite architectural boundary policy.',true);
  else logDecision('Artwork Orbit is constrained by the same finite architectural boundary policy.',true);
  el.mode.value=mode;
  updateModeText();
  updatePoseHud();
}
function updateModeText() {
  const notes={
    'visitor-explore':'W/A/S/D move freely parallel to the floor; E/Space ascend; Q/Ctrl descend; drag to look. Only the closed camera-derived enclosure, floor, and ceiling constrain position. Furniture and artwork have no collision.',
    'guided-tour':'A saved route plays through the shared camera-derived boundary validator. Click the viewer and use mouse-look to interrupt.',
    'artwork-orbit':'Mouse drag orbits the selected focus; every camera position remains subject to the shared camera-derived enclosure/floor/ceiling policy.'
  };
  el.modeNote.textContent=notes[activeMode];
}
function stationPose(station) {
  const p=station.centerCommonViewer, f=station.representativeForwardCommonViewer;
  return poseFacing(p,f,getPose(),70.75);
}
function spawnPose() {
  const canonical=initialCameraDoc?.pose_common_viewer;
  if(canonical&&Array.isArray(canonical.position)&&Array.isArray(canonical.target))return {position:[...canonical.position],target:[...canonical.target],fov:canonical.fov??70.75};
  const preferred=stationsDoc.stations.find(s=>s.stationId===stationsDoc.defaultStationId)??stationsDoc.stations.find(s=>s.normalVisitorPositionCandidate)??stationsDoc.stations[0];
  return stationPose(preferred);
}
function goToStation() {
  const station=stationsDoc.stations.find(x=>x.stationId===el.station.value);
  if(!station||!runtimeAvailable()) return;
  setMode('visitor-explore');
  const ok=applyPose(stationPose(station));
  logDecision(ok?`Reached ${station.stationId} · ${station.imageKeys?.[0]??'trusted camera'} through the shared policy.`:`${station.stationId} was rejected: its direct segment crosses an active boundary or leaves the floor/ceiling envelope.`,ok);
  updatePoseHud();
}
function getCandidate(id) { return candidateDoc.candidates.find(x=>x.candidateId===id); }
function sceneSpec(scene) {
  const c=getCandidate(scene)??candidateDoc.candidates.find(x=>x.candidateId===candidateDoc.defaultCandidateId)??candidateDoc.candidates[0];
  if(!c) throw new Error('No geometry viewpoint is configured for this project.');
  const station=stationsDoc.stations.find(s=>s.stationId===c.preferredStationId);
  const front=station?normalize(vSub(station.centerCommonViewer,c.commonCenter)):normalize(vScale(c.commonRotationRows?.[2]??[0,0,1],-1));
  return {id:c.candidateId,center:c.commonCenter,front,halfExtents:c.commonHalfExtents,axes:c.commonRotationRows};
}
function rotateAround(vector,axis,angle) {
  const a=normalize(axis),c=Math.cos(angle),s=Math.sin(angle);
  return vAdd(vAdd(vScale(vector,c),vScale(cross(a,vector),s)),vScale(a,dot(a,vector)*(1-c)));
}
function presetPose(scene,preset) {
  const spec=sceneSpec(scene),u=up();
  let front=spec.front;
  let offset=0,angle=0;
  if(preset==='left20') angle=-20*Math.PI/180;
  if(preset==='right20') angle=20*Math.PI/180;
  if(preset==='left40') angle=-40*Math.PI/180;
  if(preset==='right40') angle=40*Math.PI/180;
  if(preset==='elevated') offset=0.7;
  if(preset==='lower') offset=-0.5;
  front=normalize(rotateAround(front,u,angle));
  const distance=2.8;
  let position=vAdd(spec.center,vScale(front,distance));
  if(offset) position=vAdd(position,vScale(u,offset));
  const target=vAdd(spec.center,vScale(u,offset*0.35));
  const pose={position,target,fov:70.75};
  return {pose,spec,angleDegrees:angle*180/Math.PI,distance,projectedDimensions:projectedDimensions(spec,distance,angle)};
}
function projectedDimensions(spec,distance,angle) {
  const canvas=canvasMetadata(),height=canvas.drawingBuffer[1]||canvas.css[1]||720;
  const half=spec.halfExtents??[0.25,0.25,0.02];
  const axes=spec.axes??[[0,1,0],[1,0,0],[0,0,1]];
  const verticalHalf=Math.max(half[0],half[1]);
  const horizontalHalf=Math.min(half[0],half[1]);
  const viewFactor=Math.max(.1,Math.cos(Math.abs(angle)));
  const focal=height/(2*Math.tan(70.75*Math.PI/360));
  return {widthPx:Math.round(2*horizontalHalf*viewFactor*focal/distance),heightPx:Math.round(2*verticalHalf*focal/distance),method:'Approximate oriented-box projection using current renderer drawing-buffer height and fixed camera distance; no image alignment.'};
}
function cameraAnglesToTarget(pose,target) {
  const d=normalize(vSub(target,pose.position));
  const yaw=Math.atan2(d[2],d[0]),pitch=Math.asin(clamp(d[1],-1,1));
  return {yaw,pitch,distance:length(vSub(target,pose.position))};
}
function withYawPitch(position,yaw,pitch,distance,fov) {
  pitch=clamp(pitch,-Math.PI/2+0.02,Math.PI/2-0.02);
  const f=[Math.cos(pitch)*Math.cos(yaw),Math.sin(pitch),Math.cos(pitch)*Math.sin(yaw)];
  return {position:[...position],target:vAdd(position,vScale(f,distance)),fov};
}
function movementAxes(pose) {
  const u=up(),f0=cameraForward(pose),f=normalize(vSub(f0,vScale(u,dot(f0,u))));
  const r=normalize(cross(f,u));
  return {forward:f,right:r,up:u};
}
function moveByInput(dt) {
  if(!runtimeAvailable()||!keyState.size||!['visitor-explore','artwork-orbit'].includes(activeMode)) return;
  const pose=getPose(),axes=movementAxes(pose);
  const forward=(keyState.has('w')?1:0)-(keyState.has('s')?1:0);
  const strafe=(keyState.has('d')?1:0)-(keyState.has('a')?1:0);
  const vertical=(keyState.has('e')||keyState.has(' ')?1:0)-(keyState.has('q')||keyState.has('control')?1:0);
  let delta=vAdd(vAdd(vScale(axes.forward,forward),vScale(axes.right,strafe)),vScale(axes.up,vertical));
  if(length(delta)<1e-8) return;
  delta=normalize(delta);
  const speed=Number(el.speed.value)*(keyState.has('shift')?3:1);
  const desired=vAdd(pose.position,vScale(delta,speed*dt));
  const target=resolveMove(pose.position,desired);
  if(length(vSub(target,pose.position))<1e-7) { updatePoseHud(); return; }
  let next;
  if(activeMode==='artwork-orbit'&&orbitCenter) {
    const offset=vSub(target,orbitCenter),forward=normalize(vSub(orbitCenter,target));
    next={position:target,target:vAdd(target,vScale(forward,length(offset))),fov:pose.fov};
  } else {
    next=poseFacing(target,cameraForward(pose),pose,pose.fov);
  }
  const ok=applyPose(next);
  if(!ok) logDecision('Camera movement was stopped by the shared architectural boundary policy.',false);
  updatePoseHud();
}
function lookByPointer(dx,dy) {
  if(!runtimeAvailable()||!['visitor-explore','artwork-orbit'].includes(activeMode)) return;
  const pose=getPose(),sensitivity=0.0032;
  if(activeMode==='artwork-orbit'&&orbitCenter) {
    const offset=vSub(pose.position,orbitCenter),yawed=rotateAround(offset,up(),-dx*sensitivity);
    const right=normalize(cross(normalize(vSub(orbitCenter,pose.position)),up()));
    const pitched=rotateAround(yawed,right,dy*sensitivity);
    const desired={position:vAdd(orbitCenter,pitched),target:[...orbitCenter],fov:pose.fov};
    if(segmentAllowed(pose.position,desired.position)) applyPose(desired);
  } else {
    const {yaw,pitch,distance}=cameraAnglesToTarget(pose,pose.target);
    const next=withYawPitch(pose.position,yaw-dx*sensitivity,pitch-dy*sensitivity,distance,pose.fov);
    applyPose(next);
  }
  updatePoseHud();
}
function pointOnWallAtT(wall,t,fromFloor=2.3,distance=0.1) {
  const g=wall.geometry,n=g.normal,upN=boundary.floor.normal,planeD=wallDistance(wall,[0,0,0]);
  const nh=Math.hypot(n[0],n[2])||1,u=g.tangentXZ;
  const normal2=[n[0]/nh,n[2]/nh];
  const baseX=u[0]*t,baseZ=u[1]*t;
  const wallTarget=wall.geometry.insideSign*distance;
  // xz = tangent*t + horizontal-normal*alpha; y is set from the floor-height plane.
  const base=[baseX,0,baseZ];
  const baseFloor=dot(upN,base)+boundary.floor.offset;
  const baseY=(fromFloor-baseFloor)/upN[1];
  const baseWall=wall.geometry.insideSign*(dot(n,[baseX,baseY,baseZ])+g.offset);
  const alpha=(wallTarget-baseWall)/(wall.geometry.insideSign*(n[0]*normal2[0]+n[2]*normal2[1]+n[1]*(-(upN[0]*normal2[0]+upN[2]*normal2[1])/upN[1])));
  const x=baseX+normal2[0]*alpha,z=baseZ+normal2[1]*alpha;
  const y=(fromFloor-boundary.floor.offset-upN[0]*x-upN[2]*z)/upN[1];
  return [x,y,z];
}
function wallApproachPose(wallId, floorHeight=2.3, signedDistance=0.2, fraction=0.5) {
  const wall=boundary.wallSegments.find(x=>x.id===wallId&&x.enabled);
  if(!wall) return null;
  const t=wall.geometry.tangentMin+(wall.geometry.tangentMax-wall.geometry.tangentMin)*fraction;
  const p=pointOnWallAtT(wall,t,floorHeight,signedDistance);
  const f=vScale(normalize(wall.geometry.normal),-wall.geometry.insideSign);
  return poseFacing(p,f,getPose(),70.75);
}
function wallFloorPoint(wall,t,height=1.2,distance=0) {
  const g=wall.geometry,n=g.normal,upN=boundary.floor.normal,tt=g.tangentXZ,nh=Math.hypot(n[0],n[2])||1,normal2=[n[0]/nh,n[2]/nh];
  const base=[tt[0]*t,0,tt[1]*t],baseFloor=dot(upN,base)+boundary.floor.offset,baseY=(height-baseFloor)/upN[1];
  const baseWall=wall.geometry.insideSign*(dot(n,[base[0],baseY,base[2]])+g.offset);
  const denom=wall.geometry.insideSign*(n[0]*normal2[0]+n[2]*normal2[1]+n[1]*(-(upN[0]*normal2[0]+upN[2]*normal2[1])/upN[1]));
  const alpha=(wall.geometry.insideSign*distance-baseWall)/(denom||1),x=base[0]+normal2[0]*alpha,z=base[2]+normal2[1]*alpha,y=(height-boundary.floor.offset-upN[0]*x-upN[2]*z)/upN[1];
  return [x,y,z];
}
function mapDataPoints() {
  const pts=[];for(const s of cameraDoc?.stations??[])pts.push(s.planeCoordinates);
  for(const c of footprintDoc?.candidates??[])for(const p of c.polygon2d??[])pts.push(p);
  for(const shape of footprintDoc?.concaveAlphaSweep??[])for(const ring of shape.allLoops2d??[])for(const p of ring)pts.push(p);
  return pts.filter(p=>p&&p.length===2);
}
function mapCanvasPoint(p) {return [mapTransform.left+(p[0]-mapTransform.minU)*mapTransform.scale,mapTransform.top+mapTransform.height-(p[1]-mapTransform.minV)*mapTransform.scale];}
function drawPolygon(ctx,poly,style,{fill=null,dash=[]}={}) {
  if(!poly?.length)return;ctx.beginPath();poly.forEach((p,i)=>{const q=mapCanvasPoint(p);if(!i)ctx.moveTo(...q);else ctx.lineTo(...q);});ctx.closePath();ctx.strokeStyle=style;ctx.lineWidth=1.5;ctx.setLineDash(dash);ctx.stroke();ctx.setLineDash([]);if(fill){ctx.fillStyle=fill;ctx.fill();}
}
function drawBoundaryMap() {
  if(!boundary||!el.boundaryMap)return;
  const canvas=el.boundaryMap,ctx=canvas.getContext('2d'),dpr=window.devicePixelRatio||1,w=canvas.clientWidth||480,h=canvas.clientHeight||250;
  if(canvas.width!==Math.round(w*dpr)||canvas.height!==Math.round(h*dpr)){canvas.width=Math.round(w*dpr);canvas.height=Math.round(h*dpr);}
  ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,w,h);ctx.fillStyle='#101612';ctx.fillRect(0,0,w,h);
  const all=mapDataPoints();if(!all.length)return;const minU=Math.min(...all.map(p=>p[0]))-.45,maxU=Math.max(...all.map(p=>p[0]))+.45,minV=Math.min(...all.map(p=>p[1]))-.45,maxV=Math.max(...all.map(p=>p[1]))+.45,pad=18,sc=Math.min((w-2*pad)/(maxU-minU),(h-2*pad)/(maxV-minV));
  mapTransform={minU,minV,scale:sc,left:(w-(maxU-minU)*sc)/2,height:(maxV-minV)*sc,top:(h-(maxV-minV)*sc)/2};
  const selected=boundary.horizontalBoundary.vertices2d;
  if(el.showCandidates.checked)for(const c of footprintDoc.candidates){if(c.algorithm==='CONVEX_HULL'&&c.id!==boundary.horizontalBoundary.candidateId)drawPolygon(ctx,c.polygon2d,'rgba(185,199,198,.45)',{dash:[5,4]});else if(c.algorithm==='OBB')drawPolygon(ctx,c.polygon2d,'rgba(125,159,221,.42)',{dash:[3,5]});else if(c.algorithm==='AABB')drawPolygon(ctx,c.polygon2d,'rgba(124,195,221,.42)',{dash:[3,5]});}
  if(el.showCandidates.checked)for(const shape of footprintDoc?.concaveAlphaSweep??[])for(const ring of shape.allLoops2d??[])drawPolygon(ctx,ring,'rgba(205,133,230,.35)',{dash:[2,5]});
  drawPolygon(ctx,selected,'#ffd276',{fill:'rgba(238,183,94,.14)'});
  if(el.showWalls.checked)for(const wall of boundary.referenceWallCandidates??[]){const a=wallFloorPoint(wall,wall.geometry.tangentMin),b=wallFloorPoint(wall,wall.geometry.tangentMax);const pa=mapCanvasPoint(horizontalCoordinates(a)),pb=mapCanvasPoint(horizontalCoordinates(b));ctx.beginPath();ctx.moveTo(...pa);ctx.lineTo(...pb);ctx.strokeStyle=wall.enabled?'#f27866':'#8a805b';ctx.lineWidth=2;ctx.setLineDash(wall.enabled?[6,4]:[2,4]);ctx.stroke();ctx.setLineDash([]);}
  if(el.showNeighborGraph.checked)for(const e of distributionDoc?.threeNearestNeighborEdges??[]){const a=cameraDoc.stations.find(s=>s.stationId===e.from),b=cameraDoc.stations.find(s=>s.stationId===e.to);if(!a||!b)continue;const pa=mapCanvasPoint(a.planeCoordinates),pb=mapCanvasPoint(b.planeCoordinates);ctx.beginPath();ctx.moveTo(...pa);ctx.lineTo(...pb);ctx.strokeStyle='rgba(139,163,148,.15)';ctx.lineWidth=.5;ctx.stroke();}
  const principal=new Set(cameraDoc?.principalImageKeys??[]);
  if(el.showCameras.checked)for(const s of cameraDoc?.stations??[]){const p=mapCanvasPoint(s.planeCoordinates),local=(cameraDoc.stations??[]).filter(o=>Math.hypot(o.planeCoordinates[0]-s.planeCoordinates[0],o.planeCoordinates[1]-s.planeCoordinates[1])<=.45).length;ctx.beginPath();ctx.arc(p[0],p[1],Math.min(4.4,1.3+local*.2),0,2*Math.PI);ctx.fillStyle='#e6c277';ctx.fill();if((s.imageKeys??[]).some(k=>principal.has(k))){ctx.beginPath();ctx.arc(p[0],p[1],5.3,0,2*Math.PI);ctx.strokeStyle='#fff';ctx.lineWidth=1;ctx.stroke();}}
  const cam=getPose()?.position;if(cam){const p=mapCanvasPoint(horizontalCoordinates(cam));ctx.beginPath();ctx.arc(...p,4.3,0,Math.PI*2);ctx.fillStyle='#fff';ctx.fill();ctx.strokeStyle='#111';ctx.lineWidth=1;ctx.stroke();}
  for(let i=0;i<selected.length;i++){const p=mapCanvasPoint(selected[i]);ctx.beginPath();ctx.arc(...p,3.1,0,2*Math.PI);ctx.fillStyle='#ffd276';ctx.fill();ctx.strokeStyle='#1a1f1b';ctx.lineWidth=1;ctx.stroke();}
  ctx.strokeStyle='#45564a';ctx.lineWidth=1;ctx.strokeRect(.5,.5,w-1,h-1);
}
function populateControls() {
  const stations=stationsDoc.stations;
  for(const station of stations){const option=document.createElement('option');option.value=station.stationId;option.textContent=`Trusted camera station · ${station.stationId}`;el.station.append(option);}
  const defaultStation=stations.find(s=>s.stationId===stationsDoc.defaultStationId)??stations.find(s=>s.normalVisitorPositionCandidate)??stations[0];if(defaultStation)el.station.value=defaultStation.stationId;
  for(const route of tourDoc.routes){const option=document.createElement('option');option.value=route.id;option.textContent=`${route.title} · ${route.waypoints.length} poses`;el.tour.append(option);}
  for(const c of candidateDoc.candidates){const option=document.createElement('option');option.value=c.candidateId;option.textContent=c.publicLabel??`Geometry viewpoint · ${c.candidateId}`;el.artwork.append(option);}
  if(candidateDoc.defaultCandidateId)el.artwork.value=candidateDoc.defaultCandidateId;
}
function boundarySummaryText() {
  const h=boundary.horizontalBoundary;
  return `Accepted boundary: ${h.algorithm} · ${h.classification} · ${h.closed?'closed':'OPEN'} · ${h.vertexCount??h.vertices2d.length} vertices\nMargin: ${h.marginUnits.toFixed(3)} units (${h.marginBasis??'accepted configuration'}) · camera clearance: ${h.cameraClearanceUnits.toFixed(3)} units\nTrusted stations inside accepted hull: ${h.cameraStationInclusion?.insideOrOn??'not recorded'} / ${h.cameraStationInclusion?.total??cameraDoc.stations.length}\nFloor: ${boundary.floor.clearance.toFixed(2)} unit clearance · ${boundary.floor.classification}\nCeiling: ${boundary.ceiling.heightAlongFloorNormal.toFixed(2)} units · ${boundary.ceiling.clearance.toFixed(2)} unit clearance\nFurniture and artwork do not collide. Only the accepted enclosure, floor, and ceiling constrain camera position.`;
}
function downloadJson(name,value) {const blob=new Blob([JSON.stringify(value,null,2)+'\n'],{type:'application/json'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=name;a.click();URL.revokeObjectURL(url);}
function resizeSelectionOverlay() {
 const canvas=el.selectionOverlay,host=el.viewerHost,rect=host.getBoundingClientRect(),dpr=window.devicePixelRatio||1;
 canvas.style.width=`${rect.width}px`;canvas.style.height=`${rect.height}px`;canvas.width=Math.max(1,Math.round(rect.width*dpr));canvas.height=Math.max(1,Math.round(rect.height*dpr));drawSelectionOverlay();
}
function drawSelectionOverlay() {
 const canvas=el.selectionOverlay;if(!canvas)return;const ctx=canvas.getContext('2d'),dpr=window.devicePixelRatio||1,w=canvas.clientWidth||el.viewerHost.clientWidth,h=canvas.clientHeight||el.viewerHost.clientHeight;ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,w,h);
 if(annotationDraft.length){ctx.beginPath();annotationDraft.forEach((p,i)=>{const q=[p[0]*w,p[1]*h];if(!i)ctx.moveTo(...q);else ctx.lineTo(...q);});if(!annotationDrawing)ctx.closePath();ctx.strokeStyle='#61efaa';ctx.lineWidth=2;ctx.fillStyle='rgba(56,196,137,.16)';if(!annotationDrawing)ctx.fill();ctx.stroke();}
}
function startGhostAnnotation() {
 if(!runtimeAvailable())return;annotationActive=true;annotationDrawing=false;annotationDraft=[];annotationPose=JSON.parse(JSON.stringify(getPose()));el.selectionOverlay.classList.add('active');el.annotationToggle.textContent='Finish annotation mode';el.annotationStatus.textContent='Draw a lasso around the visible ghost region. Camera pose is frozen into the selection record.';resizeSelectionOverlay();
}
function stopGhostAnnotation() {annotationActive=false;annotationDrawing=false;el.selectionOverlay.classList.remove('active');el.annotationToggle.textContent='Mark ghost region';}
async function refreshGhostAnnotations() {
 annotationsDoc=await fetch('../api/ghost-annotations.json').then(r=>r.json());annotationSaved=annotationsDoc.annotations??[];el.annotationList.replaceChildren();
 for(const a of annotationSaved){const row=document.createElement('div');row.className='annotation-row';row.textContent=`${a.annotationId} · ${a.viewLabel??a.viewPreset??'view'} · ${a.selectionPolygonNormalized.length} points · ${a.cameraPose.fov}°`;el.annotationList.append(row);}
 el.annotationStatus.textContent=annotationSaved.length?`${annotationSaved.length} saved side-view selection(s). Multi-view Gaussian localization is available through the local script.`:'No reviewer selections saved yet. Candidate Gaussian matching awaits at least one marked view.';
}
async function persistGhostAnnotation() { throw new Error('Annotations are not available in the static visitor package.'); }
function installGhostAnnotation() {
 const c=el.selectionOverlay;
 c.addEventListener('pointerdown',event=>{if(!annotationActive)return;const r=c.getBoundingClientRect();annotationDrawing=true;annotationDraft=[[clamp((event.clientX-r.left)/r.width,0,1),clamp((event.clientY-r.top)/r.height,0,1)]];annotationPose=JSON.parse(JSON.stringify(getPose()));drawSelectionOverlay();event.preventDefault();});
 c.addEventListener('pointermove',event=>{if(!annotationActive||!annotationDrawing)return;const r=c.getBoundingClientRect(),p=[clamp((event.clientX-r.left)/r.width,0,1),clamp((event.clientY-r.top)/r.height,0,1)],last=annotationDraft.at(-1);if(!last||Math.hypot((p[0]-last[0])*r.width,(p[1]-last[1])*r.height)>2)annotationDraft.push(p);drawSelectionOverlay();event.preventDefault();});
 const finish=()=>{if(annotationDrawing){annotationDrawing=false;drawSelectionOverlay();el.annotationStatus.textContent=`Selection drawn with ${annotationDraft.length} points. Save it, then capture another oblique view.`;}};c.addEventListener('pointerup',finish);c.addEventListener('pointercancel',finish);
}
function sourcePhotoFor(scene) { el.sourcePhoto.textContent='Original photographs are retained in the restricted archive source set; this public viewer does not expose them.'; }
async function setDiagnosticPreset(preset) {
  const scene=el.diagnosticScene.value,{pose,spec,angleDegrees,distance,projectedDimensions:proj}=presetPose(scene,preset);annotationPreset=preset;
  const ok=applyPose(pose);
  if(!ok) {logDecision(`Diagnostic pose ${scene}/${preset} is outside an active boundary; no boundary check was bypassed.`,false);return false;}
  const meta=canvasMetadata();
  el.diagnosticPose.textContent=`Scene: ${spec.id}\nPreset: ${preset} · viewing angle ${angleDegrees.toFixed(1)}° from front\nCamera distance: ${distance.toFixed(3)} common units\nPosition: [${pose.position.map(x=>x.toFixed(4)).join(', ')}]\nTarget: [${pose.target.map(x=>x.toFixed(4)).join(', ')}]\nProjected artwork estimate: ${proj.widthPx} × ${proj.heightPx} drawing-buffer pixels\nRenderer: ${meta.renderer} · canvas ${meta.drawingBuffer.join(' × ')} · DPR ${meta.devicePixelRatio}\nSource reference: ${spec.source} · qualitative comparison, not aligned metrics.`;
  sourcePhotoFor(scene);updatePoseHud();
  return true;
}
function startArtworkOrbit() {
  const selected=el.artwork.value;
  const spec=sceneSpec(selected);
  const pose=getPose();
  let direction=spec.front;
  let position=vAdd(spec.center,vScale(direction,2.8));
  if(!validPosition(position)) position=pose.position;
  const orbitPose={position,target:spec.center,fov:pose.fov};
  const ok=applyPose(orbitPose);
  if(!ok){logDecision('Artwork Orbit rejected: this anchor would cross an architectural boundary.',false);return false;}
  orbitCenter=[...spec.center];setMode('artwork-orbit');
  // setMode captures the current pose and retains the same policy; restore the requested focus after it is active.
  if(!applyPose(orbitPose)){orbitCenter=null;logDecision('Artwork Orbit anchor failed shared boundary validation.',false);return false;}
  logDecision(`Artwork Orbit focused on ${spec.id}; wall/floor/ceiling validation remains active.`,true);updatePoseHud();return true;
}
function saveBookmark() {
  const pose=getPose();if(!pose)return;
  if(!validPosition(pose.position)){logDecision('Bookmark not saved: current position is outside the active architecture boundary.',false);return;}
  localStorage.setItem(bookmarkKey,JSON.stringify({pose,savedAt:new Date().toISOString(),modelSha256:health.sha256}));
  logDecision('Bookmark saved with the current baseline identity.',true);
}
function restoreBookmark() {
  let record;try{record=JSON.parse(localStorage.getItem(bookmarkKey)||'null')}catch{}
  if(!record?.pose){logDecision('No local bookmark is saved.',false);return;}
  if(record.modelSha256!==health.sha256){logDecision('Bookmark belongs to a different PLY identity and was rejected.',false);return;}
  const ok=applyPose(record.pose);
  logDecision(ok?'Bookmark restored through the shared architectural policy.':'Bookmark restore rejected because its segment crosses a wall or exits the floor/ceiling envelope.',ok);updatePoseHud();
}
function routePoseAt(route,fromIndex,toIndex,t) {
  const a=route.waypoints[fromIndex],b=route.waypoints[toIndex],mix=(x,y)=>x+(y-x)*t;
  return {position:a.position.map((x,i)=>mix(x,b.position[i])),target:a.target.map((x,i)=>mix(x,b.target[i])),fov:mix(a.fov??70.75,b.fov??70.75)};
}
async function playTour() {
  const route=tourDoc.routes.find(x=>x.id===el.tour.value);if(!route||route.waypoints.length<2)return;
  setMode('guided-tour');
  const initial=route.waypoints[0];
  if(!applyPose(initial)){logDecision('Guided route start rejected by the architectural boundary policy.',false);return;}
  let segment=1,start=performance.now(),startPose=getPose();
  const tick=(now,scheduleRaf=true)=>{
    if(activeMode!=='guided-tour')return;
    const next=route.waypoints[segment],duration=clamp(length(vSub(next.position,startPose.position))/1.25*1000,450,3500),t=clamp((now-start)/duration,0,1),pose=routePoseAt(route,segment-1,segment,t);
    if(!applyPose(pose)){clearTour();logDecision(`Guided tour stopped at waypoint ${segment+1}: the route encountered the current architectural boundary configuration.`,false);updatePoseHud();return;}
    if(t>=1){segment++;start=now;startPose=getPose();if(segment>=route.waypoints.length){clearTour();logDecision(`Completed ${route.title}; all interpolated positions passed the shared policy.`,true);updatePoseHud();return;}}
    if(scheduleRaf)tourRaf=requestAnimationFrame(tick);
  };
  routeAdvanceForAcceptance=now=>tick(now,false);
  logDecision(`Playing ${route.title} through the shared camera-derived enclosure/floor/ceiling policy.`,true);tourRaf=requestAnimationFrame(tick);
}
const acceptanceTasks=[
  ['free','Move freely around the whole exhibition.'],['table','Fly above and pass through the central table.'],['low','Descend to inspect low artworks, then ascend again.'],['rotation','Rotate freely with mouse look from several viewing angles.'],['walls','Test an active wall, floor, ceiling, and corner for plausible stops.'],['controls','After WASD, drag the scene and click the controls normally.'],['modes','Switch Visitor Explore, Guided Tour, and Artwork Orbit.'],['smooth','Assess whether movement feels smooth and intuitive.']
];
function renderAcceptance() {
  const saved=JSON.parse(localStorage.getItem(acceptanceKey)||'{}');el.acceptanceChecklist.replaceChildren();
  for(const [id,label] of acceptanceTasks){
    const row=document.createElement('div');row.className='acceptance-row';const title=document.createElement('strong');title.textContent=label;
    const select=document.createElement('select');select.id=`accept-${id}`;for(const status of ['NOT TESTED','PASS','PARTIAL','FAIL']){const o=document.createElement('option');o.value=status;o.textContent=status;select.append(o);}select.value=saved[id]?.status??'NOT TESTED';
    const notes=document.createElement('textarea');notes.rows=2;notes.placeholder='Human notes (optional)';notes.value=saved[id]?.notes??'';
    const persist=()=>{const value={};for(const [task] of acceptanceTasks)value[task]={status:$( `accept-${task}`)?.value??'NOT TESTED',notes:document.querySelector(`#acceptanceChecklist .acceptance-row:nth-child(${acceptanceTasks.findIndex(x=>x[0]===task)+1}) textarea`)?.value??''};localStorage.setItem(acceptanceKey,JSON.stringify(value));};
    select.addEventListener('change',persist);notes.addEventListener('input',persist);row.append(title,select,notes);el.acceptanceChecklist.append(row);
  }
}
function installInputHandlers() {
  window.addEventListener('pointerdown',event=>{
    const target=event.target;
    if(!(target instanceof Element)||!el.viewerHost.contains(target)||!target.closest('canvas'))return;
    if(annotationActive&&target.id==='selectionOverlay')return;
    event.preventDefault();event.stopImmediatePropagation();
    navArmed=true;el.viewerHost.focus({preventScroll:true});pointerDragging=event.button===0;lastPointer=[event.clientX,event.clientY];
  },true);
  window.addEventListener('pointermove',event=>{
    if(!pointerDragging)return;
    event.preventDefault();event.stopImmediatePropagation();
    if(lastPointer){const dx=event.clientX-lastPointer[0],dy=event.clientY-lastPointer[1];if(Math.abs(dx)+Math.abs(dy)>0)lookByPointer(dx,dy);}
    lastPointer=[event.clientX,event.clientY];
  },true);
  const releasePointer=event=>{if(!pointerDragging)return;event.preventDefault();event.stopImmediatePropagation();pointerDragging=false;lastPointer=null;};
  window.addEventListener('pointerup',releasePointer,true);window.addEventListener('pointercancel',releasePointer,true);window.addEventListener('blur',()=>{pointerDragging=false;lastPointer=null;keyState.clear();},true);
  window.addEventListener('keydown',event=>{
    const key=event.key.toLowerCase();
    if(!['w','a','s','d','e','q',' ','control','shift','escape'].includes(key))return;
    const target=event.target;
    if(target instanceof Element&&target.closest('input,select,textarea,button'))return;
    if(!navArmed&&document.activeElement!==el.viewerHost)return;
    if(key==='escape'){pointerDragging=false;keyState.clear();return;}
    event.preventDefault();event.stopImmediatePropagation();keyState.add(key);
  },true);
  window.addEventListener('keyup',event=>{const key=event.key.toLowerCase();if(keyState.has(key)){event.preventDefault();event.stopImmediatePropagation();keyState.delete(key);}},true);
  const loop=now=>{const dt=lastFrameAt?Math.min(.05,(now-lastFrameAt)/1000):0;lastFrameAt=now;moveByInput(dt);frameId=requestAnimationFrame(loop);};frameId=requestAnimationFrame(loop);
}
async function startViewer() {
  const initial=spawnPose();
  const settings={version:2,tonemapping:'none',highPrecisionRendering:false,background:{color:[.012,.016,.013]},animTracks:[],cameras:[{initial:{position:initial.position,target:initial.target,fov:initial.fov}}],annotations:[],startMode:'orbit'};
  const response=await fetch(health.contentUrl,{cache:'no-store'});if(!response.ok)throw new Error(`Selected model request failed (${response.status})`);
  const transform=await fetch('../api/transform.json').then(r=>{if(!r.ok)throw new Error(`Transform audit request failed (${r.status})`);return r.json();});
  const auditedNode=transform?.viewerSceneNode;
  const sceneTransform={
    translation:auditedNode?.translation,
    rotationQuaternionXYZW:auditedNode?.rotationQuaternionXYZW??auditedNode?.quaternionXYZW,
    uniformScale:auditedNode?.uniformScale
  };
  if(!Array.isArray(sceneTransform.translation)||sceneTransform.translation.length!==3||!Array.isArray(sceneTransform.rotationQuaternionXYZW)||sceneTransform.rotationQuaternionXYZW.length!==4||!Number.isFinite(sceneTransform.uniformScale))throw new Error('Selected model transform metadata is incomplete; refusing to apply an implicit identity transform.');
  handle=await createViewer({container:el.viewerHost,contentUrl:health.contentUrl,contentFilename:health.modelFilename??'exhibition.ply',contents:Promise.resolve(response),settings,sceneTransform,renderer:'webgl',ui:false,aa:false,nofx:true,exposeGlobals:false});
  await new Promise((resolve,reject)=>{let done=false;const ready=()=>{if(done||!runtimeAvailable())return;done=true;clearTimeout(timeout);resolve();};const timeout=setTimeout(()=>{if(done)return;done=true;reject(new Error('Viewer did not render its first usable frame within 150 seconds.'));},150000);handle.events.on('firstFrame',ready);handle.events.on('loaded:changed',ready);handle.events.on('progress:changed',()=>{if(!handle.state.loaded)el.modelStatus.textContent=`Loading selected Gaussian PLY · ${Math.round(handle.state.progress)}%`;ready();});if(runtimeAvailable())ready();});
  if(health.sha256!==health.expectedSha256)throw new Error('The selected model identity is invalid; refusing to load it.');
  handle.setCollisionEnabled(false);handle.setMoveSpeed(Number(el.speed.value));
  if(!handle.setCameraPose(initial))throw new Error('Safe initial station failed architectural validation.');
  handle.setNavigationPolicy('architectural-envelope',initial,policyValidator);
  activeMode='visitor-explore';el.mode.value=activeMode;updateModeText();
  el.modelStatus.textContent=`Ready · ${health.modelBytes.toLocaleString()} bytes · selected archive model identity verified`;
  el.graphics.textContent=`${canvasMetadata().renderer} · WebGL · AA off · postprocessing off · DPR ${renderDpr()} · shared architecture policy active`;
  el.viewerHost.querySelector('.loading')?.remove();resizeSelectionOverlay();
  logDecision('Selected archive model loaded. Visitor Explore is free in XYZ inside the camera-derived enclosure, floor, and ceiling.',true);updatePoseHud();
  handle.app.on('frameend',updatePoseHud);
}
async function init() {
  [health,boundary,generatedBoundary,stationsDoc,initialCameraDoc,tourDoc,candidateDoc,artifactEvidence,cameraDoc,footprintDoc,distributionDoc,annotationsDoc]=await Promise.all([
    fetch('../api/health.json',{cache:'no-store'}).then(r=>r.json()),fetch('../api/boundary.json',{cache:'no-store'}).then(r=>r.json()),fetch('../api/boundary-generated.json',{cache:'no-store'}).then(r=>r.json()),fetch('../api/stations.json').then(r=>r.json()),fetch('../api/initial-camera.json').then(r=>r.json()),fetch('../api/tours.json').then(r=>r.json()),fetch('../api/candidates.json').then(r=>r.json()),fetch('../api/artifact-evidence.json').then(r=>r.json()),fetch('../api/cameras.json').then(r=>r.json()),fetch('../api/footprints.json').then(r=>r.json()),fetch('../api/distribution.json').then(r=>r.json()),fetch('../api/ghost-annotations.json').then(r=>r.json())
  ]);
  if(!health.ok)throw new Error(`Model identity check failed: ${health.sha256} (${health.modelBytes} bytes).`);
  el.serverStatus.textContent=`Static archive · ${health.modelBytes.toLocaleString()} model bytes`;
  el.modelHash.textContent=`SHA-256 ${health.sha256}`;
  el.modelStatus.textContent='Selected model identity verified · loading Gaussian renderer…';
  populateControls();
  el.boundarySummary.textContent=boundarySummaryText();refreshGhostAnnotations();installGhostAnnotation();
  renderAcceptance();
  const sourceHash=await fetch('../api/health.json').then(r=>r.json());
  if(sourceHash.sha256!==health.expectedSha256)throw new Error('Selected model identity changed during initialization.');
  await startViewer();
  sourcePhotoFor(el.diagnosticScene.value);installInputHandlers();
  drawBoundaryMap();
}

el.mode.addEventListener('change',()=>{try{setMode(el.mode.value);}catch(error){logDecision(`Mode switch failed: ${error.message}`);}});
el.speed.addEventListener('input',()=>{el.speedLabel.value=Number(el.speed.value).toFixed(1);handle?.setMoveSpeed(Number(el.speed.value));});
el.goStation.addEventListener('click',goToStation);
el.station.addEventListener('change',()=>{});
el.saveBookmark.addEventListener('click',saveBookmark);el.restoreBookmark.addEventListener('click',restoreBookmark);
el.returnExplore.addEventListener('click',()=>{orbitCenter=null;setMode('visitor-explore');logDecision('Returned to Visitor Explore at a valid interior position.',true);updatePoseHud();});
el.playTour.addEventListener('click',()=>void playTour());el.orbitArtwork.addEventListener('click',startArtworkOrbit);
for(const toggle of [el.showCameras,el.showWalls,el.showCandidates,el.showNeighborGraph])toggle.addEventListener('change',drawBoundaryMap);
el.exportBoundary.addEventListener('click',()=>downloadJson('exhibition-archive-navigation.json',boundary));
document.querySelectorAll('[data-preset]').forEach(button=>button.addEventListener('click',()=>void setDiagnosticPreset(button.dataset.preset)));
el.diagnosticScene.addEventListener('change',()=>{sourcePhotoFor(el.diagnosticScene.value);el.diagnosticPose.textContent='Choose a view angle to place the diagnostic camera.';});
el.captureDiagnostic.addEventListener('click',()=>{const pose=getPose();downloadJson('exhibition-archive-diagnostic-camera.json',{schemaVersion:1,status:'CAMERA_METADATA_ONLY',modelSha256:health.sha256,scene:el.diagnosticScene.value,pose,renderer:canvasMetadata(),activeMode,activePolicy:activePolicy(),capturedAt:new Date().toISOString()});});
el.exportCamera.addEventListener('click',()=>downloadJson('exhibition-archive-camera-render-metadata.json',{schemaVersion:1,modelPath:health.modelPath,modelBytes:health.modelBytes,modelSha256:health.sha256,pose:getPose(),renderer:canvasMetadata(),activeMode,activePolicy:activePolicy(),boundary:boundary.id,exportedAt:new Date().toISOString()}));
el.annotationToggle.addEventListener('click',()=>annotationActive?stopGhostAnnotation():startGhostAnnotation());
el.annotationClear.addEventListener('click',()=>{annotationDraft=[];drawSelectionOverlay();el.annotationStatus.textContent='Current unsaved screen selection cleared.';});
el.annotationSave.addEventListener('click',()=>void persistGhostAnnotation().then(()=>{el.annotationStatus.textContent='Selection saved with exact camera pose and normalized screen polygon.';}).catch(e=>{el.annotationStatus.textContent=e.message;}));
el.annotationExport.addEventListener('click',()=>downloadJson('exhibition-archive-geometry-view-side-view-annotations.json',annotationsDoc??{schemaVersion:1,modelSha256:health.sha256,annotations:annotationSaved}));
el.exportAcceptance.addEventListener('click',()=>downloadJson('exhibition-archive-human-acceptance.json',{schemaVersion:1,status:'HUMAN_REVIEW_PENDING',checklist:JSON.parse(localStorage.getItem(acceptanceKey)||'{}'),exportedAt:new Date().toISOString()}));
el.resetAcceptance.addEventListener('click',()=>{localStorage.removeItem(acceptanceKey);renderAcceptance();});
window.addEventListener('resize',()=>{updatePoseHud();drawBoundaryMap();resizeSelectionOverlay();});
window.__exhibitionArchiveTestHooks={
  getPose:()=>{const pose=getPose();return pose?JSON.parse(JSON.stringify(pose)):null;},
  getMode:()=>activeMode, getPolicy:activePolicy, getBoundary:()=>JSON.parse(JSON.stringify(boundary)),
  isValidPosition:validPosition, isSegmentAllowed:segmentAllowed, resolveMove,
  getLastBlock:()=>lastBlock?JSON.parse(JSON.stringify(lastBlock)):null, horizontalCoordinates,
  getPresetPose:(scene,preset)=>JSON.parse(JSON.stringify(presetPose(scene,preset))),
  wallApproachPose:(...args)=>JSON.parse(JSON.stringify(wallApproachPose(...args))), canvasMetadata,
  getActiveMode:()=>activeMode, getNavArmed:()=>navArmed,
  getBoundaryVertices:()=>boundary.horizontalBoundary.vertices2d.map(point=>[...point]),
  getMapVertexScreen:index=>{const p=mapCanvasPoint(boundary.horizontalBoundary.vertices2d[index]),r=el.boundaryMap.getBoundingClientRect();return [r.left+p[0],r.top+p[1]];},
  getKeyState:()=>[...keyState], advanceInputForAcceptance:dt=>moveByInput(Math.min(0.05,Math.max(0,Number(dt)||0))), advanceRouteForAcceptance:now=>routeAdvanceForAcceptance?.(Number(now)), getSourceImageCount:()=>health.indexedSourceImages,
  getGhostAnnotationCount:()=>annotationSaved.length
};
await init().catch(error=>{console.error(error);el.serverStatus.textContent='Viewer unavailable';el.modelStatus.textContent=`This exhibition could not be loaded: ${error.message}`;logDecision(error.message,false);});
