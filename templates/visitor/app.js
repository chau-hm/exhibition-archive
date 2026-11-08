import { createViewer } from '../vendor/supersplat-viewer.js';

const $ = (selector) => document.querySelector(selector);
const host = $('#viewer');
const loading = $('#loading');
const loadingMessage = $('#loadingMessage');
const languageSelect = $('#languageSelect');
const resetButton = $('#resetView');
const helpButton = $('#helpToggle');
const helpPanel = $('#helpPanel');
const instructions = $('#navigationInstructions');
const held = new Set();
let viewer = null;
let metadata = null;
let strings = null;
let currentLanguage = 'en';
let flyLocked = false;
let testPoseAnnotations = [];
const now = () => performance.now();
const startedAt = now();

function focusViewer() {
  const canvas = host.querySelector('canvas');
  if (!canvas) return;
  canvas.tabIndex = 0;
  canvas.setAttribute('aria-label', strings.viewerAria);
  canvas.focus({ preventScroll: true });
}

function normalizeLanguage(value) {
  const locale = String(value ?? '').toLowerCase().replaceAll('_', '-');
  if (locale === 'en' || locale.startsWith('en-')) return 'en';
  if (['zh-hant', 'zh-tw', 'zh-hk', 'zh-mo'].some((supported) => locale === supported || locale.startsWith(`${supported}-`))) return 'zh-Hant';
  return null;
}

function chooseLanguage(defaultLanguage) {
  const query = new URLSearchParams(location.search);
  if (query.has('lang')) return normalizeLanguage(query.get('lang')) ?? 'en';
  for (const locale of navigator.languages ?? [navigator.language]) {
    const supported = normalizeLanguage(locale);
    if (supported) return supported;
  }
  return normalizeLanguage(defaultLanguage) ?? 'en';
}

function applyLanguage(language, focus = false) {
  currentLanguage = normalizeLanguage(language) ?? 'en';
  document.documentElement.lang = currentLanguage === 'zh-Hant' ? 'zh-Hant' : 'en';
  document.documentElement.dir = 'ltr';
  const locale = window.__visitorLocales[currentLanguage];
  $('#exhibition').setAttribute('aria-label', locale.exhibitionAria);
  host.setAttribute('aria-label', locale.viewerAria);
  loadingMessage.textContent = locale.loading;
  languageSelect.setAttribute('aria-label', locale.languageAria);
  languageSelect.title = locale.languageAria;
  resetButton.textContent = locale.reset;
  resetButton.title = locale.resetTitle;
  resetButton.setAttribute('aria-label', locale.reset);
  helpButton.textContent = locale.help;
  helpButton.title = locale.helpTitle;
  helpButton.setAttribute('aria-label', locale.help);
  helpPanel.setAttribute('aria-label', locale.helpTitle);
  instructions.replaceChildren(...locale.instructions.map((line) => {
    const item = document.createElement('li');
    item.textContent = line;
    return item;
  }));
  languageSelect.value = currentLanguage;
  if (focus) focusViewer();
}

