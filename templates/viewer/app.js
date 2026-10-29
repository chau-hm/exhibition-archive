import { createViewer } from '/vendor/index.js';

const $ = id => document.getElementById(id);
const ids = ['serverStatus','modelStatus','modelHash','viewerHost','selectionOverlay','graphics','cameraPosition','decision','mode','modeNote','speed','speedLabel','station','goStation','saveBookmark','restoreBookmark','returnExplore','tour','playTour','artwork','orbitArtwork','boundarySummary','boundaryMap','candidateSelect','cameraClearance','showCameras','showWalls','showCandidates','showNeighborGraph','vertexSummary','resetVertices','saveBoundary','revertBoundary','exportBoundary','boundarySaveStatus','diagnosticScene','diagnosticPose','captureDiagnostic','sourcePhoto','annotationToggle','annotationClear','annotationSave','annotationExport','annotationLabel','annotationStatus','annotationList','acceptanceChecklist','exportAcceptance','resetAcceptance','runtimeSummary','exportCamera'];
const el = Object.fromEntries(ids.map(id => [id, $(id)]));
let health, boundary, generatedBoundary, stationsDoc, tourDoc, candidateDoc, artifactEvidence, cameraDoc, footprintDoc, distributionDoc, annotationsDoc, handle;
let activeMode = 'visitor-explore';
let navArmed = false, pointerDragging = false, lastPointer = null;
let frameId = 0, lastFrameAt = 0, tourRaf = 0;
let keyState = new Set();
let orbitCenter = null;
let mapTransform=null, draggedVertex=-1, annotationActive=false, annotationDrawing=false, annotationDraft=[], annotationPose=null, annotationPreset='front', annotationSaved=[];
let lastBlock=null;
const clearanceDefault = 0.08;
const bookmarkKey = 'phase2er-free-gallery-bookmark-v1';
const acceptanceKey = 'phase2er-human-acceptance-v1';
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
  if(length(vSub(target,desired))>1e-8){const requestedHeight=floorDistance(desired),reason=requestedHeight<boundary.floor.clearance?'floor':'ceiling';lastBlock={reason,attempted:[...desired],accepted:[...target],edgeIndex:null,boundarySource:`Phase2EQ ${reason} constraint`};}
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
function activePolicy() { return runtimeAvailable()?(activeMode==='debug'?'debug':'architectural-envelope'):'unavailable'; }
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
function applyPose(pose,{debug=activeMode==='debug'}={}) {
  if (!runtimeAvailable()) return false;
  if (!debug&&!segmentAllowed(getPose().position,pose.position)) return false;
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
  el.runtimeSummary.textContent=`Mode: ${activeMode}\nShared policy: ${activePolicy()}\nPosition: [${p.map(x=>x.toFixed(4)).join(', ')}]\nSigned floor distance: ${floorDistance(p).toFixed(4)} u\nCeiling distance: ${(boundary.ceiling.heightAlongFloorNormal-floorDistance(p)).toFixed(4)} u\nRenderer: WebGL · Phase 2EQ rendering settings unchanged\nHorizontal boundary: ${boundary.horizontalBoundary.algorithm} · ${boundary.horizontalBoundary.classification} · closed polygon · clearance ${boundary.horizontalBoundary.cameraClearanceUnits.toFixed(2)} u\n${boundaryEventText()}\nCanvas CSS: ${canvasMetadata().css.join(' × ')} · drawing buffer: ${canvasMetadata().drawingBuffer.join(' × ')} · DPR ${renderDpr()}\nLegacy finite wall collisions: 0 · furniture collision: none · camera-station/raster limits: none`;
  drawBoundaryMap();
}
function clearTour() { if(tourRaf) cancelAnimationFrame(tourRaf); tourRaf=0; }
function setMode(mode) {
  if(!runtimeAvailable()) return;
  clearTour();
  const current=getPose();
  if(mode==='debug') {
    handle.setNavigationPolicy('debug',current,null);
    activeMode='debug';
    document.body.classList.add('debug-active');
    logDecision('Debug Fly is active. This is the only mode that can leave the camera-derived enclosure.',false);
  } else {
    if(!validPosition(current.position)) {
      const spawn=spawnPose();
      handle.setNavigationPolicy('debug',current,null);
      if(!handle.setCameraPose(spawn)) throw new Error('Could not restore the safe Visitor Explore spawn.');
    }
    handle.setNavigationPolicy('architectural-envelope',getPose(),policyValidator);
    activeMode=mode;
    document.body.classList.remove('debug-active');
    if(mode==='visitor-explore') logDecision('Visitor Explore: free XYZ movement inside the closed camera-derived boundary, floor, and ceiling. Internal exhibits do not collide.',true);
    else if(mode==='guided-tour') logDecision('Guided Tour is constrained by the same finite architectural boundary policy.',true);
    else logDecision('Artwork Orbit is constrained by the same finite architectural boundary policy.',true);
  }
  el.mode.value=mode;
  updateModeText();
  updatePoseHud();
}
function updateModeText() {
  const notes={
    'visitor-explore':'W/A/S/D move freely parallel to the floor; E/Space ascend; Q/Ctrl descend; drag to look. Only the closed camera-derived enclosure, floor, and ceiling constrain position. Furniture and artwork have no collision.',
    'guided-tour':'A saved route plays through the shared camera-derived boundary validator. Click the viewer and use mouse-look to interrupt.',
    'artwork-orbit':'Mouse drag orbits the selected focus; every camera position remains subject to the shared camera-derived enclosure/floor/ceiling policy.',
    'debug':'Unrestricted developer-only flight. The red state is intentional; returning to Visitor Explore restores a valid interior pose.'
  };
  el.modeNote.textContent=notes[activeMode];
}
function stationPose(station) {
  const p=station.centerCommonViewer, f=station.representativeForwardCommonViewer;
  return poseFacing(p,f,getPose(),70.75);
}
function spawnPose() {
  const preferred=stationsDoc.stations.find(s=>s.imageKeys.includes('IMG_0542'))??stationsDoc.stations.find(s=>s.normalVisitorPositionCandidate)??stationsDoc.stations[0];
  return stationPose(preferred);
}
function goToStation() {
  const station=stationsDoc.stations.find(x=>x.stationId===el.station.value);
  if(!station||!runtimeAvailable()) return;
  setMode(activeMode==='debug'?'debug':'visitor-explore');
  const ok=applyPose(stationPose(station));
  logDecision(ok?`Reached ${station.stationId} · ${station.imageKeys[0]??'trusted camera'} through the shared policy.`:`${station.stationId} was rejected: its direct segment crosses an active boundary or leaves the floor/ceiling envelope.`,ok);
  updatePoseHud();
}
function getCandidate(id) { return candidateDoc.candidates.find(x=>x.candidateId===id); }
function sceneSpec(scene) {
  if(scene==='hanging-text') {
    const c=artifactEvidence.candidate;
    const station=stationsDoc.stations.find(s=>s.imageKeys.includes('IMG_0716'));
    const center=c.commonCenter, from=station?normalize(vSub(station.centerCommonViewer,center)):normalize(vScale(c.commonRotationRows[2],-1));
    return {id:'recovery-hanging-text-001',center,front:from,halfExtents:c.commonHalfExtents,axes:c.commonRotationRows,source:'IMG_0716'};
  }
  if(scene==='photo-wall') {
    const c=getCandidate('attention-008');
    return {id:c.candidateId,center:c.commonCenter,front:normalize(vScale(c.commonRotationRows[2],-1)),halfExtents:c.commonHalfExtents,axes:c.commonRotationRows,source:'IMG_0633'};
  }
  if(scene==='central-table') {
    const c=getCandidate('attention-018');
    return {id:c.candidateId,center:c.commonCenter,front:normalize(vScale(c.commonRotationRows[2],-1)),halfExtents:c.commonHalfExtents,axes:c.commonRotationRows,source:'IMG_0542'};
  }
  const c=getCandidate('attention-003');
  return {id:c.candidateId,center:c.commonCenter,front:normalize(vScale(c.commonRotationRows[2],-1)),halfExtents:c.commonHalfExtents,axes:c.commonRotationRows,source:'IMG_0643'};
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
  if(!runtimeAvailable()||!keyState.size||!['visitor-explore','debug','artwork-orbit'].includes(activeMode)) return;
  const pose=getPose(),axes=movementAxes(pose);
  const forward=(keyState.has('w')?1:0)-(keyState.has('s')?1:0);
  const strafe=(keyState.has('d')?1:0)-(keyState.has('a')?1:0);
  const vertical=(keyState.has('e')||keyState.has(' ')?1:0)-(keyState.has('q')||keyState.has('control')?1:0);
  let delta=vAdd(vAdd(vScale(axes.forward,forward),vScale(axes.right,strafe)),vScale(axes.up,vertical));
  if(length(delta)<1e-8) return;
  delta=normalize(delta);
  const speed=Number(el.speed.value)*(keyState.has('shift')?3:1);
  const desired=vAdd(pose.position,vScale(delta,speed*dt));
  const target=activeMode==='debug'?desired:resolveMove(pose.position,desired);
  if(length(vSub(target,pose.position))<1e-7) { updatePoseHud(); return; }
  let next;
  if(activeMode==='artwork-orbit'&&orbitCenter) {
    const offset=vSub(target,orbitCenter),forward=normalize(vSub(orbitCenter,target));
    next={position:target,target:vAdd(target,vScale(forward,length(offset))),fov:pose.fov};
  } else {
    next=poseFacing(target,cameraForward(pose),pose,pose.fov);
  }
  const ok=activeMode==='debug'?handle.setCameraPose(next):applyPose(next);
  if(!ok) logDecision('Camera movement was stopped by the shared architectural boundary policy.',false);
  updatePoseHud();
}
function lookByPointer(dx,dy) {
  if(!runtimeAvailable()||!['visitor-explore','debug','artwork-orbit'].includes(activeMode)) return;
  const pose=getPose(),sensitivity=0.0032;
  if(activeMode==='artwork-orbit'&&orbitCenter) {
    const offset=vSub(pose.position,orbitCenter),yawed=rotateAround(offset,up(),-dx*sensitivity);
    const right=normalize(cross(normalize(vSub(orbitCenter,pose.position)),up()));
    const pitched=rotateAround(yawed,right,dy*sensitivity);
    const desired={position:vAdd(orbitCenter,pitched),target:[...orbitCenter],fov:pose.fov};
    if(activeMode==='debug'||segmentAllowed(pose.position,desired.position)) applyPose(desired);
  } else {
    const {yaw,pitch,distance}=cameraAnglesToTarget(pose,pose.target);
    const next=withYawPitch(pose.position,yaw-dx*sensitivity,pitch-dy*sensitivity,distance,pose.fov);
    if(activeMode==='debug'||applyPose(next)) { if(activeMode==='debug') handle.setCameraPose(next); }
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
function mapCanvasCoord(x,y) {return [mapTransform.minU+(x-mapTransform.left)/mapTransform.scale,mapTransform.minV+(mapTransform.height-(y-mapTransform.top))/mapTransform.scale];}
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
  const principal=new Set(['IMG_0542','IMG_0503','IMG_0748','IMG_0540','IMG_0633','IMG_0578']);
  if(el.showCameras.checked)for(const s of cameraDoc?.stations??[]){const p=mapCanvasPoint(s.planeCoordinates),local=(cameraDoc.stations??[]).filter(o=>Math.hypot(o.planeCoordinates[0]-s.planeCoordinates[0],o.planeCoordinates[1]-s.planeCoordinates[1])<=.45).length;ctx.beginPath();ctx.arc(p[0],p[1],Math.min(4.4,1.3+local*.2),0,2*Math.PI);ctx.fillStyle='#e6c277';ctx.fill();if((s.imageKeys??[]).some(k=>principal.has(k))){ctx.beginPath();ctx.arc(p[0],p[1],5.3,0,2*Math.PI);ctx.strokeStyle='#fff';ctx.lineWidth=1;ctx.stroke();}}
  const cam=getPose()?.position;if(cam){const p=mapCanvasPoint(horizontalCoordinates(cam));ctx.beginPath();ctx.arc(...p,4.3,0,Math.PI*2);ctx.fillStyle='#fff';ctx.fill();ctx.strokeStyle='#111';ctx.lineWidth=1;ctx.stroke();}
  for(let i=0;i<selected.length;i++){const p=mapCanvasPoint(selected[i]);ctx.beginPath();ctx.arc(...p,draggedVertex===i?6:3.1,0,2*Math.PI);ctx.fillStyle=draggedVertex===i?'#71e3a0':'#ffd276';ctx.fill();ctx.strokeStyle='#1a1f1b';ctx.lineWidth=1;ctx.stroke();}
  ctx.strokeStyle='#45564a';ctx.lineWidth=1;ctx.strokeRect(.5,.5,w-1,h-1);
}
function populateControls() {
  const stations=stationsDoc.stations;
  for(const station of stations){const option=document.createElement('option');option.value=station.stationId;option.textContent=`${station.stationId} · ${station.imageKeys.slice(0,2).join(', ')}`;el.station.append(option);}
  const defaultStation=stations.find(s=>s.imageKeys.includes('IMG_0542'))??stations[0];if(defaultStation)el.station.value=defaultStation.stationId;
  for(const route of tourDoc.routes){const option=document.createElement('option');option.value=route.id;option.textContent=`${route.title} · ${route.waypoints.length} poses`;el.tour.append(option);}
  for(const id of ['attention-003','attention-008','attention-018']){const c=getCandidate(id);if(!c)continue;const option=document.createElement('option');option.value=id;option.textContent=`${id} · ${id==='attention-003'?'fine-line drawing':id==='attention-018'?'central table':'photographic wall'}`;el.artwork.append(option);}
  const recovered=document.createElement('option');recovered.value='hanging-text';recovered.textContent='recovery-hanging-text-001 · hanging text';el.artwork.append(recovered);el.artwork.value='attention-003';
  for(const c of footprintDoc.candidates){const option=document.createElement('option');option.value=c.id;option.textContent=`${c.algorithm.replaceAll('_',' ')} · margin ${c.marginUnits.toFixed(3)} · ${c.areaUnits2.toFixed(1)} u² · ${c.trustedStationInclusion.insideOrOn}/${c.trustedStationInclusion.total} cameras`;el.candidateSelect.append(option);}
  el.candidateSelect.value=boundary.horizontalBoundary.candidateId;
}
function boundarySummaryText() {
  const h=boundary.horizontalBoundary;
  return `Active boundary: ${h.algorithm} · ${h.classification} · ${h.closed?'closed':'OPEN'} · ${h.vertexCount??h.vertices2d.length} vertices\nMargin: ${h.marginUnits.toFixed(3)} units (${h.marginBasis??'manual vertices'}) · active camera clearance: ${h.cameraClearanceUnits.toFixed(3)} units\nTrusted camera stations inside selected automatic hull: ${h.cameraStationInclusion?.insideOrOn??'manual'} / ${h.cameraStationInclusion?.total??cameraDoc.stations.length}\nFloor: Phase 2EQ plane retained exactly · ${boundary.floor.clearance.toFixed(2)} unit clearance · ${boundary.floor.classification}\nCeiling: Phase 2EQ height ${boundary.ceiling.heightAlongFloorNormal.toFixed(2)} units retained exactly · ${boundary.ceiling.clearance.toFixed(2)} unit clearance\nOld Phase 2EQ wall candidates: reference overlay only; active horizontal wall collisions: 0\nNo table, furniture, installation, artwork, station buffer, old raster, or unknown voxel collision.`;
}
function syncEditorSummary() {el.vertexSummary.textContent=`${boundary.horizontalBoundary.vertices2d.length} plane vertices · drag gold handles on the map to adjust. Coordinates are floor-aligned common-frame units.`;el.boundarySummary.textContent=boundarySummaryText();drawBoundaryMap();}
function polygonValid(poly) {
 if(!Array.isArray(poly)||poly.length<3||poly.length>100||poly.some(p=>!Array.isArray(p)||p.length!==2||!p.every(Number.isFinite)))return false;
 const orient=(a,b,c)=>(b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]);
 for(let i=0;i<poly.length;i++)for(let j=i+1;j<poly.length;j++){if(j===i||j===(i+1)%poly.length||i===(j+1)%poly.length)continue;const a=poly[i],b=poly[(i+1)%poly.length],c=poly[j],d=poly[(j+1)%poly.length];if(orient(a,b,c)*orient(a,b,d)<-1e-12&&orient(c,d,a)*orient(c,d,b)<-1e-12)return false;}
 let area=0;for(let i=0;i<poly.length;i++)area+=poly[i][0]*poly[(i+1)%poly.length][1]-poly[(i+1)%poly.length][0]*poly[i][1];return Math.abs(area)>.02;
}
function candidateChanged() {
 const prior=JSON.parse(JSON.stringify(boundary.horizontalBoundary)),choice=footprintDoc.candidates.find(c=>c.id===el.candidateSelect.value);if(!choice)return;
 const h=boundary.horizontalBoundary;h.candidateId=choice.id;h.algorithm=choice.algorithm;h.classification='CAMERA_DERIVED';h.closed=true;h.marginUnits=choice.marginUnits;h.marginBasis=choice.marginUnits===0?'strict camera hull':`${(choice.marginUnits/comparisonMedian()).toFixed(1)} × observed median station spacing`;h.vertices2d=JSON.parse(JSON.stringify(choice.polygon2d));h.verticesCommonViewer=h.vertices2d.map(p=>planeToWorld(p));h.areaUnits2=choice.areaUnits2;h.vertexCount=h.vertices2d.length;h.cameraStationInclusion=choice.trustedStationInclusion;h.manualConfigPath=null;
 if(runtimeAvailable()&&!validPosition(getPose().position)){boundary.horizontalBoundary=prior;el.candidateSelect.value=prior.candidateId;logDecision(`Candidate ${choice.algorithm} rejected at the current camera pose. Move to a location it contains, then select again.`,false);return;}
 if(runtimeAvailable()&&activeMode!=='debug')handle.setNavigationPolicy('architectural-envelope',getPose(),policyValidator);
 syncEditorSummary();logDecision(`Active boundary changed to ${choice.algorithm}, ${choice.marginUnits.toFixed(3)} units margin.`,true);
}
function comparisonMedian(){return distributionDoc.medianNearestNeighborSpacingUnits;}
function planeToWorld(p){const b=boundary.horizontalBoundary.planeBasis,p0=b.pointOnPlaneCommonViewer;return p0.map((x,i)=>x+b.basisU[i]*p[0]+b.basisV[i]*p[1]);}
function refreshBoundary() {
 const prior=boundary.horizontalBoundary.cameraClearanceUnits,next=Number(el.cameraClearance.value);
 if(!Number.isFinite(next)||next<0||next>.5){el.cameraClearance.value=prior.toFixed(2);return;}
 boundary.horizontalBoundary.cameraClearanceUnits=next;boundary.navigation.horizontalClearanceUnits=next;
 if(runtimeAvailable()&&!validPosition(getPose().position)){boundary.horizontalBoundary.cameraClearanceUnits=prior;boundary.navigation.horizontalClearanceUnits=prior;el.cameraClearance.value=prior.toFixed(2);logDecision('Camera clearance edit rejected because it excludes the current camera pose.',false);return;}
 if(runtimeAvailable()&&activeMode!=='debug')handle.setNavigationPolicy('architectural-envelope',getPose(),policyValidator);
 syncEditorSummary();
}
async function saveBoundaryConfig() {
 const h=boundary.horizontalBoundary;
 const response=await fetch('/api/boundary/manual',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({schemaVersion:1,status:'MANUALLY_ADJUSTED',selectedEnvelopeId:generatedBoundary.id,sourceModelSha256:health.sha256,manualVertices2d:h.vertices2d,cameraClearanceUnits:h.cameraClearanceUnits,notes:'Reviewer-adjusted from local camera-pose enclosure map.'})});
 const result=await response.json();if(!response.ok||!result.ok)throw new Error(result.error??'Manual boundary configuration was not saved.');
 h.classification='MANUALLY_ADJUSTED';h.manualConfigPath=result.path;el.boundarySaveStatus.textContent=`Saved separate manual boundary JSON at ${result.path} · ${result.savedAt}`;return result;
}
function resetAutomaticBoundary() {
 return fetch('/api/boundary/manual',{method:'DELETE'}).then(r=>r.json()).then(result=>{if(!result.ok)throw new Error(result.error??'Could not reset manual boundary.');return fetch('/api/boundary/generated').then(r=>r.json());}).then(generated=>{boundary=generated;el.candidateSelect.value=boundary.horizontalBoundary.candidateId;el.cameraClearance.value=boundary.horizontalBoundary.cameraClearanceUnits.toFixed(2);syncEditorSummary();if(runtimeAvailable()&&activeMode!=='debug')handle.setNavigationPolicy('architectural-envelope',getPose(),policyValidator);el.boundarySaveStatus.textContent='Reverted to generated camera-derived geometry; manual JSON remains separately cleared.';});
}
function installBoundaryMapEditor() {
 const canvas=el.boundaryMap;
 canvas.addEventListener('pointerdown',event=>{if(!mapTransform)return;const rect=canvas.getBoundingClientRect(),x=event.clientX-rect.left,y=event.clientY-rect.top,poly=boundary.horizontalBoundary.vertices2d;let nearest=-1,best=12;for(let i=0;i<poly.length;i++){const p=mapCanvasPoint(poly[i]),d=Math.hypot(p[0]-x,p[1]-y);if(d<best){best=d;nearest=i;}}if(nearest>=0){draggedVertex=nearest;canvas.setPointerCapture?.(event.pointerId);drawBoundaryMap();event.preventDefault();}});
 canvas.addEventListener('pointermove',event=>{if(draggedVertex<0||!mapTransform)return;const rect=canvas.getBoundingClientRect(),p=mapCanvasCoord(event.clientX-rect.left,event.clientY-rect.top),poly=boundary.horizontalBoundary.vertices2d,prior=poly[draggedVertex];poly[draggedVertex]=p;boundary.horizontalBoundary.verticesCommonViewer=poly.map(planeToWorld);if(!polygonValid(poly)||runtimeAvailable()&&!validPosition(getPose().position)){poly[draggedVertex]=prior;boundary.horizontalBoundary.verticesCommonViewer=poly.map(planeToWorld);logDecision('Vertex edit rejected: polygon would self-intersect or exclude the current camera.',false);}else{boundary.horizontalBoundary.classification='MANUALLY_ADJUSTED';boundary.horizontalBoundary.manualConfigPath=null;el.boundarySaveStatus.textContent='Unsaved manual vertex edit. Save to write a separate configuration JSON.';}syncEditorSummary();event.preventDefault();});
 const release=()=>{if(draggedVertex>=0){draggedVertex=-1;syncEditorSummary();}};canvas.addEventListener('pointerup',release);canvas.addEventListener('pointercancel',release);window.addEventListener('resize',()=>drawBoundaryMap());
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
 annotationsDoc=await fetch('/api/ghost-annotations').then(r=>r.json());annotationSaved=annotationsDoc.annotations??[];el.annotationList.replaceChildren();
 for(const a of annotationSaved){const row=document.createElement('div');row.className='annotation-row';row.textContent=`${a.annotationId} · ${a.viewLabel??a.viewPreset??'view'} · ${a.selectionPolygonNormalized.length} points · ${a.cameraPose.fov}°`;el.annotationList.append(row);}
 el.annotationStatus.textContent=annotationSaved.length?`${annotationSaved.length} saved side-view selection(s). Multi-view Gaussian localization is available through the local script.`:'No reviewer selections saved yet. Candidate Gaussian matching awaits at least one marked view.';
}
async function persistGhostAnnotation() {
 if(annotationDraft.length<3||!annotationPose)throw new Error('Draw a selection polygon with at least three points first.');
 const canvas=el.viewerHost.querySelector('canvas:not(#selectionOverlay)'),record={schemaVersion:1,status:'REVIEWER_SCREEN_SELECTION_NOT_A_GAUSSIAN_MASK',modelSha256:health.sha256,scene:'hanging-text',sourceImage:'IMG_0716',viewPreset:annotationPreset,viewLabel:el.annotationLabel.value||annotationPreset,cameraPose:annotationPose,cameraUpCommonViewer:[0,1,0],projection:{type:'perspective',fovDegrees:annotationPose.fov,fovConvention:'vertical',aspectRatio:canvas?.width/(canvas?.height||1),cssResolution:[canvas?.clientWidth??0,canvas?.clientHeight??0],drawingBufferResolution:[canvas?.width??0,canvas?.height??0],devicePixelRatio:window.devicePixelRatio||1},selectionPolygonNormalized:annotationDraft.map(p=>[Number(p[0].toFixed(7)),Number(p[1].toFixed(7))]),selectedPixelSpace:'normalized viewer canvas, origin top-left',depthKnown:false,visibilityKnown:false,createdAt:new Date().toISOString()};
 const response=await fetch('/api/ghost-annotations',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(record)}),result=await response.json();if(!response.ok||!result.ok)throw new Error(result.error??'Annotation was not saved.');await refreshGhostAnnotations();el.boundarySaveStatus.textContent=`Saved side-view annotation ${result.annotationId} to ${result.path}.`;return result;
}
function installGhostAnnotation() {
 const c=el.selectionOverlay;
 c.addEventListener('pointerdown',event=>{if(!annotationActive)return;const r=c.getBoundingClientRect();annotationDrawing=true;annotationDraft=[[clamp((event.clientX-r.left)/r.width,0,1),clamp((event.clientY-r.top)/r.height,0,1)]];annotationPose=JSON.parse(JSON.stringify(getPose()));drawSelectionOverlay();event.preventDefault();});
 c.addEventListener('pointermove',event=>{if(!annotationActive||!annotationDrawing)return;const r=c.getBoundingClientRect(),p=[clamp((event.clientX-r.left)/r.width,0,1),clamp((event.clientY-r.top)/r.height,0,1)],last=annotationDraft.at(-1);if(!last||Math.hypot((p[0]-last[0])*r.width,(p[1]-last[1])*r.height)>2)annotationDraft.push(p);drawSelectionOverlay();event.preventDefault();});
 const finish=()=>{if(annotationDrawing){annotationDrawing=false;drawSelectionOverlay();el.annotationStatus.textContent=`Selection drawn with ${annotationDraft.length} points. Save it, then capture another oblique view.`;}};c.addEventListener('pointerup',finish);c.addEventListener('pointercancel',finish);
}
function sourcePhotoFor(scene) {
  const spec=sceneSpec(scene);el.sourcePhoto.replaceChildren();
  const figure=document.createElement('figure'),img=document.createElement('img'),caption=document.createElement('figcaption');
  img.src=`/api/image/${encodeURIComponent(spec.source)}`;img.alt=`Original registered source photograph ${spec.source}`;img.loading='lazy';caption.textContent=`Original capture ${spec.source} · reference only; not pixel-aligned to the rendered camera.`;figure.append(img,caption);el.sourcePhoto.append(figure);
}
async function setDiagnosticPreset(preset) {
  const scene=el.diagnosticScene.value,{pose,spec,angleDegrees,distance,projectedDimensions:proj}=presetPose(scene,preset);annotationPreset=preset;
  if(activeMode==='debug') setMode('visitor-explore');
  const ok=applyPose(pose);
  if(!ok) {logDecision(`Diagnostic pose ${scene}/${preset} is outside an active boundary; no boundary check was bypassed.`,false);return false;}
  const meta=canvasMetadata();
  el.diagnosticPose.textContent=`Scene: ${spec.id}\nPreset: ${preset} · viewing angle ${angleDegrees.toFixed(1)}° from front\nCamera distance: ${distance.toFixed(3)} common units\nPosition: [${pose.position.map(x=>x.toFixed(4)).join(', ')}]\nTarget: [${pose.target.map(x=>x.toFixed(4)).join(', ')}]\nProjected artwork estimate: ${proj.widthPx} × ${proj.heightPx} drawing-buffer pixels\nRenderer: ${meta.renderer} · canvas ${meta.drawingBuffer.join(' × ')} · DPR ${meta.devicePixelRatio}\nSource reference: ${spec.source} · qualitative comparison, not aligned metrics.`;
  sourcePhotoFor(scene);updatePoseHud();
  return true;
}
function startArtworkOrbit() {
  const selected=el.artwork.value;
  const spec=sceneSpec(selected==='hanging-text'?'hanging-text':selected==='attention-008'?'photo-wall':selected==='attention-018'?'central-table':'attention-003');
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
  if(activeMode!=='debug'&&!validPosition(pose.position)){logDecision('Bookmark not saved: current position is outside the active architecture boundary.',false);return;}
  localStorage.setItem(bookmarkKey,JSON.stringify({pose,savedAt:new Date().toISOString(),modelSha256:health.sha256}));
  logDecision('Bookmark saved with the current baseline identity.',true);
}
function restoreBookmark() {
  let record;try{record=JSON.parse(localStorage.getItem(bookmarkKey)||'null')}catch{}
  if(!record?.pose){logDecision('No local bookmark is saved.',false);return;}
  if(record.modelSha256!==health.sha256){logDecision('Bookmark belongs to a different PLY identity and was rejected.',false);return;}
  if(activeMode==='debug')setMode('visitor-explore');
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
  const tick=now=>{
    if(activeMode!=='guided-tour')return;
    const next=route.waypoints[segment],duration=clamp(length(vSub(next.position,startPose.position))/1.25*1000,450,3500),t=clamp((now-start)/duration,0,1),pose=routePoseAt(route,segment-1,segment,t);
    if(!applyPose(pose)){clearTour();logDecision(`Guided tour stopped at waypoint ${segment+1}: the route encountered the current architectural boundary configuration.`,false);updatePoseHud();return;}
    if(t>=1){segment++;start=now;startPose=getPose();if(segment>=route.waypoints.length){clearTour();logDecision(`Completed ${route.title}; all interpolated positions passed the shared policy.`,true);updatePoseHud();return;}}
    tourRaf=requestAnimationFrame(tick);
  };
  logDecision(`Playing ${route.title} through the shared camera-derived enclosure/floor/ceiling policy.`,true);tourRaf=requestAnimationFrame(tick);
}
const acceptanceTasks=[
  ['free','Move freely around the whole exhibition without Debug Fly.'],['table','Fly above and pass through the central table.'],['low','Descend to inspect low artworks, then ascend again.'],['rotation','Rotate freely with mouse look from several viewing angles.'],['walls','Test an active wall, floor, ceiling, and corner for plausible stops.'],['controls','After WASD, drag the scene and click the controls normally.'],['modes','Switch Visitor Explore, Guided Tour, Artwork Orbit, and Debug Fly.'],['smooth','Assess whether movement feels smooth and intuitive.']
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
  const response=await fetch('/assets/spirula20k.ply',{cache:'no-store'});if(!response.ok)throw new Error(`Baseline PLY request failed (${response.status})`);
  const transform=await fetch('/api/transform').then(r=>{if(!r.ok)throw new Error(`Transform audit request failed (${r.status})`);return r.json();});
  const auditedNode=transform?.viewerSceneNode;
  const sceneTransform=auditedNode?{
    translation:auditedNode.translation,
    rotationQuaternionXYZW:auditedNode.rotationQuaternionXYZW??auditedNode.quaternionXYZW,
    uniformScale:auditedNode.uniformScale
  }:{translation:[1.191624914477,-1.919848582059,-.494804866022],rotationQuaternionXYZW:[-.695552342084,-.114758924056,-.10564161364,.70133955987],uniformScale:6.640819914527};
  if(!Array.isArray(sceneTransform.translation)||sceneTransform.translation.length!==3||!Array.isArray(sceneTransform.rotationQuaternionXYZW)||sceneTransform.rotationQuaternionXYZW.length!==4||!Number.isFinite(sceneTransform.uniformScale))throw new Error('Audited Phase 2EF scene transform is incomplete; refusing implicit identity transform.');
  handle=await createViewer({container:el.viewerHost,contentUrl:'/assets/spirula20k.ply',contentFilename:'spirula20k.ply',contents:Promise.resolve(response),settings,sceneTransform,renderer:'webgl',ui:false,aa:false,nofx:true,exposeGlobals:false});
  await new Promise((resolve,reject)=>{let done=false;const ready=()=>{if(done||!runtimeAvailable())return;done=true;clearTimeout(timeout);resolve();};const timeout=setTimeout(()=>{if(done)return;done=true;reject(new Error('Viewer did not render its first usable frame within 150 seconds.'));},150000);handle.events.on('firstFrame',ready);handle.events.on('loaded:changed',ready);handle.events.on('progress:changed',()=>{if(!handle.state.loaded)el.modelStatus.textContent=`Loading verified baseline PLY · ${Math.round(handle.state.progress)}%`;ready();});if(runtimeAvailable())ready();});
  if(!health.exactBaselineMatch)throw new Error('Served PLY does not match the preserved baseline identity; refusing to load it.');
  handle.setCollisionEnabled(false);handle.setMoveSpeed(Number(el.speed.value));
  if(!handle.setCameraPose(initial))throw new Error('Safe initial station failed architectural validation.');
  handle.setNavigationPolicy('architectural-envelope',initial,policyValidator);
  activeMode='visitor-explore';el.mode.value=activeMode;updateModeText();
  el.modelStatus.textContent=`Ready · ${health.modelBytes.toLocaleString()} bytes · exact preserved ROI-off baseline`;
  el.graphics.textContent=`${canvasMetadata().renderer} · WebGL · AA off · postprocessing off · DPR ${renderDpr()} · shared architecture policy active`;
  el.viewerHost.querySelector('.loading')?.remove();resizeSelectionOverlay();
  logDecision('Baseline loaded. Visitor Explore is free in XYZ inside the camera-derived enclosure, floor, and ceiling.',true);updatePoseHud();
  handle.app.on('frameend',updatePoseHud);
}
async function init() {
  [health,boundary,generatedBoundary,stationsDoc,tourDoc,candidateDoc,artifactEvidence,cameraDoc,footprintDoc,distributionDoc,annotationsDoc]=await Promise.all([
    fetch('/api/health',{cache:'no-store'}).then(r=>r.json()),fetch('/api/boundary',{cache:'no-store'}).then(r=>r.json()),fetch('/api/boundary/generated',{cache:'no-store'}).then(r=>r.json()),fetch('/api/stations').then(r=>r.json()),fetch('/api/tours').then(r=>r.json()),fetch('/api/candidates').then(r=>r.json()),fetch('/api/artifact-evidence').then(r=>r.json()),fetch('/api/cameras').then(r=>r.json()),fetch('/api/footprints').then(r=>r.json()),fetch('/api/distribution').then(r=>r.json()),fetch('/api/ghost-annotations').then(r=>r.json())
  ]);
  if(!health.ok)throw new Error(`Model identity check failed: ${health.sha256} (${health.modelBytes} bytes).`);
  el.serverStatus.textContent=`${health.modelBytes.toLocaleString()} bytes · ${health.indexedSourceImages} local photos`;
  el.modelHash.textContent=`SHA-256 ${health.sha256}`;
  el.modelStatus.textContent='Baseline identity verified · loading Gaussian renderer…';
  populateControls();
  el.cameraClearance.value=boundary.horizontalBoundary.cameraClearanceUnits.toFixed(2);el.boundarySummary.textContent=boundarySummaryText();syncEditorSummary();refreshGhostAnnotations();installBoundaryMapEditor();installGhostAnnotation();
  renderAcceptance();
  const sourceHash=await fetch('/api/health').then(r=>r.json());
  if(sourceHash.sha256!==health.expectedSha256)throw new Error('Baseline source hash changed during initialization.');
  await startViewer();
  sourcePhotoFor(el.diagnosticScene.value);installInputHandlers();
  drawBoundaryMap();
}

