import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';

function argsFrom(argv) {
  const result = {};
  for (let index = 2; index < argv.length; index += 2) result[argv[index].replace(/^--/, '')] = argv[index + 1];
  return result;
}

const args = argsFrom(process.argv);
const cdpUrl = args.cdp;
const siteUrl = args.site;
const visitor = args.visitor;
const reviewer = args.reviewer;
const output = args.output;
const timeoutMs = Number(args['timeout-seconds'] ?? 600) * 1000;
await mkdir(output, { recursive: true });
const metadata = JSON.parse(await readFile(path.join(visitor, 'config', 'exhibition.json'), 'utf8'));
const settings = JSON.parse(await readFile(path.join(visitor, 'config', 'settings.json'), 'utf8'));
const initial = settings.cameras?.[0]?.initial;
const orientation = metadata.initial_camera?.orientation ?? null;
const sceneTransform = metadata.initial_camera?.model_scene_transform_supersplat;
if (!initial || !sceneTransform) throw new Error('Generated Visitor package has no initial camera or model transform');

const targets = await (await fetch(cdpUrl + '/json/list')).json();
const target = targets.find((item) => item.type === 'page');
if (!target) throw new Error('Chrome has no page target');
const socket = new WebSocket(target.webSocketDebuggerUrl);
const pending = new Map();
const listeners = new Map();
let commandId = 0;
await new Promise((resolve, reject) => {
  socket.addEventListener('open', resolve, { once: true });
  socket.addEventListener('error', reject, { once: true });
});
socket.addEventListener('message', (event) => {
  const message = JSON.parse(String(event.data));
  if (message.id) {
    const wait = pending.get(message.id);
    if (!wait) return;
    pending.delete(message.id);
    if (message.error) wait.reject(new Error(message.error.message));
    else wait.resolve(message.result);
    return;
  }
  for (const callback of listeners.get(message.method) ?? []) callback(message.params);
});
function on(method, callback) {
  const callbacks = listeners.get(method) ?? [];
  callbacks.push(callback);
  listeners.set(method, callbacks);
}
function call(method, params = {}) {
  const id = ++commandId;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error('Chrome DevTools command timed out: ' + method));
    }, 30_000);
    pending.set(id, {
      resolve: (value) => { clearTimeout(timer); resolve(value); },
      reject: (error) => { clearTimeout(timer); reject(error); }
    });
    socket.send(JSON.stringify({ id, method, params }));
  });
}
async function evaluate(expression) {
  const response = await call('Runtime.evaluate', {
    expression, returnByValue: true, awaitPromise: true, userGesture: true
  });
  if (response.exceptionDetails) {
    throw new Error(response.exceptionDetails.exception?.description ?? response.exceptionDetails.text ?? 'Page evaluation failed');
  }
  return response.result?.value;
}
const checks = [];
const browserErrors = [];
const consoleErrors = [];
const networkErrors = [];
const screenshots = [];
on('Runtime.exceptionThrown', (params) => browserErrors.push(
  params.exceptionDetails?.exception?.description ?? params.exceptionDetails?.text ?? 'page exception'));
