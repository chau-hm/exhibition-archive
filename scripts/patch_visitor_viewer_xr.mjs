import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const scriptPath = fileURLToPath(import.meta.url);
const root = path.resolve(path.dirname(scriptPath), '..');
const stockBundle = path.join(root, 'templates/visitor/vendor/supersplat-viewer.js');
const xrBundle = path.join(root, 'templates/visitor/vendor/supersplat-viewer-xr.js');
const expectedBaseSha256 = '408e3cc3cce59d4c438b23b6a27f8e47c385cc6b68c28ee1753af67da0d34bec';
const acceptedXrSha256 = '94d9a4c4ad9612385708c21602d5a7f481d352468667dee6d3335beb6f4478ac';

const homeMarkers = [
  'rebaseHome(){return this.homePosition.copy(this.entity.getPosition()),this.homeRotation.copy(this.entity.getRotation()),this.homeScale.copy(this.entity.getLocalScale()),!0}',
  'rebaseHome:()=>_s?.rebaseHome()??!1',
  'rebaseXRHomeTransform:()=>A.xr.rebaseHome()'
];
const cameraStateMarker = 'getCameraManagerState:()=>{let n=A.cameraManager,e=n?.camera;';
const cameraStateAccessor = 'getCameraManagerState:()=>{let n=A.cameraManager,e=n?.camera;const t=o=>o?{position:[o.position.x,o.position.y,o.position.z],angles:[o.angles.x,o.angles.y,o.angles.z],distance:o.distance,fov:o.fov}:null;return n&&e?{cameraMode:f.cameraMode,currentPose:t(e),targetPose:t(n.getTargetCamera?.()),targetPoseAvailable:typeof n.getTargetCamera==="function",transitionProgress:n.transitionProgress?.()??null}:null},';
const cameraManagerTargetMarker = 'this.getTargetCamera=()=>E';
const cameraManagerTargetAliasMarker = 'this.targetCamera=E';
const cameraStateTargetMarker = 'targetPose:t(n.getTargetCamera?.())';
const floorStartMarker = 'ce&&!Et){let is=ce;';
const floorCompleteMarker = 'e.fire("xrFloorPlacementComplete",';
const legacyFloorStart = 'ce&&!Et){e.fire("xrFloorPlacementStarted",{initialFloorY:fp,rigPosition:T.getPosition().clone().toArray(),referenceSpaceType:h._referenceSpaceType??null});let is=ce;';
const legacyFloorComplete = 'is.floorAhead().then(Ps=>{let ft=Ps!==null&&!u&&h.active;ft&&(fp=Ps,Nx.y=Ps,I.groundHeight=Ps,mp()),e.fire("xrFloorPlacementComplete",{floorY:Ps,accepted:ft,groundHeight:I.groundHeight,rigPosition:T.getPosition().clone().toArray(),referenceSpaceType:h._referenceSpaceType??null})})';

function sha256(text) {
  return createHash('sha256').update(text).digest('hex');
}

function replaceOnce(source, before, after, label) {
  const first = source.indexOf(before);
  if (first < 0 || source.indexOf(before, first + before.length) >= 0) {
    throw new Error(`Expected exactly one pinned Viewer insertion point for ${label}.`);
  }
  return `${source.slice(0, first)}${after}${source.slice(first + before.length)}`;
}

