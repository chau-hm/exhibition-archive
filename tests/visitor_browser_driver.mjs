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
const cases = JSON.parse(await readFile(args.cases, 'utf8'));
const screenshotsDir = args.screenshots;
await mkdir(screenshotsDir, { recursive: true });

const targetResponse = await fetch(cdpUrl + '/json/list');
const targets = await targetResponse.json();
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
  const entries = listeners.get(method) ?? [];
  entries.push(callback);
  listeners.set(method, entries);
}
function call(method, params = {}) {
  const id = ++commandId;
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      pending.delete(id);
      reject(new Error('Chrome DevTools command timed out: ' + method));
    }, 30_000);
    pending.set(id, {
      resolve: (value) => { clearTimeout(timeout); resolve(value); },
      reject: (error) => { clearTimeout(timeout); reject(error); }
    });
    socket.send(JSON.stringify({ id, method, params }));
  });
}
async function evaluate(expression) {
  const response = await call('Runtime.evaluate', {
    expression,
    returnByValue: true,
    awaitPromise: true,
    userGesture: true
  });
  if (response.exceptionDetails) {
    const details = response.exceptionDetails;
    throw new Error(details.exception?.description ?? details.text ?? 'Page evaluation failed');
  }
  return response.result?.value;
}
const checks = [];
const browserErrors = [];
const consoleErrors = [];
const networkErrors = [];
const performanceRows = [];
const screenshots = [];
on('Runtime.exceptionThrown', (params) => browserErrors.push(params.exceptionDetails?.exception?.description ?? params.exceptionDetails?.text ?? 'Page exception'));
on('Runtime.consoleAPICalled', (params) => {
  if (params.type === 'error') consoleErrors.push((params.args ?? []).map((arg) => arg.value ?? arg.description ?? '').join(' '));
});
on('Network.responseReceived', (params) => {
  if (params.response?.status >= 400) networkErrors.push({ url: params.response.url, status: params.response.status });
});
on('Network.loadingFailed', (params) => {
  if (params.errorText !== 'net::ERR_ABORTED') networkErrors.push({ requestId: params.requestId, error: params.errorText, url: params.request?.url });
});
await call('Page.enable');
await call('Page.addScriptToEvaluateOnNewDocument', { source: `window.__phase4dDiagnostics={errors:[],unhandled:[]};window.addEventListener('error',event=>window.__phase4dDiagnostics.errors.push(String(event.message||event.error||'window error')));window.addEventListener('unhandledrejection',event=>window.__phase4dDiagnostics.unhandled.push(String(event.reason?.stack||event.reason||'unhandled rejection')));` });
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
function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}
function buildUrl(prefix, format, noCollision = false) {
  const root = siteUrl + (prefix ? '/' + prefix : '') + '/';
  const url = new URL(root);
  url.searchParams.set('acceptance', '1');
  url.searchParams.set('lang', 'zh-Hant');
  if (format === 'ply') url.searchParams.set('format', 'ply');
  if (noCollision) url.searchParams.set('collision', 'off');
  url.searchParams.set('poses', Buffer.from(JSON.stringify(cases.viewpoints)).toString('base64'));
  return url.toString();
}
async function loadPage(prefix, format, noCollision = false) {
  const url = buildUrl(prefix, format, noCollision);
  const start = Date.now();
  await call('Page.navigate', { url });
  const deadline = Date.now() + 180_000;
  let state = null;
  let ui = null;
  while (Date.now() < deadline) {
    try {
      const sample = await evaluate(`(() => {
        const api = window.__phase4dAcceptance;
        const canvas = document.querySelector('#viewer canvas');
        const loading = document.querySelector('#loading');
        return { state: api?.state ?? null, canvas: Boolean(canvas && canvas.width && canvas.height),
          loadingHidden: Boolean(loading?.hidden), error: document.querySelector('#loading')?.classList.contains('load-error') || false };
      })()`);
      state = sample?.state ?? null;
      if (state?.loaded && sample.canvas && sample.loadingHidden) break;
      if (sample?.error) throw new Error('Visitor page displayed a loading error');
    } catch (error) {
      if (String(error).includes('Visitor page displayed')) throw error;
    }
    await sleep(250);
  }
  if (!state?.loaded) throw new Error('Visitor viewer failed to load at ' + url);
  console.error('acceptance: loaded ' + (prefix || 'root') + ' ' + format);
  await sleep(900);
  const final = await evaluate(`(() => {
    const api = window.__phase4dAcceptance;
    const root = document.querySelector('#exhibition');
    const visibleButtons = [...document.querySelectorAll('button')].filter((button) => !button.closest('[hidden]'));
    return {
      state: api.state,
      path: location.pathname,
      title: document.querySelector('h1')?.textContent?.trim(),
      visibleButtons: visibleButtons.map((button) => button.id),
      selectors: document.querySelectorAll('select').length,
      canvasCount: document.querySelectorAll('#viewer canvas').length,
      helpHidden: document.querySelector('#helpPanel')?.hidden,
      bodyText: document.body.innerText,
      rootClass: root?.className,
      resources: performance.getEntriesByType('resource').map((entry) => ({
        name: entry.name, durationMs: entry.duration, transferSize: entry.transferSize,
        encodedBodySize: entry.encodedBodySize, decodedBodySize: entry.decodedBodySize
      })).filter((entry) => entry.name.includes('/assets/exhibition.'))
    };
  })()`);
  const pageDiagnostics = await evaluate('window.__phase4dDiagnostics || {errors:[],unhandled:[]}');
  browserErrors.push(...(pageDiagnostics.errors ?? []), ...(pageDiagnostics.unhandled ?? []));
  const expectedPath = prefix ? '/' + prefix + '/' : '/';
  check('viewer loads at ' + (prefix ? 'nested' : 'root') + ' URL (' + format.toUpperCase() + ')', final.path === expectedPath,
    { url, actualPath: final.path, expectedPath });
  check('loaded in locked Fly Mode', final.state.cameraMode === 'fly', { mode: final.state.cameraMode, walkAllowed: final.state.walkAllowed });
  check('requested model format is active', final.state.activeFormat === format,
    { requested: format, active: final.state.activeFormat, renderer: final.state.renderer });
  check('visitor presentation shows only Reset View and Help', JSON.stringify(final.visibleButtons.sort()) === JSON.stringify([...cases.expected_ui_buttons].sort()),
    { buttons: final.visibleButtons });
  check('navigation help starts open and there is one language selector', !final.helpHidden && final.selectors === 1,
    { helpHidden: final.helpHidden, selectCount: final.selectors, canvasCount: final.canvasCount });
  check('visitor page text has no tour or diagnostic panel', !/guided tour|route selector|diagnostic|geometry overlay|technical status/i.test(final.bodyText),
    { bodyText: final.bodyText });
  const geometry = measurePosition(final.state.position, cases.envelope, cases.up_alignment);
  check('opening camera is inside the accepted enclosure', geometry.contained, geometry);
  const openingPose = cases.viewpoints[0].pose;
  const targetViewer = openingPose.position;
  const framingDistance = distance(targetViewer, final.state.position);
  check('loaded camera matches the requested viewpoint', framingDistance < 0.45,
    { distance_viewer_units: framingDistance, authored_camera_supersplat: targetViewer, actual_camera_supersplat: final.state.position });
  const forward = normalize(final.state.forward);
  const right = normalize(final.state.right);
  const expectedRight = normalize(cross(forward, [0, 1, 0]));
  const expectedUp = normalize(cross(expectedRight, forward));
  check('opening camera has no unintended roll and uses aligned world up',
    dot(right, expectedRight) > 0.995 && dot(normalize(final.state.up), expectedUp) > 0.995,
    { forward, right, up: final.state.up, expectedRight, expectedUp, euler: final.state.euler });
  const timing = {
    viewpoint: 'opening',
    label: 'accepted opening camera',
    prefix: prefix || '/',
    format,
    load_duration_ms: Math.round(final.state.loadDurationMs),
    navigation_elapsed_ms: Date.now() - start,
    renderer: final.state.renderer,
    model_resources: final.resources,
    active_pose_common: viewerToCommon(final.state.position, cases.up_alignment)
  };
  performanceRows.push(timing);
  return { state: final.state, pose: openingPose, final, timing, geometry };
}
function dot(a, b) { return a.reduce((sum, value, index) => sum + value * b[index], 0); }
function distance(a, b) { return Math.hypot(...a.map((value, index) => value - b[index])); }
function cross(a, b) { return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]; }
function normalize(a) { const length = Math.hypot(...a) || 1; return a.map((value) => value / length); }
function rotateQuaternion(q, point) {
  const x = q[0], y = q[1], z = q[2], w = q[3];
  const px = point[0], py = point[1], pz = point[2];
  const tx = 2 * (y * pz - z * py);
  const ty = 2 * (z * px - x * pz);
  const tz = 2 * (x * py - y * px);
  return [px + w * tx + y * tz - z * ty,
    py + w * ty + z * tx - x * tz,
    pz + w * tz + x * ty - y * tx];
}
function viewerToCommon(position, upAlignment) {
  return rotateQuaternion([-upAlignment[0], -upAlignment[1], -upAlignment[2], upAlignment[3]], position);
}
function measurePosition(position, envelope, upAlignment) {
  const common = viewerToCommon(position, upAlignment);
  const floor = envelope.floor;
  const height = dot(common, floor.normal) + floor.offset;
  const floorMargin = height - floor.clearance;
  const ceilingLimit = envelope.ceiling.heightAlongFloorNormal - envelope.ceiling.clearance;
  const ceilingMargin = ceilingLimit - height;
  const boundary = envelope.horizontalBoundary;
  const basis = boundary.planeBasis;
  const delta = common.map((value, index) => value - basis.pointOnPlaneCommonViewer[index]);
  const uv = [dot(delta, basis.basisU), dot(delta, basis.basisV)];
  const polygon = boundary.vertices2d;
  const area = polygon.reduce((sum, point, index) => sum + point[0] * polygon[(index + 1) % polygon.length][1] - polygon[(index + 1) % polygon.length][0] * point[1], 0);
  const orientation = area >= 0 ? 1 : -1;
  let minBoundaryDistance = Infinity;
  for (let index = 0; index < polygon.length; index++) {
    const a = polygon[index], b = polygon[(index + 1) % polygon.length];
    const edge = [b[0] - a[0], b[1] - a[1]];
    const length = Math.hypot(...edge);
    const cross = edge[0] * (uv[1] - a[1]) - edge[1] * (uv[0] - a[0]);
    minBoundaryDistance = Math.min(minBoundaryDistance, orientation * cross / length);
  }
  const horizontalMargin = minBoundaryDistance - boundary.cameraClearanceUnits;
  return {
    common,
    uv,
    height: Number(height.toFixed(4)),
    floor_clearance_margin: Number(floorMargin.toFixed(4)),
    ceiling_clearance_margin: Number(ceilingMargin.toFixed(4)),
    nearest_horizontal_boundary_distance: Number(minBoundaryDistance.toFixed(4)),
    accepted_horizontal_clearance: boundary.cameraClearanceUnits,
    horizontal_clearance_margin: Number(horizontalMargin.toFixed(4)),
    contained: floorMargin >= -0.08 && ceilingMargin >= -0.08 && horizontalMargin >= -0.08
  };
}
async function clickElement(selector) {
  const rect = await evaluate(`(() => { const e = document.querySelector(${JSON.stringify(selector)}); if (!e) return null; const r = e.getBoundingClientRect(); return {x:r.x+r.width/2,y:r.y+r.height/2}; })()`);
  if (!rect) throw new Error('Missing visitor control: ' + selector);
  await call('Input.dispatchMouseEvent', { type: 'mouseMoved', x: rect.x, y: rect.y });
  await call('Input.dispatchMouseEvent', { type: 'mousePressed', x: rect.x, y: rect.y, button: 'left', clickCount: 1 });
  await call('Input.dispatchMouseEvent', { type: 'mouseReleased', x: rect.x, y: rect.y, button: 'left', clickCount: 1 });
  await sleep(150);
}
const KEY_MAP = {
  w: ['KeyW', 87], a: ['KeyA', 65], s: ['KeyS', 83], d: ['KeyD', 68],
  e: ['KeyE', 69], q: ['KeyQ', 81], '1': ['Digit1', 49], '3': ['Digit3', 51],
  f: ['KeyF', 70], v: ['KeyV', 86], g: ['KeyG', 71], Escape: ['Escape', 27]
};
async function keyDown(key) {
  const [code, keyCode] = KEY_MAP[key];
  await call('Input.dispatchKeyEvent', { type: 'rawKeyDown', key, code, windowsVirtualKeyCode: keyCode, nativeVirtualKeyCode: keyCode });
}
async function keyUp(key) {
  const [code, keyCode] = KEY_MAP[key];
  await call('Input.dispatchKeyEvent', { type: 'keyUp', key, code, windowsVirtualKeyCode: keyCode, nativeVirtualKeyCode: keyCode });
}
async function pressFor(key, milliseconds) {
  await keyDown(key);
  await sleep(milliseconds);
  await keyUp(key);
  await sleep(150);
}
async function snap(name) {
  await sleep(500);
  const image = await call('Page.captureScreenshot', { format: 'png', fromSurface: true, captureBeyondViewport: false });
  const file = path.join(screenshotsDir, name + '.png');
  await writeFile(file, Buffer.from(image.data, 'base64'));
  screenshots.push({ name, path: file, bytes: Buffer.from(image.data, 'base64').length });
}
async function setPose(index) {
  console.error('acceptance: setting camera ' + index);
  await evaluate(`window.__phase4dAcceptance.setPose(${index})`);
  console.error('acceptance: camera set ' + index);
  await sleep(250);
}