on('Runtime.consoleAPICalled', (params) => {
  if (params.type === 'error') consoleErrors.push((params.args ?? []).map((item) => item.value ?? item.description ?? '').join(' '));
});
on('Network.responseReceived', (params) => {
  if (params.response?.status >= 400) networkErrors.push({ url: params.response.url, status: params.response.status });
});
on('Network.loadingFailed', (params) => {
  if (params.errorText !== 'net::ERR_ABORTED') networkErrors.push({ url: params.request?.url, error: params.errorText });
});
await call('Page.enable');
await call('Runtime.enable');
await call('Network.enable');
await call('Log.enable');
await call('Page.setWebLifecycleState', { state: 'active' });
await call('Emulation.setFocusEmulationEnabled', { enabled: true });
await call('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
await call('Page.bringToFront');

function check(name, ok, details = {}) {
  checks.push({ name, ok: Boolean(ok), ...details });
}
function sleep(ms) { return new Promise((resolve) => setTimeout(resolve, ms)); }
function dot(a, b) { return a.reduce((sum, value, index) => sum + value * b[index], 0); }
function subtract(a, b) { return a.map((value, index) => value - b[index]); }
function distance(a, b) { return Math.hypot(...subtract(a, b)); }
function normalize(vector) { const length = Math.hypot(...vector) || 1; return vector.map((value) => value / length); }
function cross(a, b) { return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]; }
function rotateQuaternion(q, point) {
  const [x, y, z, w] = q;
  const [px, py, pz] = point;
  const tx = 2 * (y * pz - z * py);
  const ty = 2 * (z * px - x * pz);
  const tz = 2 * (x * py - y * px);
  return [px + w * tx + y * tz - z * ty,
    py + w * ty + z * tx - x * tz,
    pz + w * tz + x * ty - y * tx];
}
function basisCheck(state) {
  const forward = normalize(state.forward);
  const expectedRight = normalize(cross(forward, [0, 1, 0]));
  const expectedUp = normalize(cross(expectedRight, forward));
  return { ok: dot(normalize(state.right), expectedRight) > 0.99 &&
      dot(normalize(state.up), expectedUp) > 0.99,
    forward, right: state.right, up: state.up, expectedRight, expectedUp };
}
async function stateOf(frame = 'window') {
  return evaluate(`(() => {
    const target = ${frame};
    const api = target.__phase4dAcceptance;
    const canvas = target.document.querySelector('#viewer canvas');
    return { state: api?.state ?? null, canvas: Boolean(canvas && canvas.width && canvas.height),
      loadingHidden: Boolean(target.document.querySelector('#loading')?.hidden),
      loadError: target.document.querySelector('#loading')?.classList.contains('load-error') || false };
  })()`);
}
async function waitForViewer(frame = 'window') {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const sample = await stateOf(frame);
    if (sample.state?.loaded && sample.canvas && sample.loadingHidden) return sample.state;
    if (sample.loadError) throw new Error('Generated Visitor displayed a load error');
    await sleep(250);
  }
  throw new Error('Generated Visitor did not load before the browser timeout');
}
async function screenshot(name) {
  const result = await call('Page.captureScreenshot', { format: 'png', fromSurface: true });
  const file = path.join(output, `${name}.png`);
  const bytes = Buffer.from(result.data, 'base64');
  await writeFile(file, bytes);
  screenshots.push({ name, path: file, bytes: bytes.length });
}
async function currentState() {
  const current = await evaluate('window.__phase4dAcceptance?.state ?? null');
  if (!current) throw new Error('Visitor acceptance camera state is unavailable');
  return current;
}
async function clickReset() {
  const found = await evaluate(`(() => { const button=document.querySelector('#resetView'); if (!button) return false; button.click(); return true; })()`);
  if (!found) throw new Error('Reset View button was not found');
  await sleep(1550);
  return currentState();
}
function resetCheck(state) {
  const expectedForward = normalize(subtract(initial.target, initial.position));
  return { positionError: distance(state.position, initial.position),
    forwardDot: dot(normalize(state.forward), expectedForward), basis: basisCheck(state) };
}
async function press(code, keyCode) {
  const key = code.slice(-1).toLowerCase();
  await call('Input.dispatchKeyEvent', { type: 'keyDown', key, code,
    windowsVirtualKeyCode: keyCode, nativeVirtualKeyCode: keyCode });
  await sleep(350);
  await call('Input.dispatchKeyEvent', { type: 'keyUp', key, code,
    windowsVirtualKeyCode: keyCode, nativeVirtualKeyCode: keyCode });
  await sleep(450);
  return currentState();
}
async function moveCheck(code, keyCode, axis, sign, label) {
  const before = await currentState();
  const after = await press(code, keyCode);
  const delta = subtract(after.position, before.position);
  const projection = dot(delta, axis);
  check(label, sign * projection > 0.035, { delta, projectedMovement: projection, before: before.position, after: after.position });
  return after;
}