function installVisitorInput() {
  const movementKeys = new Set(['KeyW', 'KeyA', 'KeyS', 'KeyD']);
  const verticalKeys = new Set(['KeyQ', 'KeyE']);
  const blockedModeKeys = new Set(['Digit1', 'Digit2', 'Digit3', 'KeyF', 'KeyG', 'KeyV']);
  let mouseLookHeld = false;
  let restoreAutoRender = false;
  let autoRenderBeforeInput = false;
  let settleTimer = 0;
  const updateMovement = () => {
    const x = Number(held.has('KeyD')) - Number(held.has('KeyA'));
    const z = Number(held.has('KeyW')) - Number(held.has('KeyS'));
    const magnitude = Math.hypot(x, z) || 1;
    if (viewer?.state.loaded && viewer.state.inputEnabled) viewer.setMoveInput(x / magnitude, z / magnitude);
  };
  const wakeViewer = () => {
    if (!viewer?.state.loaded) return;
    if (settleTimer) clearTimeout(settleTimer);
    settleTimer = 0;
    if (!restoreAutoRender) {
      autoRenderBeforeInput = viewer.app.autoRender;
      restoreAutoRender = true;
    }
    // The upstream viewer switches to on-demand rendering after the scene is
    // ready. Its input sources update in the app loop, so keep that loop active
    // during real-time input. Camera movement still goes through SuperSplat's
    // Fly controller and collision mover.
    viewer.app.autoRender = true;
    viewer.app.renderNextFrame = true;
  };
  const settleViewer = () => {
    if (!restoreAutoRender || held.size || mouseLookHeld || !viewer?.app) return;
    if (settleTimer) clearTimeout(settleTimer);
    settleTimer = setTimeout(() => {
      settleTimer = 0;
      if (held.size || mouseLookHeld || !viewer?.app) return;
      viewer.app.renderNextFrame = true;
      viewer.app.autoRender = autoRenderBeforeInput;
      restoreAutoRender = false;
    }, 400);
  };
  const onKeyDown = (event) => {
    if (movementKeys.has(event.code)) {
      event.preventDefault();
      event.stopImmediatePropagation();
      if (!viewer?.state.inputEnabled) return;
      held.add(event.code);
      updateMovement();
      wakeViewer();
      return;
    }
    if (verticalKeys.has(event.code)) {
      if (viewer?.state.inputEnabled) {
        held.add(event.code);
        wakeViewer();
      }
      return;
    }
    if (blockedModeKeys.has(event.code)) {
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  };
  const onKeyUp = (event) => {
    if (!movementKeys.has(event.code) && !verticalKeys.has(event.code)) return;
    if (movementKeys.has(event.code)) {
      event.preventDefault();
      event.stopImmediatePropagation();
    }
    held.delete(event.code);
    updateMovement();
    if (verticalKeys.has(event.code)) wakeViewer();
    settleViewer();
  };
  const stop = () => {
    held.clear();
    mouseLookHeld = false;
    viewer?.setMoveInput(0, 0);
    if (settleTimer) clearTimeout(settleTimer);
    settleTimer = 0;
    if (restoreAutoRender && viewer?.app) {
      viewer.app.renderNextFrame = true;
      viewer.app.autoRender = autoRenderBeforeInput;
      restoreAutoRender = false;
    }
  };
  window.addEventListener('keydown', onKeyDown, true);
  window.addEventListener('keyup', onKeyUp, true);
  window.addEventListener('blur', stop);
  document.addEventListener('visibilitychange', () => { if (document.hidden) stop(); });
  document.querySelectorAll('.top-controls, .bottom-controls').forEach((controls) => {
    controls.addEventListener('focusin', () => {
      viewer && (viewer.state.inputEnabled = false);
      stop();
    });
    controls.addEventListener('focusout', () => setTimeout(() => {
      if (!controls.contains(document.activeElement) && viewer) viewer.state.inputEnabled = true;
    }, 0));
  });
  host.addEventListener('pointerdown', (event) => {
    const canvas = host.querySelector('canvas');
    if (event.target !== canvas) return;
    if (viewer) viewer.state.inputEnabled = true;
    focusViewer();
    if (event.button === 0) {
      mouseLookHeld = true;
      wakeViewer();
    }
  });
  const releaseLook = (event) => {
    if (event.target !== host.querySelector('canvas')) return;
    mouseLookHeld = false;
    settleViewer();
  };
  host.addEventListener('pointerup', releaseLook);
  host.addEventListener('pointercancel', releaseLook);
  host.addEventListener('lostpointercapture', releaseLook);
}

function applyTestSettings(settings) {
  const params = new URLSearchParams(location.search);
  if (!__EXHIBITION_ARCHIVE_VISITOR_ACCEPTANCE__ || params.get('acceptance') !== '1') return;
  if (params.has('pose')) {
    const pose = JSON.parse(atob(params.get('pose')));
    settings.cameras ??= [{ initial: pose }];
    settings.cameras[0].initial = pose;
  }
  if (!params.has('poses')) return;
  const poses = JSON.parse(atob(params.get('poses')));
  if (!Array.isArray(poses) || poses.length > 16) throw new Error('Invalid browser acceptance poses.');
  settings.annotations ??= [];
  testPoseAnnotations = poses.map((record, index) => {
    const pose = record?.pose;
    if (!Array.isArray(pose?.position) || !Array.isArray(pose?.target) ||
        [...pose.position, ...pose.target].some((value) => !Number.isFinite(value))) {
      throw new Error('Invalid browser acceptance pose.');
    }
    const annotationIndex = settings.annotations.length;
    settings.annotations.push({ position: pose.position, title: `test-${index + 1}`, text: '', camera: { initial: pose } });
    return annotationIndex;
  });
}

function applyModelTransform(handle) {
  const transform = metadata.initial_camera.model_scene_transform_supersplat;
  if (!transform || !Array.isArray(transform.translation) || !Array.isArray(transform.rotationQuaternionXYZW) ||
      !Number.isFinite(transform.uniformScale)) throw new Error('The visitor package has no valid shared model transform.');
  const splats = handle.app.root.findComponents('gsplat');
  if (splats.length !== 1) throw new Error(`Expected one SuperSplat model entity; found ${splats.length}.`);
  const entity = splats[0].entity;
  const [x, y, z] = transform.translation;
  const [qx, qy, qz, qw] = transform.rotationQuaternionXYZW;
  entity.setLocalPosition(x, y, z);
  entity.setLocalRotation(qx, qy, qz, qw);
  entity.setLocalScale(transform.uniformScale, transform.uniformScale, transform.uniformScale);
}

function waitForLoaded(handle, timeoutMs) {
  if (handle.state.loaded) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      handle.events.off('loaded:changed', onLoaded);
      reject(new Error('The exhibition did not finish loading in time.'));
    }, timeoutMs);
    const onLoaded = (loaded) => {
      if (!loaded) return;
      clearTimeout(timeout);
      handle.events.off('loaded:changed', onLoaded);
      resolve();
    };
    handle.events.on('loaded:changed', onLoaded);
  });
}