const views = cases.viewpoints;
const primary = cases.primary_format;
const noCollisionMode = args['no-collision'] === '1';
if (args['nested-smoke'] === '1') {
  const nested = await loadPage('nested', primary, noCollisionMode);
  check('relative static assets load beneath /nested/', nested.final.path === '/nested/' && nested.state.activeFormat === primary,
    { path: nested.final.path, activeFormat: nested.state.activeFormat, resources: nested.final.resources });
  console.log(JSON.stringify({ status: checks.every((item) => item.ok) ? 'PASS' : 'FAIL', checks,
    browser_errors: browserErrors, console_errors: consoleErrors, network_errors: networkErrors,
    performance: performanceRows, screenshots }));
  socket.close();
  process.exit(0);
}
let firstOpening = await loadPage('', primary, noCollisionMode);
if (args['motion-smoke'] === '1') {
  const start = firstOpening.state.position;
  await evaluate('window.__phase4dAcceptance.move(0, 0.45)');
  await sleep(700);
  await evaluate('window.__phase4dAcceptance.stop()');
  await sleep(200);
  const end = await evaluate('window.__phase4dAcceptance.state');
  check('controlled collision configuration matches the request', end.hasCollision === !noCollisionMode,
    { requestedCollision: !noCollisionMode, actualCollision: end.hasCollision });
  check('official Fly movement API translates from the canonical opening spawn',
    distance(start, end.position) > 0.12 && end.cameraMode === 'fly',
    { start, end: end.position, delta: end.position.map((value, index) => value - start[index]),
      cameraMode: end.cameraMode, hasCollision: end.hasCollision });
  console.log(JSON.stringify({ status: checks.every((item) => item.ok) ? 'PASS' : 'FAIL', checks,
    browser_errors: browserErrors, console_errors: consoleErrors, network_errors: networkErrors,
    performance: performanceRows, screenshots }));
  socket.close();
  process.exit(0);
}
if (args['input-smoke'] === '1') {
  const start = firstOpening.state;
  await evaluate(`(() => { window.__visitorKeyEvents=[]; for (const type of ['keydown','keyup']) window.addEventListener(type,event=>window.__visitorKeyEvents.push({type,code:event.code,target:event.target?.tagName,active:document.activeElement?.tagName,defaultPrevented:event.defaultPrevented}),true); })()`);
  const verticalStart = await evaluate('window.__phase4dAcceptance.state');
  await pressFor('e', 700);
  const afterRiseState = await evaluate('window.__phase4dAcceptance.state');
  const afterRise = measurePosition(afterRiseState.position, cases.envelope, cases.up_alignment).height;
  await pressFor('q', 700);
  const afterDescendState = await evaluate('window.__phase4dAcceptance.state');
  const afterDescend = measurePosition(afterDescendState.position, cases.envelope, cases.up_alignment).height;
  const apiMotionStart = await evaluate('window.__phase4dAcceptance.state');
  await evaluate('window.__phase4dAcceptance.move(0, 0.45)');
  await sleep(700);
  await evaluate('window.__phase4dAcceptance.stop()');
  await sleep(200);
  const apiMotionEnd = await evaluate('window.__phase4dAcceptance.state');
  check('public Fly movement API remains live after vertical key input',
    distance(apiMotionStart.position, apiMotionEnd.position) > 0.12,
    { start: apiMotionStart.position, end: apiMotionEnd.position, inputEnabled: apiMotionEnd.inputEnabled,
      cameraMode: apiMotionEnd.cameraMode, activeElement: apiMotionEnd.activeElement,
      start_render: { auto: apiMotionStart.appAutoRender, updates: apiMotionStart.appUpdates,
        frames: apiMotionStart.appFrames, next: apiMotionStart.renderNextFrame },
      end_render: { auto: apiMotionEnd.appAutoRender, updates: apiMotionEnd.appUpdates,
        frames: apiMotionEnd.appFrames, next: apiMotionEnd.renderNextFrame } });
  check('native E and Q keys provide free vertical movement',
    afterRise > measurePosition(verticalStart.position, cases.envelope, cases.up_alignment).height + 0.08 &&
    afterDescend < afterRise - 0.08,
    { before: verticalStart.position, after_e: afterRiseState.position, after_q: afterDescendState.position,
      before_height: measurePosition(verticalStart.position, cases.envelope, cases.up_alignment).height,
      after_e_height: afterRise, after_q_height: afterDescend,
      inputEnabled: afterDescendState.inputEnabled, activeElement: afterDescendState.activeElement,
      before_render: { auto: verticalStart.appAutoRender, updates: verticalStart.appUpdates,
        frames: verticalStart.appFrames, next: verticalStart.renderNextFrame },
      after_e_render: { auto: afterRiseState.appAutoRender, updates: afterRiseState.appUpdates,
        frames: afterRiseState.appFrames, next: afterRiseState.renderNextFrame },
      after_q_render: { auto: afterDescendState.appAutoRender, updates: afterDescendState.appUpdates,
        frames: afterDescendState.appFrames, next: afterDescendState.renderNextFrame },
      observedKeys: await evaluate('window.__visitorKeyEvents') });
  await pressFor('w', 700);
  const afterForward = await evaluate('window.__phase4dAcceptance.state');
  check('native W key moves the Fly camera forward',
    afterForward.position.map((value, index) => value - start.position[index])
      .reduce((sum, value, index) => sum + value * start.forward[index], 0) > 0.08,
    { start: start.position, end: afterForward.position, forward: start.forward,
      delta: afterForward.position.map((value, index) => value - start.position[index]),
      gamingControls: afterForward.gamingControls, pointerLocked: afterForward.pointerLocked });
  const lookStart = await evaluate('window.__phase4dAcceptance.state');
  const canvasRect = await evaluate(`(() => {const r=document.querySelector('#viewer canvas').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()`);
  await call('Input.dispatchMouseEvent', { type: 'mouseMoved', x: canvasRect.x, y: canvasRect.y });
  await call('Input.dispatchMouseEvent', { type: 'mousePressed', x: canvasRect.x, y: canvasRect.y, button: 'left', clickCount: 1 });
  await call('Input.dispatchMouseEvent', { type: 'mouseMoved', x: canvasRect.x + 95, y: canvasRect.y + 30, button: 'left', buttons: 1 });
  await call('Input.dispatchMouseEvent', { type: 'mouseReleased', x: canvasRect.x + 95, y: canvasRect.y + 30, button: 'left', clickCount: 1 });
  await sleep(300);
  const lookEnd = await evaluate('window.__phase4dAcceptance.state');
  check('native mouse input turns the Fly camera', distance(lookStart.forward, lookEnd.forward) > 0.02,
    { before: lookStart.forward, after: lookEnd.forward, pointerLocked: lookEnd.pointerLocked,
      gamingControls: lookEnd.gamingControls });
  await snap('input-smoke');
  console.log(JSON.stringify({ status: checks.every((item) => item.ok) && browserErrors.length === 0 &&
    consoleErrors.length === 0 && networkErrors.length === 0 ? 'PASS' : 'FAIL', checks,
    browser_errors: browserErrors, console_errors: consoleErrors, network_errors: networkErrors,
    performance: performanceRows, screenshots }));
  socket.close();
  process.exit(0);
}
for (let index = 0; index < views.length; index++) {
  if (index) await setPose(index);
  await snap(views[index].id + '-' + primary);
}
console.error('acceptance: viewpoint screenshots complete');