await call('Page.navigate', { url: `${siteUrl}/visitor/index.html?acceptance=1` });
let initialState;
try {
  initialState = await waitForViewer();
} catch (error) {
  let sample = null;
  try { sample = await stateOf(); } catch {}
  try { await screenshot('visitor-load-timeout'); } catch {}
  const report = { status: 'FAIL', checks: [{ name: 'Visitor model reaches loaded state', ok: false,
    error: String(error), state: sample }], browserErrors, consoleErrors, networkErrors, screenshots };
  await new Promise((resolve) => process.stdout.write(JSON.stringify(report), resolve));
  socket.close();
  process.exit(1);
}
if (orientation?.candidate_up_colmap_xyz) {
  const expectedUp = rotateQuaternion(orientation.rotation_quaternion_xyzw, orientation.candidate_up_colmap_xyz);
  check('camera-up estimate maps to viewer +Y', distance(expectedUp, [0, 1, 0]) < 1e-6,
    { mappedUp: expectedUp, sourceUp: orientation.candidate_up_colmap_xyz });
} else {
  check('camera-up evidence is recorded for the generated preview', false,
    { orientationStatus: orientation?.status ?? 'LEGACY_PREVIEW_WITHOUT_ORIENTATION_EVIDENCE' });
}
check('opening pose matches the settings camera', distance(initialState.position, initial.position) < 0.04 &&
  dot(normalize(initialState.forward), normalize(subtract(initial.target, initial.position))) > 0.995,
  { actualPosition: initialState.position, initialPosition: initial.position, actualForward: initialState.forward });
check('opening camera is level to viewer Y-up', basisCheck(initialState).ok, basisCheck(initialState));
check('model and camera use the same orientation transform',
  JSON.stringify(initialState.modelTransform.rotationQuaternionXYZW) === JSON.stringify(sceneTransform.rotationQuaternionXYZW) &&
  JSON.stringify(initialState.modelTransform.translation) === JSON.stringify(sceneTransform.translation),
  { modelTransform: initialState.modelTransform, cameraTransform: sceneTransform });
check('ordinary Visitor does not expose technical navigation diagnostics',
  await evaluate("!document.querySelector('.navigation-diagnostic-panel')"));
check('ordinary Visitor states its free-flight boundary is unbounded until approval',
  await evaluate("document.querySelector('.preview-status')?.textContent.includes('Free flight is unbounded until navigation approval')"));
await screenshot('visitor-opening');

for (let index = 1; index <= 2; index++) {
  const reset = await clickReset();
  const result = resetCheck(reset);
  check(`Reset View ${index} restores the corrected opening pose`, result.positionError < 0.04 &&
    result.forwardDot > 0.995 && result.basis.ok, result);
  if (index === 1) await screenshot('visitor-after-reset');
}

const axes = basisCheck(await currentState());
await moveCheck('KeyW', 87, normalize((await currentState()).forward), 1, 'W moves forward');
await clickReset();
await moveCheck('KeyS', 83, normalize((await currentState()).forward), -1, 'S moves backward');
await clickReset();
await moveCheck('KeyD', 68, normalize((await currentState()).right), 1, 'D moves laterally right');
await clickReset();
await moveCheck('KeyA', 65, normalize((await currentState()).right), -1, 'A moves laterally left');
await clickReset();
await moveCheck('KeyE', 69, [0, 1, 0], 1, 'E moves upward in viewer Y');
await clickReset();
await moveCheck('KeyQ', 81, [0, 1, 0], -1, 'Q moves downward in viewer Y');
await clickReset();
check('movement and reset preserve the initial world-up frame', basisCheck(await currentState()).ok, axes);