const nextFrame = () => new Promise((resolve) => requestAnimationFrame(resolve));
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
function waitForFrames(app, count, timeoutMs) {
  return new Promise((resolve, reject) => {
    let remaining = count;
    const finish = (error) => {
      clearTimeout(timeout);
      app.off('frameend', onFrame);
      if (error) reject(error);
      else resolve();
    };
    const onFrame = () => {
      remaining--;
      if (remaining <= 0) finish();
    };
    const timeout = setTimeout(() => finish(new Error('The visitor opening frame did not settle.')), timeoutMs);
    app.on('frameend', onFrame);
  });
}

async function openModel(format, settings) {
  let model = metadata.models[format];
  const params = new URLSearchParams(location.search);
  if (format === 'ply' && !model?.available && __EXHIBITION_ARCHIVE_VISITOR_ACCEPTANCE__ &&
      params.get('acceptance') === '1' && params.has('comparisonPlyUrl')) {
    model = { available: true, filename: 'exhibition.ply', url: params.get('comparisonPlyUrl') };
  }
  if (!model?.available || typeof model.url !== 'string') throw new Error(`${format.toUpperCase()} is not included in this visitor package.`);
  const response = await fetch(model.url, { cache: 'force-cache' });
  if (!response.ok) throw new Error(`${format.toUpperCase()} ${strings[currentLanguage].assetError} (${response.status}).`);
  const handle = await createViewer({
    container: host,
    settings,
    contentUrl: model.url,
    contentFilename: model.filename,
    collisionUrl: (__EXHIBITION_ARCHIVE_VISITOR_ACCEPTANCE__ && new URLSearchParams(location.search).get('collision') === 'off')
      ? undefined : metadata.collision?.url,
    contents: Promise.resolve(response),
    renderer: 'webgpu',
    ui: false,
    lang: currentLanguage,
    exposeGlobals: false
  });
  try {
    await waitForLoaded(handle, format === 'sog' ? 60_000 : 180_000);
    applyModelTransform(handle);
    return handle;
  } catch (error) {
    handle.destroy();
    throw error;
  }
}