export function patchViewerText(source, options = {}) {
  const { visitorCompatibility = false } = options;
  const hasHomePatch = homeMarkers.every(marker => source.includes(marker));
  if (!hasHomePatch && homeMarkers.some(marker => source.includes(marker))) {
    throw new Error('Pinned Viewer bundle contains a partial XR home-transform patch.');
  }

  let result = source;
  if (!hasHomePatch) {
    result = replaceOnce(
      result,
      'restore(){this.end(),this.restoreHome(),this.placedScale=1}destroy(){',
      'rebaseHome(){return this.homePosition.copy(this.entity.getPosition()),this.homeRotation.copy(this.entity.getRotation()),this.homeScale.copy(this.entity.getLocalScale()),!0}restore(){this.end(),this.restoreHome(),this.placedScale=1}destroy(){',
      'XrObjectGrab snapshot method'
    );
    result = replaceOnce(
      result,
      'return{start:A4,end:Bx,setCollision:',
      'return{start:A4,end:Bx,rebaseHome:()=>_s?.rebaseHome()??!1,setCollision:',
      'XR integration facade'
    );
    result = replaceOnce(
      result,
      'endXR:()=>A.endXR(),destroy:()=>A.destroy()',
      'endXR:()=>A.endXR(),rebaseXRHomeTransform:()=>A.xr.rebaseHome(),destroy:()=>A.destroy()',
      'Viewer public facade'
    );
  }
  if (!result.includes(cameraStateMarker)) {
    const cameraStateInsertionPoint = visitorCompatibility
      ? 'captureFrame:b=>A.captureFrame(b),'
      : 'rebaseXRHomeTransform:()=>A.xr.rebaseHome(),destroy:()=>A.destroy()';
    const cameraStateInsertion = visitorCompatibility
      ? `${cameraStateInsertionPoint}${cameraStateAccessor}`
      : `${cameraStateAccessor}rebaseXRHomeTransform:()=>A.xr.rebaseHome(),destroy:()=>A.destroy()`;
    result = replaceOnce(
      result,
      cameraStateInsertionPoint,
      cameraStateInsertion,
      'CameraManager state diagnostic accessor'
    );
  }
  if (!result.includes(cameraManagerTargetMarker)) {
    result = replaceOnce(
      result,
      'camera=new Yl;constructor(e,t,s=null,r=!0){',
      'camera=new Yl;getTargetCamera;constructor(e,t,s=null,r=!0){',
      'CameraManager target pose diagnostic field'
    );
    result = replaceOnce(
      result,
      'this.camera.copy(d);let E=new Yl(this.camera),y=new Yl(this.camera),T=',
      'this.camera.copy(d);let E=new Yl(this.camera),y=new Yl(this.camera);this.getTargetCamera=()=>E;let T=',
      'CameraManager target pose diagnostic accessor'
    );
  }
  if (visitorCompatibility && !result.includes(cameraManagerTargetAliasMarker)) {
    result = replaceOnce(
      result,
      'let T=()=>p?"orbit":l?"walk":"fly",C=null,A=p?"orbit":"fly";',
      'let T=()=>p?"orbit":l?"walk":"fly",C=null,A=p?"orbit":"fly";this.targetCamera=E;',
      'Visitor CameraManager target pose compatibility alias'
    );
  }
  if (!result.includes(cameraStateTargetMarker) && result.includes('targetPose:t(n.targetCamera)')) {
    result = replaceOnce(
      result,
      'targetPose:t(n.targetCamera)',
      'targetPose:t(n.getTargetCamera?.()),targetPoseAvailable:typeof n.getTargetCamera==="function"',
      'CameraManager target pose accessor source'
    );
  }
  if (result.includes(legacyFloorStart)) {
    result = replaceOnce(
      result,
      legacyFloorStart,
      'ce&&!Et){e.fire("xrFloorPlacementStarted",{initialFloorY:fp,referenceSpaceType:h.referenceSpaceType??null});let is=ce;',
      'XR asynchronous splat-floor placement start diagnostics'
    );
  }
  if (result.includes(legacyFloorComplete)) {
    result = replaceOnce(
      result,
      legacyFloorComplete,
      'is.floorAhead().then(Ps=>{let ft=Ps!==null&&!u&&h.active;ft&&(fp=Ps,Nx.y=Ps,I.groundHeight=Ps,mp()),e.fire("xrFloorPlacementComplete",{floorY:Ps,accepted:ft,groundHeight:I.groundHeight,referenceSpaceType:h.referenceSpaceType??null})})',
      'XR asynchronous splat-floor placement completion diagnostics'
    );
  }
  if (!result.includes(floorCompleteMarker)) {
    result = replaceOnce(
      result,
      floorStartMarker,
      'ce&&!Et){e.fire("xrFloorPlacementStarted",{initialFloorY:fp,referenceSpaceType:h.referenceSpaceType??null});let is=ce;',
      'XR asynchronous splat-floor placement start'
    );
    result = replaceOnce(
      result,
      'is.floorAhead().then(Ps=>{Ps===null||u||!h.active||(fp=Ps,Nx.y=Ps,I.groundHeight=Ps,mp())})',
      'is.floorAhead().then(Ps=>{let ft=Ps!==null&&!u&&h.active;ft&&(fp=Ps,Nx.y=Ps,I.groundHeight=Ps,mp()),e.fire("xrFloorPlacementComplete",{floorY:Ps,accepted:ft,groundHeight:I.groundHeight,referenceSpaceType:h.referenceSpaceType??null})})',
      'XR asynchronous splat-floor placement completion'
    );
  }
  return result;
}