const rect = await evaluate(`(() => { const box=document.querySelector('#viewer canvas')?.getBoundingClientRect(); return box ? {x:box.x+box.width/2,y:box.y+box.height/2}:null; })()`);
const lookStart = await currentState();
await call('Input.dispatchMouseEvent', { type: 'mouseMoved', x: rect.x, y: rect.y });
await call('Input.dispatchMouseEvent', { type: 'mousePressed', x: rect.x, y: rect.y, button: 'left', clickCount: 1 });
await call('Input.dispatchMouseEvent', { type: 'mouseMoved', x: rect.x + 90, y: rect.y + 24, button: 'left', buttons: 1 });
await sleep(120);
await call('Input.dispatchMouseEvent', { type: 'mouseReleased', x: rect.x + 90, y: rect.y + 24, button: 'left', clickCount: 1 });
await sleep(500);
const lookEnd = await currentState();
check('mouse look changes heading without moving or rolling the camera',
  distance(lookStart.position, lookEnd.position) < 0.03 &&
  dot(normalize(lookStart.forward), normalize(lookEnd.forward)) < 0.999 && basisCheck(lookEnd).ok,
  { positionDelta: distance(lookStart.position, lookEnd.position),
    forwardDot: dot(normalize(lookStart.forward), normalize(lookEnd.forward)), basis: basisCheck(lookEnd) });
const afterMouseReset = await clickReset();
check('Reset View clears mouse-look heading and roll', resetCheck(afterMouseReset).forwardDot > 0.995 && basisCheck(afterMouseReset).ok,
  resetCheck(afterMouseReset));
await screenshot('visitor-after-controls-reset');

await call('Page.navigate', { url: `${siteUrl}/reviewer/index.html` });
const deadline = Date.now() + timeoutMs;
let reviewerState = null;
while (Date.now() < deadline) {
  const sample = await evaluate(`(() => {
    const frame=document.querySelector('iframe');
    const api=frame?.contentWindow?.__phase4dAcceptance;
    const canvas=frame?.contentDocument?.querySelector('#viewer canvas');
    return {src:frame?.getAttribute('src')??null, title:document.querySelector('h1')?.textContent?.trim()??'',
      state:api?.state??null, canvas:Boolean(canvas&&canvas.width&&canvas.height)};
  })()`);
  reviewerState = sample;
  if (sample.state?.loaded && sample.canvas) break;
  await sleep(250);
}
const reviewerCamera = reviewerState?.state;
const reviewerPose = reviewerCamera ? resetCheck(reviewerCamera) : { positionError: Infinity, forwardDot: -1, basis: { ok: false } };
check('Technical Reviewer embeds the same corrected Visitor pose',
  reviewerState?.title?.startsWith('Technical Reviewer') && reviewerState.src === '../visitor/index.html?acceptance=1' &&
  reviewerPose.positionError < 0.04 && reviewerPose.forwardDot > 0.995 && reviewerPose.basis.ok,
  { title: reviewerState?.title, frameSource: reviewerState?.src, pose: reviewerPose });
await screenshot('reviewer-opening');

await call('Page.navigate', { url: `${siteUrl}/visitor/index.html?acceptance=1&navigationDiagnostics=1` });
await waitForViewer();
const framingControls = await evaluate(`(() => ({
  perspective: Boolean(document.querySelector('[data-nav-frame="perspective"]')),
  topDown: Boolean(document.querySelector('[data-nav-frame="top-down"]')),
  reset: Boolean(document.querySelector('[data-nav-frame="reset"]'))
}))()`);
await evaluate(`document.querySelector('[data-nav-frame="perspective"]')?.click()`);
await sleep(1600);
const framedCameraState = await currentState();
check('diagnostic Frame boundary preset frames the candidate region',
  framingControls.perspective && framingControls.topDown && framingControls.reset &&
  distance(framedCameraState.position, lookEnd.position) > 2,
  { controls: framingControls, position: framedCameraState.position });
await evaluate(`document.querySelector('[data-nav-frame="top-down"]')?.click()`);
await sleep(1600);
const topDownCameraState = await currentState();
check('diagnostic Top down preset points along viewer Y',
  Math.abs(normalize(topDownCameraState.forward)[1]) > 0.95,
  { forward: topDownCameraState.forward });