async function enterFlyAtOpeningPose(handle) {
  const app = handle.app;
  const autoRenderBeforeOpening = app.autoRender;
  // The Viewer resolves loaded on its first complete frame and then switches to
  // on-demand rendering. The visitor model transform is applied after that
  // frame, so keep rendering through the transform and Fly spawn reset before
  // hiding the loading curtain. This lets PlayCanvas refresh the transformed
  // splat bounds and depth order before the opening composition is shown.
  app.autoRender = true;
  handle.state.gamingControls = false;
  handle.state.cameraMode = 'fly';
  await nextFrame();
  await nextFrame();
  handle.resetCamera();
  try {
    await delay(1_300);
    handle.state.cameraMode = 'fly';
    app.renderNextFrame = true;
    await waitForFrames(app, 2, 5_000);
    flyLocked = true;
  } finally {
    app.renderNextFrame = true;
    app.autoRender = autoRenderBeforeOpening;
  }
}

function installButtons(handle) {
  handle.events.on('cameraMode:changed', (mode) => {
    if (!flyLocked) return;
    if (mode !== 'fly') handle.state.cameraMode = 'fly';
  });
  resetButton.addEventListener('click', () => {
    handle.state.inputEnabled = true;
    handle.state.cameraMode = 'fly';
    handle.resetCamera();
    focusViewer();
  });
  helpButton.addEventListener('click', () => {
    const open = helpPanel.hidden;
    helpPanel.hidden = !open;
    helpButton.setAttribute('aria-expanded', String(open));
    focusViewer();
  });
  languageSelect.addEventListener('change', () => applyLanguage(languageSelect.value, true));
}