if (primary === 'sog') {
  firstOpening = await loadPage('', 'ply');
  for (let index = 0; index < views.length; index++) {
    if (index) await setPose(index);
    await snap(views[index].id + '-ply');
  }
}


const wallView = views.find((view) => view.id === 'wall-boundary');
if (wallView) {
  const wallStart = measurePosition(await evaluate('window.__phase4dAcceptance.state.position'), cases.envelope, cases.up_alignment);
  await evaluate('window.__phase4dAcceptance.move(0, 1)');
  await sleep(2500);
  await evaluate('window.__phase4dAcceptance.stop()');
  await sleep(250);
  const wallState = await evaluate('window.__phase4dAcceptance.state');
  const wallGeometry = measurePosition(wallState.position, cases.envelope, cases.up_alignment);
  check('camera first moves toward open space and then stops at an enclosure wall', distance(wallStart.common, wallGeometry.common) > 0.75 && wallGeometry.contained &&
    wallGeometry.nearest_horizontal_boundary_distance < cases.envelope.horizontalBoundary.cameraClearanceUnits + 0.45,
    { start: wallStart, stopped: wallGeometry, cameraMode: wallState.cameraMode });
  const wallPosition = [...wallGeometry.common];
  await evaluate('window.__phase4dAcceptance.move(1, 0)');
  await sleep(650);
  await evaluate('window.__phase4dAcceptance.stop()');
  const slideState = await evaluate('window.__phase4dAcceptance.state');
  const slideGeometry = measurePosition(slideState.position, cases.envelope, cases.up_alignment);
  check('movement slides along a wall without leaving the enclosure', slideGeometry.contained &&
    distance(wallPosition, slideGeometry.common) > 0.12 &&
    slideGeometry.nearest_horizontal_boundary_distance >= cases.envelope.horizontalBoundary.cameraClearanceUnits - 0.12,
    { before: wallGeometry, after: slideGeometry });
  await clickElement('#resetView');
  await sleep(1700);
  const wallReset = await evaluate('window.__phase4dAcceptance.state');
  const wallResetDistance = distance(viewerToCommon(wallView.pose.position, cases.up_alignment), viewerToCommon(wallReset.position, cases.up_alignment));
  check('Reset from the wall restores a valid authored Fly spawn', wallResetDistance < 0.45 &&
    measurePosition(wallReset.position, cases.envelope, cases.up_alignment).contained,
    { distance_common_units: wallResetDistance, position: wallReset.position });
  console.error('acceptance: wall checks complete');
}