await evaluate(`document.querySelector('[data-nav-frame="reset"]')?.click()`);
await sleep(1600);
const diagnosticResetState = await currentState();
check('diagnostic reset restores the normal opening camera',
  resetCheck(diagnosticResetState).positionError < 0.04 &&
  resetCheck(diagnosticResetState).forwardDot > 0.995,
  resetCheck(diagnosticResetState));
await evaluate(`document.querySelector('[data-nav-frame="perspective"]')?.click()`);
await sleep(1600);
const overlayState = await evaluate(`(async () => {
  const deadline = Date.now() + 15000;
  let panel, canvas, status;
  while (Date.now() < deadline) {
    panel = document.querySelector('.navigation-diagnostic-panel');
    canvas = document.querySelector('.navigation-diagnostic-canvas');
    status = panel?.querySelector('[data-diagnostic-status]')?.textContent ?? '';
    if (canvas?.width > 0 && status && !status.startsWith('Loading')) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  let coloredPixels = 0;
  if (canvas?.width && canvas?.height) {
    const context = canvas.getContext('2d');
    const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
    for (let index = 3; index < pixels.length; index += 4) if (pixels[index] > 0) coloredPixels++;
  }
  return { status, panel: Boolean(panel), overlayCanvas: Boolean(canvas?.width && canvas?.height),
    layerControlCount: panel?.querySelectorAll('[data-nav-layer]').length ?? 0,
    framingControlCount: panel?.querySelectorAll('[data-nav-frame]').length ?? 0,
    projection: panel?.querySelector('[data-diagnostic-projection]')?.textContent ?? '',
    unboundedWarning: panel?.textContent?.includes('remains unbounded') ?? false, coloredPixels };
})()`);
const panelLayout = await evaluate(`(() => {
  const panel = document.querySelector('.navigation-diagnostic-panel');
  const heading = panel?.querySelector('h2');
  if (!panel || !heading) return null;
  const panelRect = panel.getBoundingClientRect();
  const headingRect = heading.getBoundingClientRect();
  const topElement = document.elementFromPoint(headingRect.left + 8, headingRect.top + headingRect.height / 2);
  const css = getComputedStyle(heading);
  return { panelWidth: panelRect.width, headingWidth: headingRect.width,
    headingScrollWidth: heading.scrollWidth, headingClientWidth: heading.clientWidth,
    headingVisible: css.visibility !== 'hidden' && Number(css.opacity) > 0,
    hitTestInsidePanel: Boolean(topElement?.closest('.navigation-diagnostic-panel')) };
})()`);
check('technical overlay controls remain visible above the model canvas',
  panelLayout && panelLayout.panelWidth >= 250 && panelLayout.headingWidth > 100 &&
  panelLayout.headingScrollWidth <= panelLayout.headingClientWidth + 1 &&
  panelLayout.headingVisible && panelLayout.hitTestInsidePanel, panelLayout);
check('model-relative navigation overlay loads against the Visitor scene',
  overlayState.panel && overlayState.overlayCanvas && overlayState.layerControlCount >= 5 &&
  overlayState.framingControlCount === 3 &&
  overlayState.status.includes('NAVIGATION_NEEDS_REVIEW') && overlayState.status.includes('77/77 cameras') &&
  overlayState.unboundedWarning && overlayState.coloredPixels > 0 &&
  /Overlay projection: [1-9]\d*\/77 cameras in view · [1-9]\d* segments/.test(overlayState.projection), overlayState);
await screenshot('navigation-model-overlay');

check('page has no uncaught browser exceptions', browserErrors.length === 0, { browserErrors });
check('page has no failed network requests', networkErrors.length === 0, { networkErrors });
const report = { status: checks.every((item) => item.ok) ? 'PASS' : 'FAIL', checks,
  browserErrors, consoleErrors, networkErrors, screenshots };
process.stdout.write(JSON.stringify(report));
socket.close();
if (report.status !== 'PASS') process.exitCode = 1;