async function installNavigationDiagnostics(handle, settings) {
  if (new URLSearchParams(location.search).get('navigationDiagnostics') !== '1') return;
  const panel = document.createElement('aside');
  panel.className = 'navigation-diagnostic-panel';
  panel.setAttribute('aria-label', 'Technical navigation overlay');
  panel.innerHTML = `<h2>Navigation proposal overlay</h2>
    <p data-diagnostic-status>Loading review-only lines…</p>
    <div class="navigation-diagnostic-actions">
      <button type="button" data-nav-frame="perspective">Frame boundary</button>
      <button type="button" data-nav-frame="top-down">Top down</button>
      <button type="button" data-nav-frame="reset">Reset opening view</button>
    </div>
    <p class="navigation-diagnostic-hint">Framing changes only this review camera. Reset restores the Visitor opening pose.</p>
    <p data-diagnostic-view-status>Opening Visitor view.</p>
    <label><input type="checkbox" data-nav-layer="cameras" checked> Camera poses and directions</label>
    <label><input type="checkbox" data-nav-layer="path" checked> Ordered camera path</label>
    <label><input type="checkbox" data-nav-layer="gaps" checked> Inferred bridge and open endpoints</label>
    <label><input type="checkbox" data-nav-layer="v1" checked> Existing v1 boundary</label>
    <label><input type="checkbox" data-nav-layer="v2" checked> Candidate v2 boundary</label>
    <label><input type="checkbox" data-nav-layer="ground"> Photo-SfM lower candidate</label>
    <p data-diagnostic-hover>Hover a camera marker for its image name and track count.</p>
    <p data-diagnostic-projection>Overlay projection will be reported after the camera is ready.</p>
    <p>Review only. This overlay does not enforce navigation. Visitor movement remains unbounded until a boundary is explicitly approved.</p>`;
  host.append(panel);
  try {
    const overlayUrl = new URL('../diagnostics/navigation-overlay.json', location.href);
    const response = await fetch(overlayUrl, { cache: 'no-cache' });
    if (!response.ok) throw new Error(`Navigation overlay unavailable (${response.status}).`);
    const data = await response.json();
    if (data.schema_version !== 'exhibition-archive.navigation-model-overlay.v1') {
      throw new Error('Unsupported navigation overlay data.');
    }
    const status = panel.querySelector('[data-diagnostic-status]');
    const viewStatus = panel.querySelector('[data-diagnostic-view-status]');
    const projectionStatus = panel.querySelector('[data-diagnostic-projection]');
    const summary = data.summary ?? {};
    status.textContent = `${data.proposal_status} · ${summary.camera_used_count}/${summary.registered_camera_count} cameras · review required`;
    const openingPose = settings?.cameras?.[0]?.initial
      ? JSON.parse(JSON.stringify(settings.cameras[0].initial)) : null;
    const navigationPoints = [
      ...(data.candidate_v2 ?? []).flatMap((region) => region.lower_loop ?? []),
      ...(data.cameras ?? []).map((cameraRow) => cameraRow.center)
    ].filter((point) => Array.isArray(point) && point.length === 3 && point.every(Number.isFinite));
    const frameNavigation = (preset) => {
      if (!Array.isArray(handle.annotations) || (preset === 'reset' ? !openingPose : !navigationPoints.length)) {
        viewStatus.textContent = 'Navigation camera framing is unavailable for this proposal.';
        return;
      }
      let pose = openingPose;
      if (preset !== 'reset') {
        const min = [0, 1, 2].map((axis) => Math.min(...navigationPoints.map((point) => point[axis])));
        const max = [0, 1, 2].map((axis) => Math.max(...navigationPoints.map((point) => point[axis])));
        const target = min.map((value, axis) => (value + max[axis]) * 0.5);
        const radius = Math.max(0.001, ...navigationPoints.map((point) =>
          Math.hypot(point[0] - target[0], point[2] - target[2])));
        const fov = 70;
        const distance = Math.max(radius, (max[1] - min[1]) * 0.5) / Math.tan(fov * Math.PI / 360) * 1.2;
        const position = preset === 'top-down'
          ? [target[0] + distance * 0.06, target[1] + distance, target[2] + distance * 0.06]
          : [target[0] + distance * 0.612, target[1] + distance * 0.5, target[2] + distance * 0.612];
        pose = { position, target, fov };
      }
      const annotationIndex = handle.annotations.push({
        position: pose.position,
        title: preset === 'top-down' ? 'Navigation top-down review' : 'Navigation boundary review',
        text: 'Temporary technical-review camera preset.',
        camera: { initial: pose }
      }) - 1;
      flyLocked = false;
      handle.selectAnnotation(annotationIndex);
      viewStatus.textContent = preset === 'reset'
        ? 'Restoring the Visitor opening view.'
        : preset === 'top-down'
        ? 'Top-down navigation review view · proposal only.'
        : 'Framed navigation review view · proposal only.';
      window.setTimeout(() => {
        if (!handle.state.loaded) return;
        handle.state.cameraMode = 'fly';
        handle.selectAnnotation(null);
        flyLocked = true;
        handle.app.renderNextFrame = true;
        if (preset === 'reset') viewStatus.textContent = 'Opening Visitor view restored.';
      }, 1_350);
    };
    for (const button of panel.querySelectorAll('[data-nav-frame]')) {
      button.addEventListener('click', () => frameNavigation(button.dataset.navFrame));
    }
    const canvas = document.createElement('canvas');
    canvas.className = 'navigation-diagnostic-canvas';
    canvas.setAttribute('aria-hidden', 'true');
    host.append(canvas);
    const context = canvas.getContext('2d');
    const cameraComponent = handle.app.root.findComponents('camera')[0];
    const camera = cameraComponent?.camera;
    const Vector = cameraComponent?.entity?.getPosition()?.constructor;
    if (!camera?.worldToScreen || !Vector) throw new Error('Viewer camera projection is unavailable.');
    const visible = Object.fromEntries([...panel.querySelectorAll('[data-nav-layer]')]
      .map((input) => [input.dataset.navLayer, input]));
    let screenCameras = [];
    const project = (position, width, height) => {
      const world = new Vector(position[0], position[1], position[2]);
      const screen = new Vector();
      camera.worldToScreen(world, width, height, screen);
      if (![screen.x, screen.y, screen.z].every(Number.isFinite)) return null;
      return { x: screen.x, y: screen.y, depth: screen.z,
        onscreen: screen.z >= camera.nearClip && screen.z <= camera.farClip &&
          screen.x >= 0 && screen.x <= width && screen.y >= 0 && screen.y <= height };
    };
    const drawLine = (ctx, a, b, color, width, dashed, viewport) => {
      const first = project(a, viewport.width, viewport.height);
      const second = project(b, viewport.width, viewport.height);
      if (!first || !second || first.depth < camera.nearClip || first.depth > camera.farClip ||
          second.depth < camera.nearClip || second.depth > camera.farClip) return;
      projectedLineCount++;
      ctx.strokeStyle = color;
      ctx.lineWidth = width;
      ctx.setLineDash(dashed ? [7, 5] : []);
      ctx.beginPath();
      ctx.moveTo(first.x, first.y);
      ctx.lineTo(second.x, second.y);
      ctx.stroke();
      ctx.setLineDash([]);
    };
    const drawLoop = (ctx, points, color, width, dashed, viewport) => {
      for (let index = 0; index < points.length; index++) {
        drawLine(ctx, points[index], points[(index + 1) % points.length], color, width, dashed, viewport);
      }
    };
    let projectedLineCount = 0;
    const drawOverlay = () => {
      projectedLineCount = 0;
      const rect = host.getBoundingClientRect();
      const ratio = window.devicePixelRatio || 1;
      canvas.width = Math.max(1, Math.round(rect.width * ratio));
      canvas.height = Math.max(1, Math.round(rect.height * ratio));
      canvas.style.width = `${rect.width}px`;
      canvas.style.height = `${rect.height}px`;
      context.setTransform(ratio, 0, 0, ratio, 0, 0);
      context.clearRect(0, 0, rect.width, rect.height);
      const viewport = { width: rect.width, height: rect.height };
      if (visible.v1.checked) for (const region of data.baseline_v1 ?? []) {
        drawLoop(context, region.loop ?? [], '#e4bd54', 2, true, viewport);
      }
      if (visible.ground.checked) for (const region of data.candidate_v2 ?? []) {
        drawLoop(context, region.ground_loop ?? [], '#6fe28a', 2, false, viewport);
      }
      if (visible.v2.checked) for (const region of data.candidate_v2 ?? []) {
        if (region.boundary_segments?.length) {
          for (const edge of region.boundary_segments) {
            const supported = edge.classification === 'CAMERA_POSITION_SUPPORTED_ENVELOPE';
            drawLine(context, edge.start, edge.end, supported ? '#58ded2' : '#ed62cc',
              2.5, !supported, viewport);
          }
        } else {
          drawLoop(context, region.lower_loop ?? [], '#ed62cc', 2.5, false, viewport);
        }
        drawLoop(context, region.upper_loop ?? [], '#b96bd4', 1, false, viewport);
      }
      if (visible.path.checked) for (const segment of data.observed_path_segments ?? []) {
        drawLine(context, segment.from, segment.to, segment.inferred ? '#ffe06b' : '#b6c3cc',
          segment.inferred ? 3 : 1.5, segment.inferred, viewport);
      }
      if (visible.gaps.checked && data.inferred_endpoint_gap) {
        const gap = data.inferred_endpoint_gap;
        drawLine(context, gap.from, gap.to, '#fa806d', 2, true, viewport);
      }
      screenCameras = [];
      const projectedCameraSamples = [];
      if (visible.cameras.checked) for (const item of data.cameras ?? []) {
        const oldExcluded = item.baseline_coverage_status === 'EXCLUDED_INSUFFICIENT_GEOMETRY';
        const color = oldExcluded ? '#ff9d62' : '#73caff';
        drawLine(context, item.center, item.forward_end, color, 1, false, viewport);
        const point = project(item.center, viewport.width, viewport.height);
        if (point) projectedCameraSamples.push(point);
        if (!point || !point.onscreen) continue;
        context.fillStyle = color;
        context.beginPath();
        context.arc(point.x, point.y, 4, 0, Math.PI * 2);
        context.fill();
        screenCameras.push({ ...point, item });
      }
      const extent = (axis) => projectedCameraSamples.length
        ? `${Math.min(...projectedCameraSamples.map((point) => point[axis])).toFixed(1)}…${Math.max(...projectedCameraSamples.map((point) => point[axis])).toFixed(1)}`
        : 'n/a';
      projectionStatus.textContent = `Overlay projection: ${screenCameras.length}/${data.cameras?.length ?? 0} cameras in view · ${projectedLineCount} segments · screen x ${extent('x')}, y ${extent('y')}, depth ${extent('depth')}.`;
    };
    for (const input of Object.values(visible)) input.addEventListener('change', drawOverlay);
    host.addEventListener('pointermove', (event) => {
      const rect = host.getBoundingClientRect();
      const x = event.clientX - rect.left, y = event.clientY - rect.top;
      const closest = screenCameras.map((item) => ({ item, distance: Math.hypot(item.x - x, item.y - y) }))
        .filter((row) => row.distance < 12).sort((a, b) => a.distance - b.distance)[0];
      const hover = panel.querySelector('[data-diagnostic-hover]');
      hover.textContent = closest
        ? `${closest.item.item.image_name} · ${closest.item.item.triangulated_observation_count ?? 'n/a'} triangulated observations · v1 ${closest.item.item.baseline_coverage_status ?? 'not classified'}`
        : 'Hover a camera marker for its image name and track count.';
    });
    handle.app.on('frameend', drawOverlay);
    new ResizeObserver(drawOverlay).observe(host);
    drawOverlay();
  } catch (error) {
    panel.querySelector('[data-diagnostic-status]').textContent = `Overlay error: ${error.message}`;
    console.error(error);
  }
}

