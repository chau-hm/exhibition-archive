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
  const model = metadata.models[format];
  const response = await fetch(model.url, { cache: 'force-cache' });
  if (!response.ok) throw new Error(`${format.toUpperCase()} ${strings[currentLanguage].assetError} (${response.status}).`);
  const handle = await createViewer({
    container: host,
    settings,
    contentUrl: model.url,
    contentFilename: model.filename,
    collisionUrl: (__EXHIBITION_ARCHIVE_VISITOR_ACCEPTANCE__ && new URLSearchParams(location.search).get('collision') === 'off')
      ? undefined : metadata.collision.url,
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

    const requested = new URLSearchParams(location.search).get('format');
    const primary = requested === 'ply' ? 'ply' : metadata.primary_format;
    const choices = primary === 'sog' ? ['sog', 'ply'] : ['ply'];
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