el.mode.addEventListener('change',()=>{try{setMode(el.mode.value);}catch(error){logDecision(`Mode switch failed: ${error.message}`);}});
el.speed.addEventListener('input',()=>{el.speedLabel.value=Number(el.speed.value).toFixed(1);handle?.setMoveSpeed(Number(el.speed.value));});
el.goStation.addEventListener('click',goToStation);
el.station.addEventListener('change',()=>{});
el.saveBookmark.addEventListener('click',saveBookmark);el.restoreBookmark.addEventListener('click',restoreBookmark);
el.returnExplore.addEventListener('click',()=>{orbitCenter=null;setMode('visitor-explore');if(!validPosition(getPose().position)){const spawn=spawnPose();handle.setNavigationPolicy('debug',getPose(),null);handle.setCameraPose(spawn);handle.setNavigationPolicy('architectural-envelope',spawn,policyValidator);}logDecision('Returned to Visitor Explore at a valid interior position.',true);updatePoseHud();});
el.playTour.addEventListener('click',()=>void playTour());el.orbitArtwork.addEventListener('click',startArtworkOrbit);
el.cameraClearance.addEventListener('change',refreshBoundary);el.candidateSelect.addEventListener('change',candidateChanged);
for(const toggle of [el.showCameras,el.showWalls,el.showCandidates,el.showNeighborGraph])toggle.addEventListener('change',drawBoundaryMap);
el.resetVertices.addEventListener('click',()=>{const choice=footprintDoc.candidates.find(c=>c.id===boundary.horizontalBoundary.candidateId);if(!choice)return;const prior=JSON.parse(JSON.stringify(boundary.horizontalBoundary.vertices2d));boundary.horizontalBoundary.vertices2d=JSON.parse(JSON.stringify(choice.polygon2d));boundary.horizontalBoundary.verticesCommonViewer=boundary.horizontalBoundary.vertices2d.map(planeToWorld);if(runtimeAvailable()&&!validPosition(getPose().position)){boundary.horizontalBoundary.vertices2d=prior;boundary.horizontalBoundary.verticesCommonViewer=prior.map(planeToWorld);logDecision('Reset rejected because the automatic candidate excludes the current camera.',false);return;}boundary.horizontalBoundary.classification='CAMERA_DERIVED';boundary.horizontalBoundary.manualConfigPath=null;syncEditorSummary();el.boundarySaveStatus.textContent='Restored the selected automatic candidate vertices.';});
el.saveBoundary.addEventListener('click',()=>void saveBoundaryConfig().then(()=>logDecision('Separate manual boundary JSON saved.',true)).catch(e=>{el.boundarySaveStatus.textContent=e.message;logDecision(e.message);}));
el.revertBoundary.addEventListener('click',()=>void resetAutomaticBoundary().then(()=>logDecision('Manual boundary reverted; the generated camera-derived candidate is active.',true)).catch(e=>{el.boundarySaveStatus.textContent=e.message;logDecision(e.message);}));
el.exportBoundary.addEventListener('click',()=>downloadJson('phase2er-camera-derived-boundary.json',boundary));
document.querySelectorAll('[data-preset]').forEach(button=>button.addEventListener('click',()=>void setDiagnosticPreset(button.dataset.preset)));
el.diagnosticScene.addEventListener('change',()=>{sourcePhotoFor(el.diagnosticScene.value);el.diagnosticPose.textContent='Choose a view angle to place the diagnostic camera.';});
el.captureDiagnostic.addEventListener('click',()=>{const pose=getPose();downloadJson('phase2er-diagnostic-camera.json',{schemaVersion:1,status:'CAMERA_METADATA_ONLY',modelSha256:health.sha256,scene:el.diagnosticScene.value,pose,renderer:canvasMetadata(),activeMode,activePolicy:activePolicy(),capturedAt:new Date().toISOString()});});
el.exportCamera.addEventListener('click',()=>downloadJson('phase2er-camera-render-metadata.json',{schemaVersion:1,modelPath:health.modelPath,modelBytes:health.modelBytes,modelSha256:health.sha256,pose:getPose(),renderer:canvasMetadata(),activeMode,activePolicy:activePolicy(),boundary:boundary.id,exportedAt:new Date().toISOString()}));
el.annotationToggle.addEventListener('click',()=>annotationActive?stopGhostAnnotation():startGhostAnnotation());
el.annotationClear.addEventListener('click',()=>{annotationDraft=[];drawSelectionOverlay();el.annotationStatus.textContent='Current unsaved screen selection cleared.';});
el.annotationSave.addEventListener('click',()=>void persistGhostAnnotation().then(()=>{el.annotationStatus.textContent='Selection saved with exact camera pose and normalized screen polygon.';}).catch(e=>{el.annotationStatus.textContent=e.message;}));
el.annotationExport.addEventListener('click',()=>downloadJson('phase2er-hanging-text-side-view-annotations.json',annotationsDoc??{schemaVersion:1,modelSha256:health.sha256,annotations:annotationSaved}));
el.exportAcceptance.addEventListener('click',()=>downloadJson('phase2er-human-acceptance.json',{schemaVersion:1,status:'HUMAN_REVIEW_PENDING',checklist:JSON.parse(localStorage.getItem(acceptanceKey)||'{}'),exportedAt:new Date().toISOString()}));
el.resetAcceptance.addEventListener('click',()=>{localStorage.removeItem(acceptanceKey);renderAcceptance();});
window.addEventListener('resize',()=>{updatePoseHud();drawBoundaryMap();resizeSelectionOverlay();});
window.__phase2erTestHooks={
  getPose, getMode:()=>activeMode, getPolicy:activePolicy, getBoundary:()=>boundary,
  isValidPosition:validPosition, isSegmentAllowed:segmentAllowed, resolveMove, getLastBlock:()=>lastBlock, horizontalCoordinates,
  tryCameraPose:pose=>handle?.setCameraPose?.(pose)??false,
  setMode:mode=>{setMode(mode);return activeMode;},
  setSetupPose:pose=>{if(activeMode!=='debug')setMode('debug');return handle.setCameraPose(pose);},
  getPresetPose:(scene,preset)=>presetPose(scene,preset),
  wallApproachPose,
  setDiagnosticPreset:async(scene,preset)=>{el.diagnosticScene.value=scene;return setDiagnosticPreset(preset);},
  canvasMetadata, getActiveMode:()=>activeMode, getNavArmed:()=>navArmed, getBoundaryVertices:()=>boundary.horizontalBoundary.vertices2d, getBoundary:()=>boundary, getMapVertexScreen:index=>{const p=mapCanvasPoint(boundary.horizontalBoundary.vertices2d[index]),r=el.boundaryMap.getBoundingClientRect();return [r.left+p[0],r.top+p[1]];}, setBoundaryCandidate:id=>{el.candidateSelect.value=id;candidateChanged();return boundary.horizontalBoundary.candidateId;},
  getKeyState:()=>[...keyState], getSourceImageCount:()=>health.indexedSourceImages, setGhostAnnotationPreset:async preset=>{el.diagnosticScene.value='hanging-text';await setDiagnosticPreset(preset);return getPose();}, getGhostAnnotationCount:()=>annotationSaved.length
};
await init().catch(error=>{console.error(error);el.serverStatus.textContent='Local preview failed';el.modelStatus.textContent=error.stack||error.message;logDecision(error.message,false);});