function exposeAcceptance(handle) {
  if (!__EXHIBITION_ARCHIVE_VISITOR_ACCEPTANCE__ || new URLSearchParams(location.search).get('acceptance') !== '1') return;
  let acceptanceUpdates = 0;
  let acceptanceFrames = 0;
  handle.app.on('update', () => { acceptanceUpdates++; });
  handle.app.on('frameend', () => { acceptanceFrames++; });
  Object.defineProperty(window, '__phase4dAcceptance', {
    configurable: true,
    value: Object.freeze({
      get state() {
        const camera = handle.app.root.findComponents('camera')[0]?.entity;
        const position = camera?.getPosition();
        const euler = camera?.getEulerAngles();
        const canvas = host.querySelector('canvas');
        return {
          loaded: handle.state.loaded,
          appAutoRender: handle.app.autoRender,
          appUpdates: acceptanceUpdates,
          appFrames: acceptanceFrames,
          renderNextFrame: handle.app.renderNextFrame,
          cameraMode: handle.state.cameraMode,
          walkAllowed: handle.state.walkAllowed,
          hasCollision: handle.state.hasCollision,
          gamingControls: handle.state.gamingControls,
          inputEnabled: handle.state.inputEnabled,
          visibilityState: document.visibilityState,
          documentHidden: document.hidden,
          pointerLocked: document.pointerLockElement === canvas,
          documentFocused: document.hasFocus(),
          canvasConnected: Boolean(canvas?.isConnected),
          canvasInActiveDocument: canvas?.ownerDocument === document,
          pointerLockErrorCount: window.__visitorPointerLockErrors ?? 0,
          activeElement: `${document.activeElement?.tagName ?? ''}${document.activeElement?.id ? `#${document.activeElement.id}` : ''}`,
          renderer: handle.app.graphicsDevice.deviceType,
          position: position ? [position.x, position.y, position.z] : null,
          euler: euler ? [euler.x, euler.y, euler.z] : null,
          forward: camera?.forward ? [camera.forward.x, camera.forward.y, camera.forward.z] : null,
          right: camera?.right ? [camera.right.x, camera.right.y, camera.right.z] : null,
          up: camera?.up ? [camera.up.x, camera.up.y, camera.up.z] : null,
          activeFormat: metadata.active_format,
          language: currentLanguage,
          loadDurationMs: now() - startedAt,
          modelTransform: metadata.initial_camera.model_scene_transform_supersplat,
          collision: metadata.collision
        };
      },
      move(x, z) { handle.setMoveInput(x, z); },
      stop() { handle.setMoveInput(0, 0); },
      async setPose(index) {
        const annotationIndex = testPoseAnnotations[index];
        if (!Number.isInteger(annotationIndex)) throw new Error('Acceptance camera index is invalid.');
        flyLocked = false;
        handle.selectAnnotation(annotationIndex);
        await delay(1_300);
        handle.state.cameraMode = 'fly';
        await nextFrame();
        flyLocked = true;
      }
    })
  });
}