const verticalView = views.find((view) => view.id === 'opening');
if (verticalView) {
  await setPose(views.indexOf(verticalView));
  await pressFor('e', 2200);
  const ceilingState = await evaluate('window.__phase4dAcceptance.state');
  const ceilingGeometry = measurePosition(ceilingState.position, cases.envelope, cases.up_alignment);
  const ceilingLimit = cases.envelope.ceiling.heightAlongFloorNormal - cases.envelope.ceiling.clearance;
  check('free flight is clamped at the accepted ceiling', ceilingGeometry.contained && ceilingLimit - ceilingGeometry.height < 0.5,
    { state: ceilingGeometry, ceiling_limit: ceilingLimit });
  await pressFor('q', 2200);
  const floorState = await evaluate('window.__phase4dAcceptance.state');
  const floorGeometry = measurePosition(floorState.position, cases.envelope, cases.up_alignment);
  check('free flight is clamped at the accepted floor', floorGeometry.contained && floorGeometry.height - cases.envelope.floor.clearance < 0.5,
    { state: floorGeometry, floor_limit: cases.envelope.floor.clearance });
  console.error('acceptance: vertical boundary checks complete');
}

if (firstOpening) {
  await clickElement('#resetView');
  await sleep(1700);
  console.error('acceptance: opening pose restored');
  const initialState = firstOpening.state;
  const initialHelpText = await evaluate('document.body.innerText');
  const uiState = await evaluate(`(() => ({expanded:document.querySelector('#helpToggle').getAttribute('aria-expanded'),hidden:document.querySelector('#helpPanel').hidden,inputEnabled:window.__phase4dAcceptance.state.inputEnabled}))()`);
  check('navigation help is visible on first load and controls match its instructions',
    uiState.expanded === 'true' && !uiState.hidden && initialHelpText.includes('W / A / S / D') &&
      initialHelpText.includes('E / Q') && /mouse|滑鼠/u.test(initialHelpText),
    { ...uiState, bodyText: initialHelpText });
  await snap('opening-zh-Hant');

  const translatedBefore = await evaluate('window.__phase4dAcceptance.state');
  const modelRequestCount = await evaluate(`performance.getEntriesByType('resource').filter(entry=>entry.name.includes('/assets/exhibition.')).length`);
  await evaluate(`(() => { const e=document.querySelector('#languageSelect'); e.value='en'; e.dispatchEvent(new Event('change',{bubbles:true})); })()`);
  await sleep(200);
  const englishState = await evaluate(`(() => ({state:window.__phase4dAcceptance.state,text:document.body.innerText,lang:document.documentElement.lang,selected:document.querySelector('#languageSelect').value,modelRequests:performance.getEntriesByType('resource').filter(entry=>entry.name.includes('/assets/exhibition.')).length,title:document.querySelector('h1')?.textContent}))()`);
  check('English language switch updates controls without reloading or moving the scene',
    englishState.lang === 'en' && englishState.selected === 'en' && /Reset view/.test(englishState.text) &&
      distance(translatedBefore.position, englishState.state.position) < 0.01 && englishState.state.activeFormat === translatedBefore.activeFormat &&
      englishState.modelRequests === modelRequestCount,
    { before: translatedBefore.position, after: englishState.state.position, language: englishState.lang,
      activeFormat: englishState.state.activeFormat, text: englishState.text });
  await snap('opening-en');
  await evaluate(`(() => { const e=document.querySelector('#languageSelect'); e.value='zh-Hant'; e.dispatchEvent(new Event('change',{bubbles:true})); })()`);
  await sleep(200);
  const chineseText = await evaluate('document.body.innerText');
  check('Traditional Chinese locale contains no accidental Simplified Chinese text',
    !/[检视览选语开启读败无请关说动后进]/u.test(chineseText), { text: chineseText });
  await snap('opening-zh-Hant-active');

  const helpClose = await (async () => {
    await clickElement('#helpToggle');
    return evaluate(`(() => ({expanded:document.querySelector('#helpToggle').getAttribute('aria-expanded'),hidden:document.querySelector('#helpPanel').hidden}))()`);
  })();
  check('Help hides and reopens its instructions without a separate panel control', helpClose.hidden && helpClose.expanded === 'false', helpClose);
  const helpOpen = await (async () => {
    await clickElement('#helpToggle');
    return evaluate(`(() => ({expanded:document.querySelector('#helpToggle').getAttribute('aria-expanded'),hidden:document.querySelector('#helpPanel').hidden}))()`);
  })();
  check('Help instructions can be reopened', !helpOpen.hidden && helpOpen.expanded === 'true', helpOpen);

  await call('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await sleep(250);
  const narrow = await evaluate(`(() => ({width:innerWidth,scrollWidth:document.documentElement.scrollWidth,controls:[...document.querySelectorAll('.top-controls h1,.top-actions, .bottom-controls')].map(e=>{const r=e.getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,bottom:r.bottom}})}))()`);
  check('visitor controls remain within a narrow viewport', narrow.scrollWidth <= narrow.width && narrow.controls.every((r) => r.left >= -1 && r.right <= narrow.width + 1), narrow);
  await snap('narrow-zh-Hant');
  await call('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
  await sleep(200);

  const apiMoveStart = await evaluate('window.__phase4dAcceptance.state');
  await evaluate('window.__phase4dAcceptance.move(0, 0.55)');
  await sleep(700);
  await evaluate('window.__phase4dAcceptance.stop()');
  await sleep(200);
  const apiMoveEnd = await evaluate('window.__phase4dAcceptance.state');
  const apiMoveDelta = apiMoveEnd.position.map((value, index) => value - apiMoveStart.position[index]);
  check('official Fly movement API moves away from the opening spawn',
    distance(apiMoveStart.position, apiMoveEnd.position) > 0.12 && apiMoveEnd.cameraMode === 'fly',
    { start: apiMoveStart.position, end: apiMoveEnd.position, delta: apiMoveDelta,
      cameraMode: apiMoveEnd.cameraMode, collision: apiMoveEnd.collision });
  await clickElement('#resetView');
  await sleep(1700);

  const startingPose = await evaluate('window.__phase4dAcceptance.state');
  const canvasRect = await evaluate(`(() => {const r=document.querySelector('#viewer canvas').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()`);
  await call('Input.dispatchMouseEvent', { type: 'mouseMoved', x: canvasRect.x, y: canvasRect.y });
  await call('Input.dispatchMouseEvent', { type: 'mousePressed', x: canvasRect.x, y: canvasRect.y, button: 'left', clickCount: 1 });
  await sleep(200);
  await call('Input.dispatchMouseEvent', { type: 'mouseMoved', x: canvasRect.x + 95, y: canvasRect.y + 30, button: 'left', buttons: 1 });
  await call('Input.dispatchMouseEvent', { type: 'mouseReleased', x: canvasRect.x + 95, y: canvasRect.y + 30, button: 'left', clickCount: 1 });
  await sleep(500);
  const lookedPose = await evaluate('window.__phase4dAcceptance.state');
  check('left-drag uses SuperSplat mouse look without pointer lock',
    !lookedPose.gamingControls && !lookedPose.pointerLocked && distance(startingPose.forward, lookedPose.forward) > 0.02,
    { before: startingPose.forward, after: lookedPose.forward, gamingControls: lookedPose.gamingControls,
      pointerLocked: lookedPose.pointerLocked, documentFocused: lookedPose.documentFocused,
      canvasConnected: lookedPose.canvasConnected, canvasInActiveDocument: lookedPose.canvasInActiveDocument,
      pointerLockErrorCount: lookedPose.pointerLockErrorCount });

  const testKey = async (key, basis, direction) => {
    const before = await evaluate('window.__phase4dAcceptance.state');
    await pressFor(key, 450);
    const after = await evaluate('window.__phase4dAcceptance.state');
    const delta = after.position.map((value, index) => value - before.position[index]);
    return { before, after, delta, projection: delta.reduce((sum, value, index) => sum + value * basis[index] * direction, 0) };
  };
  for (const [key, axis, direction] of [['w','forward',1], ['s','forward',-1], ['d','right',1], ['a','right',-1]]) {
    const movement = await testKey(key, (await evaluate(`window.__phase4dAcceptance.state.${axis}`)), direction);
    check(`${key.toUpperCase()} key moves in its labelled direction and stops on release`, movement.projection > 0.06,
      { delta: movement.delta, projection: movement.projection, activeElement: movement.after.activeElement,
        inputEnabled: movement.after.inputEnabled, gamingControls: movement.after.gamingControls });
  }
  const diagonal = await evaluate('window.__phase4dAcceptance.state');
  await keyDown('w'); await keyDown('d'); await sleep(350); await keyUp('w'); await keyUp('d'); await sleep(200);
  const diagonalAfter = await evaluate('window.__phase4dAcceptance.state');
  const diagonalDelta = diagonalAfter.position.map((value, index) => value - diagonal.position[index]);
  check('combined forward and strafe input moves diagonally',
    diagonalDelta.reduce((sum, value, index) => sum + value * diagonal.forward[index], 0) > 0.05 &&
    diagonalDelta.reduce((sum, value, index) => sum + value * diagonal.right[index], 0) > 0.05,
    { delta: diagonalDelta, before: diagonal.position, after: diagonalAfter.position });

  const keyboardState = await evaluate('window.__phase4dAcceptance.state');
  check('documented movement input remains enabled while the canvas owns keyboard input',
    keyboardState.cameraMode === 'fly' && !keyboardState.gamingControls && keyboardState.inputEnabled,
    { cameraMode: keyboardState.cameraMode, gamingControls: keyboardState.gamingControls,
      inputEnabled: keyboardState.inputEnabled, pointerLocked: keyboardState.pointerLocked });

  const beforeVertical = measurePosition((await evaluate('window.__phase4dAcceptance.state')).position, cases.envelope, cases.up_alignment).height;
  await pressFor('e', 700);
  const afterRise = measurePosition((await evaluate('window.__phase4dAcceptance.state')).position, cases.envelope, cases.up_alignment).height;
  await pressFor('q', 700);
  const afterDescend = measurePosition((await evaluate('window.__phase4dAcceptance.state')).position, cases.envelope, cases.up_alignment).height;
  check('Q and E provide free vertical movement in both directions', afterRise > beforeVertical + 0.08 && afterDescend < afterRise - 0.08,
    { before: beforeVertical, after_e: afterRise, after_q: afterDescend,
      cameraMode: (await evaluate('window.__phase4dAcceptance.state')).cameraMode,
      pointerLocked: (await evaluate('window.__phase4dAcceptance.state')).pointerLocked });

  for (const key of ['1', '3', 'f', 'v', 'g']) await pressFor(key, 70);
  const afterShortcuts = await evaluate('window.__phase4dAcceptance.state');
  check('Walk, Orbit, framing, overlay, and gaming-mode shortcuts stay blocked',
    afterShortcuts.cameraMode === 'fly' && afterShortcuts.gamingControls === false,
    { cameraMode: afterShortcuts.cameraMode, gamingControls: afterShortcuts.gamingControls,
      pointerLocked: afterShortcuts.pointerLocked });

  const initialPosition = firstOpening.state.position;
  await pressFor('Escape', 70);
  await clickElement('#resetView');
  await sleep(1700);
  const afterReset = await evaluate('window.__phase4dAcceptance.state');
  const resetDistance = distance(initialPosition, afterReset.position);
  check('Reset View restores the authored opening pose inside the enclosure',
    resetDistance < 0.45 && afterReset.cameraMode === 'fly' && measurePosition(afterReset.position, cases.envelope, cases.up_alignment).contained,
    { distance_viewer_units: resetDistance, after_reset: afterReset.position,
      geometry: measurePosition(afterReset.position, cases.envelope, cases.up_alignment) });
  check('camera remains contained after reset and browser input transitions',
    measurePosition(afterReset.position, cases.envelope, cases.up_alignment).contained,
    measurePosition(afterReset.position, cases.envelope, cases.up_alignment));
  console.error('acceptance: visitor UI, controls and reset checks complete');
}

const failures = checks.filter((item) => !item.ok);
const report = {
  status: failures.length === 0 && browserErrors.length === 0 && consoleErrors.length === 0 && networkErrors.length === 0 ? 'PASS' : 'FAIL',
  checks,
  browser_errors: browserErrors,
  console_errors: consoleErrors,
  network_errors: networkErrors,
  performance: performanceRows,
  screenshots
};
console.log(JSON.stringify(report));
socket.close();