const palmMenuMarker = 'window.__visitorXRDebug?.recenter?.("xr_palm_menu")';
const palmMenuAnchor = '...t.debug?[{label:()=>"Debug  \\u203A",submenu:b4}]:[],{label:()=>l(h.type==="immersive-ar"?"xr-menu.exit-ar":"xr-menu.exit-vr")';
const oldRecenterLabel = 'window.__visitorLocales?.[document.documentElement.lang]?.recenter??"Recenter"';
const localizedRecenterLabel = 'window.__visitorLocales?.[document.querySelector("#languageSelect")?.value||document.documentElement.lang]?.recenter??"Recenter"';

export function patchVisitorXrMenuText(bundle) {
  if (bundle.includes(palmMenuMarker)) {
    if (bundle.includes(localizedRecenterLabel)) return bundle;
    if (bundle.includes(oldRecenterLabel)) return bundle.replace(oldRecenterLabel, localizedRecenterLabel);
    throw new Error('The pinned Viewer XR Palm Menu Recenter hook has an unexpected label expression.');
  }
  const matches = bundle.split(palmMenuAnchor).length - 1;
  if (matches !== 1) throw new Error(`Expected one pinned Viewer 1.42.0 XR menu anchor, found ${matches}.`);
  const addition = `{label:()=>${localizedRecenterLabel},visible:()=>Boolean(window.__visitorXRDebug?.boundaryState?.().outOfBounds),action:()=>{${palmMenuMarker}?.catch?.(()=>{})}},`;
  return bundle.replace(palmMenuAnchor, addition + palmMenuAnchor);
}

export function patchVisitorViewerText(stockText) {
  const cameraAndHome = patchViewerText(stockText, { visitorCompatibility: true });
  return patchVisitorXrMenuText(cameraAndHome);
}

export async function patchVisitorViewer({ sourcePath = stockBundle, destinationPath = xrBundle } = {}) {
  const source = await readFile(sourcePath, 'utf8');
  const sourceSha256 = sha256(source);
  if (sourceSha256 !== expectedBaseSha256) {
    throw new Error(`Refusing to patch an unsupported SuperSplat Viewer bundle: expected ${expectedBaseSha256}, got ${sourceSha256}.`);
  }
  const patched = patchVisitorViewerText(source);
  const outputSha256 = sha256(patched);
  if (outputSha256 !== acceptedXrSha256) {
    throw new Error(`Patched Viewer output differs from the accepted XR baseline: expected ${acceptedXrSha256}, got ${outputSha256}.`);
  }
  await writeFile(destinationPath, patched);
  return { sourceSha256, outputSha256, source: sourcePath, destination: destinationPath,
           viewerVersion: '1.42.0', playcanvasVersion: '2.23.3', patchVersion: 'exhibition-archive-xr-v1' };
}

if (process.argv[1] && path.resolve(process.argv[1]) === scriptPath) {
  const result = await patchVisitorViewer();
  console.log(JSON.stringify(result));
}