async function start() {
  installVisitorInput();
  if (__EXHIBITION_ARCHIVE_VISITOR_ACCEPTANCE__ && new URLSearchParams(location.search).get('acceptance') === '1') {
    window.addEventListener('pointerlockerror', () => {
      window.__visitorPointerLockErrors = (window.__visitorPointerLockErrors ?? 0) + 1;
    });
  }
  try {
    const packageBase = new URL('../', import.meta.url);
    const [metadataResponse, settingsResponse, localeResponse] = await Promise.all([
      fetch(new URL('config/exhibition.json', packageBase), { cache: 'no-cache' }),
      fetch(new URL('config/settings.json', packageBase), { cache: 'no-cache' }),
      fetch(new URL('config/locales.json', packageBase), { cache: 'no-cache' })
    ]);
    if (!metadataResponse.ok || !settingsResponse.ok || !localeResponse.ok) throw new Error('The visitor package is incomplete.');
    metadata = await metadataResponse.json();
    const settings = await settingsResponse.json();
    window.__visitorLocales = await localeResponse.json();
    strings = window.__visitorLocales;
    applyLanguage(chooseLanguage(metadata.default_language));
    applyTestSettings(settings);

    const query = new URLSearchParams(location.search);
    const requested = query.get('format');
    const comparisonPlyRequested = __EXHIBITION_ARCHIVE_VISITOR_ACCEPTANCE__ && query.has('comparisonPlyUrl');
    const primary = requested === 'ply' && (metadata.models.ply.available || comparisonPlyRequested) ? 'ply' : metadata.primary_format;
    const choices = primary === 'sog' && metadata.models.ply.available ? ['sog', 'ply'] : [primary];
    let lastError = null;
    for (const format of choices) {
      try {
        if (format === 'ply' && primary === 'sog') loadingMessage.textContent = strings[currentLanguage].fallbackLoading;
        viewer = await openModel(format, settings);
        metadata.active_format = format;
        break;
      } catch (error) {
        lastError = error;
      }
    }
    if (!viewer) throw lastError ?? new Error('The exhibition model could not be opened.');
    await enterFlyAtOpeningPose(viewer);
    installButtons(viewer);
    await installNavigationDiagnostics(viewer, settings);
    loading.hidden = true;
    focusViewer();
    exposeAcceptance(viewer);
  } catch (error) {
    loadingMessage.textContent = strings?.[currentLanguage]?.error ?? 'This exhibition could not be opened. Please try again later.';
    loading.classList.add('load-error');
    console.error(error);
  }
}

void start();
