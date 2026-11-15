import {
  configuredResetFailureReasons,
  inspectConfiguredSpawn,
  waitForConfiguredSpawn
} from './camera-readiness.mjs?build=__VISITOR_BUILD_ID__';
import { computeRecenterGroundHeight, computeRecenterRigPosition, configuredSafeXrSpawnPoint, inspectTrackedViewpoints, intersectLocalQuadRay, nextBoundaryFeedbackMode, sampleXrViewpoints, xrTargetRayDirection } from './xr-boundary.mjs?build=__VISITOR_BUILD_ID__';
import { disableXrLocomotion, installXrNavigationGuard } from './xr-navigation-guard.mjs?build=__VISITOR_BUILD_ID__';
import { createCollisionGuard, parseCollisionGlb } from './collision-geometry.mjs?build=__VISITOR_BUILD_ID__';

const diagnosticSchema = 'exhibition-archive.visitor.xr.v1';
const buildId = new URL(import.meta.url).searchParams.get('build') ??
  document.querySelector('meta[name="visitor-build-id"]')?.content ?? 'unversioned';
const traceStorageKey = 'exhibition-archive.visitor.xr.trace.v1';
const traceLimit = 800;
const traceSampleLimit = 300;
let rendererPlanPromise;
let persistentTrace = [];
let buildIntegrity = null;

try {
  const stored = JSON.parse(localStorage.getItem(traceStorageKey) ?? '[]');
  if (Array.isArray(stored)) persistentTrace = stored.slice(-traceLimit);
} catch {
  // Keep diagnostics available in memory when storage is unavailable.
}

function persistTrace(record) {
  persistentTrace.push(record);
  const removeOldestSample = () => {
    const sampleIndex = persistentTrace.findIndex(item => item.event === 'lifecycle_frame_sample');
    if (sampleIndex < 0) return false;
    persistentTrace.splice(sampleIndex, 1);
    return true;
  };
  let sampleCount = persistentTrace.reduce(
    (count, item) => count + Number(item.event === 'lifecycle_frame_sample'),
    0
  );
  while (sampleCount > traceSampleLimit && removeOldestSample()) sampleCount--;
  while (persistentTrace.length > traceLimit) {
    if (!removeOldestSample()) persistentTrace.shift();
  }
  try {
    localStorage.setItem(traceStorageKey, JSON.stringify(persistentTrace));
  } catch (error) {
    if (!persistTrace.warned) {
      persistTrace.warned = true;
      console.warn('[visitor-xr] persistent trace storage unavailable', describeError(error));
    }
  }
}

function describeError(error) {
  return {
    name: error?.name ?? 'Error',
    message: String(error?.message ?? error)
  };
}

function instrumentWebXRRequestSession(handle) {
  const xr = navigator.xr;
  if (!xr || typeof xr.requestSession !== 'function') {
    logXr('request_session_probe_unavailable', { reason: 'navigator.xr.requestSession is unavailable' }, handle);
    return false;
  }
  if (handle.__visitorXrRequestProbe) return true;

  const original = xr.requestSession;
  const descriptor = Object.getOwnPropertyDescriptor(xr, 'requestSession');
  let requestNumber = 0;
  const wrapped = function (...args) {
    const requestId = ++requestNumber;
    const [mode, init = {}] = args;
    logXr('webxr_request_session_called', {
      requestId,
      mode: mode ?? null,
      requiredFeatures: init.requiredFeatures ?? [],
      optionalFeatures: init.optionalFeatures ?? [],
      hasDomOverlayRoot: Boolean(init.domOverlay?.root),
      activationState: navigator.userActivation?.isActive ?? null
    }, handle);

    let request;
    try {
      request = Reflect.apply(original, xr, args);
    } catch (error) {
      logXr('webxr_request_session_threw', {
        requestId,
        mode: mode ?? null,
        error: describeError(error)
      }, handle);
      throw error;
    }
    return Promise.resolve(request).then(session => {
      const sessionSummary = {
        mode: session?.mode ?? mode ?? null,
        visibilityState: session?.visibilityState ?? null,
        enabledFeatures: [...(session?.enabledFeatures ?? [])]
      };
      logXr('webxr_request_session_resolved', { requestId, session: sessionSummary }, handle);
      session?.addEventListener?.('end', () => {
        logXr('webxr_session_end_event', { requestId, session: sessionSummary }, handle);
      }, { once: true });
      return session;
    }, error => {
      logXr('webxr_request_session_rejected', {
        requestId,
        mode: mode ?? null,
        requiredFeatures: init.requiredFeatures ?? [],
        optionalFeatures: init.optionalFeatures ?? [],
        error: describeError(error)
      }, handle);
      throw error;
    });
  };

  try {
    Object.defineProperty(xr, 'requestSession', {
      configurable: true,
      enumerable: descriptor?.enumerable ?? false,
      writable: true,
      value: wrapped
    });
    if (xr.requestSession !== wrapped) throw new Error('The browser did not retain the diagnostic requestSession wrapper.');
    handle.__visitorXrRequestProbe = { xr, original, wrapped, descriptor };
    logXr('request_session_probe_installed', {
      method: 'navigator.xr.requestSession',
      forwardsToOriginal: true,
      restoresOriginalDescriptor: Boolean(descriptor)
    }, handle);
    return true;
  } catch (error) {
    logXr('request_session_probe_unavailable', {
      reason: 'The browser did not allow transparent instrumentation of requestSession',
      error: describeError(error)
    }, handle);
    return false;
  }
}

function logXr(event, details = {}, handle = null) {
  const state = handle?.state;
  const device = handle?.app?.graphicsDevice;
  const renderer = device?.isWebGPU ? 'webgpu' : device?.isWebGL2 ? 'webgl2' : null;
  const record = {
    schema: diagnosticSchema,
    buildId,
    event,
    timestamp: new Date().toISOString(),
    monotonicMs: Number(performance.now().toFixed(3)),
    pageVisibility: document.visibilityState,
    pageHasFocus: document.hasFocus?.() ?? null,
    renderer,
    deviceType: device?.deviceType ?? null,
    webgpuXRBindingAvailable: typeof globalThis.XRGPUBinding !== 'undefined',
    hasVR: state?.hasVR ?? null,
    canStartVR: state?.canStartVR ?? null,
    xrMode: state?.xrMode ?? null,
    ...details
  };
  console.info('[visitor-xr]', JSON.stringify(record));
  persistTrace(record);
}

for (const [target, eventName] of [
  [document, 'visibilitychange'],
  [window, 'pagehide'],
  [window, 'pageshow'],
  [window, 'focus'],
  [window, 'blur']
]) {
  target.addEventListener(eventName, event => {
    logXr(`browser_${eventName}`, {
      visibilityState: document.visibilityState,
      focused: document.hasFocus?.() ?? null,
      persistedPage: event.persisted ?? null,
      xrMode: window.__visitorXRDebug?.snapshot?.()?.rendering?.xrMode ?? null
    });
  });
}

function sha256Hex(buffer) {
  return crypto.subtle.digest('SHA-256', buffer).then(hash =>
    Array.from(new Uint8Array(hash), value => value.toString(16).padStart(2, '0')).join('')
  );
}

async function verifyBuildResources() {
  const buildUrl = new URL('../../config/build.json', import.meta.url);
  const manifestResponse = await fetch(buildUrl, { cache: 'no-store' });
  if (!manifestResponse.ok) throw new Error(`Build manifest request failed (${manifestResponse.status}).`);
  const manifest = await manifestResponse.json();
  const resourceUrls = {
    'index.html': new URL('../../index.html', import.meta.url),
    'config/exhibition.json': new URL('../../config/exhibition.json', import.meta.url),
    'config/settings.json': new URL('../../config/settings.json', import.meta.url),
    'config/locales.json': new URL('../../config/locales.json', import.meta.url),
    'js/viewer.js': new URL('../viewer.js', import.meta.url),
    'js/xr/xr-controls.js': new URL('./xr-controls.js', import.meta.url),
    'js/xr/camera-readiness.mjs': new URL('./camera-readiness.mjs', import.meta.url),
    'js/xr/xr-boundary.mjs': new URL('./xr-boundary.mjs', import.meta.url),
    'js/xr/xr-navigation-guard.mjs': new URL('./xr-navigation-guard.mjs', import.meta.url),
    'js/xr/collision-geometry.mjs': new URL('./collision-geometry.mjs', import.meta.url),
    'vendor/supersplat-viewer.js': new URL('../../vendor/supersplat-viewer.js', import.meta.url),
    'vendor/supersplat-viewer.css': new URL('../../vendor/supersplat-viewer.css', import.meta.url),
    'css/viewer.css': new URL('../../css/viewer.css', import.meta.url),
    'vendor/runtime.json': new URL('../../vendor/runtime.json', import.meta.url)
  };
  const loadedModuleHashes = {};
  for (const [path, url] of Object.entries(resourceUrls)) {
    url.searchParams.set('build', buildId);
    const response = await fetch(url, { cache: 'no-store' });
    if (!response.ok) throw new Error(`${path} hash request failed (${response.status}).`);
    const bytes = await response.arrayBuffer();
    const actual = await sha256Hex(bytes);
    const expected = manifest.resources?.[path] ?? null;
    loadedModuleHashes[path] = {
      url: url.href,
      sha256: actual,
      expectedSha256: expected,
      matchesExpected: expected === null ? null : actual === expected,
      sizeBytes: bytes.byteLength
    };
  }
  const buildMatches = manifest.buildId === buildId;
  const hashesMatch = Object.values(loadedModuleHashes).every(item => item.matchesExpected === true);
  logXr(buildMatches && hashesMatch ? 'build_integrity_verified' : 'build_integrity_mismatch', {
    manifestBuildId: manifest.buildId ?? null,
    buildMatches,
    hashesMatch,
    loadedModuleHashes,
    versionedImports: true,
    serviceWorkerController: navigator.serviceWorker?.controller?.scriptURL ?? null
  });
  return { buildId, buildMatches, hashesMatch, loadedModuleHashes };
}

export function prepareXRRenderer(configuration = {}) {
  if (!rendererPlanPromise) {
    rendererPlanPromise = (async () => {
      try {
        buildIntegrity = await verifyBuildResources();
      } catch (error) {
        logXr('build_integrity_error', { error: describeError(error), buildId });
      }
      const xr = navigator.xr;
      const xrApiAvailable = typeof xr?.isSessionSupported === 'function';
      let immersiveVrSupported = false;
      let supportCheckError = null;

      if (xrApiAvailable) {
        try {
          immersiveVrSupported = await xr.isSessionSupported('immersive-vr');
        } catch (error) {
          supportCheckError = describeError(error);
          logXr('capability_error', { stage: 'preflight', error: supportCheckError });
        }
      }

      const webgpuXRBindingAvailable = typeof globalThis.XRGPUBinding !== 'undefined';
      const rendererPreference = configuration.renderer ?? 'auto';
      const useWebglForXr = rendererPreference === 'webgl' ||
        (rendererPreference === 'auto' && immersiveVrSupported && !webgpuXRBindingAvailable);
      const renderer = useWebglForXr ? 'webgl' : 'webgpu';
      const plan = {
        renderer,
        rendererPreference,
        buildIntegrityValid: Boolean(buildIntegrity?.buildMatches && buildIntegrity?.hashesMatch),
        xrApiAvailable,
        immersiveVrSupported,
        webgpuXRBindingAvailable,
        reason: useWebglForXr
          ? 'immersive-vr is supported, but WebGPU XR binding is unavailable'
          : 'keep the existing WebGPU renderer'
      };

      logXr('capability_preflight', { ...plan, supportCheckError });
      return plan;
    })();
  }

  return rendererPlanPromise;
}

function readVector(value) {
  if (!value) return null;
  return [Number(value.x), Number(value.y), Number(value.z)];
}

function readQuaternion(value) {
  if (!value) return null;
  return [Number(value.x), Number(value.y), Number(value.z), Number(value.w)];
}

function entityTransform(entity) {
  if (!entity) return null;
  const position = entity.getPosition?.();
  const rotation = entity.getRotation?.();
  const localPosition = entity.getLocalPosition?.();
  const localRotation = entity.getLocalRotation?.();
  const scale = entity.getScale?.();
  const localScale = entity.getLocalScale?.();
  const worldMatrix = entity.getWorldTransform?.()?.data;
  return {
    name: entity.name ?? null,
    local: {
      position: readVector(localPosition),
      rotation: readQuaternion(localRotation),
      scale: readVector(localScale),
      euler: readVector(entity.getLocalEulerAngles?.())
    },
    world: {
      position: readVector(position),
      rotation: readQuaternion(rotation),
      scale: readVector(scale),
      euler: readVector(entity.getEulerAngles?.()),
      matrix: worldMatrix ? Array.from(worldMatrix) : null
    }
  };
}

function findScriptInstance(component, name) {
  if (!component) return null;
  try {
    const result = component.get?.(name);
    if (result) return result;
  } catch {
    // Some ScriptComponent versions throw for scripts that are not registered by name.
  }
  if (component[name]) return component[name];
  const bags = [component.instances, component._scripts];
  for (const bag of bags) {
    if (Array.isArray(bag)) {
      const found = bag.find(instance =>
        instance?.scriptName === name ||
        instance?.constructor?.scriptName === name ||
        instance?.__scriptType?.scriptName === name
      );
      if (found) return found;
    } else if (bag && typeof bag === 'object') {
      if (bag[name]) return bag[name];
      for (const instance of Object.values(bag)) {
        if (
          instance?.scriptName === name ||
          instance?.constructor?.scriptName === name ||
          instance?.__scriptType?.scriptName === name
        ) return instance;
      }
    }
  }
  return null;
}

function cameraAndRig(handle) {
  const cameras = handle.app.root.findComponents('camera');
  const cameraEntity = cameras?.[0]?.entity ?? null;
  return { cameraEntity, rigEntity: cameraEntity?.parent ?? null };
}

function cameraPoseSignature(entity) {
  if (!entity) return null;
  return {
    position: readVector(entity.getPosition?.()),
    rotation: readQuaternion(entity.getRotation?.()),
    fov: entity.camera?.fov ?? null
  };
}

function sceneTransformInvariant(entity, expected) {
  if (!entity || !expected?.translation || !expected?.rotationQuaternionXYZW || !Number.isFinite(expected.uniformScale)) {
    return { matches: false, reason: 'expected-transform-or-scene-unavailable' };
  }
  const actual = entityTransform(entity)?.local;
  if (!actual?.position || !actual?.rotation || !actual?.scale) {
    return { matches: false, reason: 'scene-transform-unavailable' };
  }
  const expectedScale = [expected.uniformScale, expected.uniformScale, expected.uniformScale];
  const positionError = Math.hypot(...actual.position.map((value, index) => value - expected.translation[index]));
  const scaleError = Math.max(...actual.scale.map((value, index) => Math.abs(value - expectedScale[index])));
  const rotationAlignment = Math.abs(actual.rotation.reduce(
    (sum, value, index) => sum + value * expected.rotationQuaternionXYZW[index], 0
  ));
  return {
    matches: positionError <= 1e-6 && scaleError <= 1e-6 && rotationAlignment >= 1 - 1e-8,
    positionError,
    scaleError,
    quaternionAlignmentError: 1 - rotationAlignment,
    tolerance: { position: 1e-6, scale: 1e-6, quaternionAlignment: 1e-8 }
  };
}

function cameraLocalPoseSignature(entity) {
  if (!entity) return null;
  return {
    position: readVector(entity.getLocalPosition?.()),
    rotation: readQuaternion(entity.getLocalRotation?.())
  };
}

function poseDifference(a, b) {
  if (!a || !b || !a.position || !b.position || !a.rotation || !b.rotation) return Infinity;
  const position = Math.hypot(
    a.position[0] - b.position[0],
    a.position[1] - b.position[1],
    a.position[2] - b.position[2]
  );
  const dot = Math.abs(
    a.rotation[0] * b.rotation[0] +
    a.rotation[1] * b.rotation[1] +
    a.rotation[2] * b.rotation[2] +
    a.rotation[3] * b.rotation[3]
  );
  const angle = 2 * Math.acos(Math.max(-1, Math.min(1, dot)));
  return { position, angle };
}

function readCameraManagerPose(handle, cameraEntity) {
  try {
    if (typeof handle.getCameraManagerState === 'function') {
      return {
        available: true,
        source: 'Visitor-local Viewer 1.42.0 CameraManager diagnostic accessor',
        value: handle.getCameraManagerState()
      };
    }
    if (typeof handle.getCameraPose === 'function') {
      return { available: true, source: 'Viewer handle getCameraPose()', value: handle.getCameraPose() };
    }
    if (typeof window.getCameraState === 'function') {
      return { available: true, source: 'Viewer debug getCameraState()', value: window.getCameraState() };
    }
  } catch (error) {
    return { available: false, error: describeError(error) };
  }
  return {
    available: false,
    source: 'Viewer 1.42.0 does not expose CameraManager pose on the public handle',
    synchronizedCameraProxy: cameraPoseSignature(cameraEntity)
  };
}

function renderingState(handle, cameraEntity, entryCanvasParent) {
  const canvas = handle.app.graphicsDevice.canvas;
  const device = handle.app.graphicsDevice;
  const xr = handle.app.xr;
  const frameStats = handle.app.stats?.frame ?? {};
  const renderCounters = Object.fromEntries(
    Object.entries(frameStats)
      .filter(([, value]) => typeof value === 'number' && Number.isFinite(value))
      .slice(0, 40)
  );
  let canvasPresentation = null;
  if (canvas) {
    const rect = canvas.getBoundingClientRect();
    const style = getComputedStyle(canvas);
    canvasPresentation = {
      pixelSize: { width: canvas.width, height: canvas.height },
      cssRect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
      display: style.display,
      visibility: style.visibility,
      opacity: style.opacity,
      filter: style.filter,
      transform: style.transform,
      parentClientRect: canvas.parentElement ? (() => {
        const parentRect = canvas.parentElement.getBoundingClientRect();
        return { x: parentRect.x, y: parentRect.y, width: parentRect.width, height: parentRect.height };
      })() : null
    };
  }
  const gsplatEntities = handle.app.root.findComponents('gsplat').map(component => ({
    entity: component.entity?.name ?? null,
    enabled: component.enabled ?? null,
    visible: component.entity?.enabledInHierarchy ?? component.entity?.enabled ?? null,
    splatCount: component.instance?.resource?.numSplats ?? component.resource?.numSplats ?? null
  }));
  return {
    autoRender: handle.app.autoRender,
    renderNextFrame: handle.app.renderNextFrame,
    xrActive: Boolean(handle.app.xr?.active),
    xrMode: handle.state.xrMode ?? null,
    xrSession: {
      type: xr?.type ?? null,
      visibilityState: xr?.session?.visibilityState ?? null,
      inputSourceCount: xr?.session?.inputSources?.length ?? null,
      referenceSpaceType: xr?.referenceSpaceType ?? null
    },
    canvasConnected: Boolean(canvas?.isConnected),
    canvasParent: canvas?.parentElement?.id || canvas?.parentElement?.className || canvas?.parentElement?.tagName || null,
    canvasReturnedToEntryParent: !entryCanvasParent || canvas?.parentElement === entryCanvasParent,
    canvasPresentation,
    frameEndCount: handle.__visitorFrameStats?.count ?? 0,
    lastFrameEndMonotonicMs: handle.__visitorFrameStats?.lastMonotonicMs ?? null,
    renderCounters,
    gsplatEntities,
    graphicsDevice: {
      deviceType: device?.deviceType ?? null,
      isWebGL2: Boolean(device?.isWebGL2),
      isWebGPU: Boolean(device?.isWebGPU),
      contextLost: device?.contextLost ?? device?.gl?.isContextLost?.() ?? null,
      destroyed: device?.destroyed ?? null,
      width: device?.width ?? null,
      height: device?.height ?? null
    },
    cameraNearClip: cameraEntity?.camera?.nearClip ?? null,
    cameraFarClip: cameraEntity?.camera?.farClip ?? null,
    cameraFov: cameraEntity?.camera?.fov ?? null
  };
}

function diagnosticSnapshot(handle, resetSpawn, lifecyclePhase, entryCanvasParent, transition = {}) {
  const { cameraEntity, rigEntity } = cameraAndRig(handle);
  const sceneEntity = handle.app.root.findComponents('gsplat')?.[0]?.entity ?? null;
  const guard = handle.__visitorNavigationGuard?.guard ?? null;
  const cameraPosition = cameraPoseSignature(cameraEntity)?.position ?? null;
  const rigPosition = readVector(rigEntity?.getPosition?.());
  const cameraManager = readCameraManagerPose(handle, cameraEntity);
  const internalTransition = cameraManager.value?.transitionProgress ?? null;
  return {
    lifecyclePhase,
    navigationMode: handle.state.cameraMode ?? null,
    resetSpawn: resetSpawn ?? null,
    cameraManagerPose: cameraManager,
    cameraWorldPose: cameraPoseSignature(cameraEntity),
    cameraTransform: entityTransform(cameraEntity),
    rigTransform: entityTransform(rigEntity),
    sceneTransform: entityTransform(sceneEntity),
    sceneTransformInvariant: sceneTransformInvariant(sceneEntity, handle.__visitorExpectedSceneTransform),
    enclosureContainment: {
      cameraEyeWithinClearance: cameraPosition && guard ? guard.isValid(cameraPosition) : null,
      rigOriginWithinClearance: rigPosition && guard ? guard.isValid(rigPosition) : null,
      artificialMovementPolicy: 'xrNavigation.update and xrNavigation.tryTeleport are swept against the existing enclosure mesh',
      trackedHeadPolicy: 'observed separately; never rewritten or clamped'
    },
    cameraTransition: {
      hostPhase: lifecyclePhase,
      hostState: transition.state ?? null,
      lastObservation: transition.observation ?? null,
      viewerTransitionProgress: Number.isFinite(internalTransition) ? internalTransition : null,
      targetPose: cameraManager.value?.targetPose ?? null
    },
    rendering: renderingState(handle, cameraEntity, entryCanvasParent)
  };
}

function waitForFrameEnd(app, timeoutMs = 2000) {
  return new Promise(resolve => {
    let finished = false;
    const finish = value => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      app.off?.('frameend', onFrameEnd);
      resolve(value);
    };
    const onFrameEnd = () => finish(true);
    const timer = setTimeout(() => finish(false), timeoutMs);
    app.on('frameend', onFrameEnd);
  });
}

function nextAnimationFrame() {
  return new Promise(resolve => requestAnimationFrame(() => resolve()));
}

function delay(milliseconds) {
  return new Promise(resolve => setTimeout(resolve, milliseconds));
}

async function waitForCameraSettled(app, cameraEntity, minimumMs = 1050, timeoutMs = 5000, resetSpawn = null) {
  const started = performance.now();
  let previous = null;
  let stableFrames = 0;
  let resetSpawnFrames = 0;
  let frames = 0;
  let lastDifference = null;
  let finalPose = null;
  while (performance.now() - started < timeoutMs) {
    const rendered = await waitForFrameEnd(app, 1500);
    if (!rendered) break;
    frames++;
    const current = cameraPoseSignature(cameraEntity);
    const difference = poseDifference(previous, current);
    lastDifference = difference;
    finalPose = current;
    if (difference && Number.isFinite(difference.position) && Number.isFinite(difference.angle) &&
        difference.position < 1e-3 && difference.angle < 1e-3) {
      stableFrames++;
    } else {
      stableFrames = 0;
    }
    resetSpawnFrames = resetSpawn && matchesResetSpawn(cameraEntity, resetSpawn)
      ? resetSpawnFrames + 1
      : 0;
    previous = current;
    if (performance.now() - started >= minimumMs && resetSpawnFrames >= 3) {
      return {
        settled: true,
        reason: 'configured-reset-spawn',
        frames,
        stableFrames,
        resetSpawnFrames,
        lastDifference,
        finalPose,
        elapsedMs: performance.now() - started
      };
    }
    if (performance.now() - started >= minimumMs && stableFrames >= 3) {
      return {
        settled: true,
        reason: 'stable-camera-pose',
        frames,
        stableFrames,
        resetSpawnFrames,
        lastDifference,
        finalPose,
        elapsedMs: performance.now() - started
      };
    }
  }
  return {
    settled: false,
    frames,
    stableFrames,
    resetSpawnFrames,
    lastDifference,
    finalPose,
    elapsedMs: performance.now() - started
  };
}

function setButtonState(button, pending, phase, canStartVR, state, strings, navigationSafe) {
  const active = state.xrMode === 'vr';
  const label = pending
    ? (pending === 'enter' ? strings.startingVR : strings.endingVR)
    : phase === 'restoring'
      ? (strings.recoveringVR ?? strings.endingVR)
      : active ? strings.exitVR : strings.enterVR;

  button.hidden = !active && (!canStartVR || !navigationSafe || phase !== 'desktop');
  button.disabled = Boolean(pending || (phase !== 'desktop' && phase !== 'in-vr'));
  button.textContent = label ?? (active ? 'Exit VR' : 'Enter VR');
  button.title = active ? (strings.exitVRTitle ?? label) : (strings.enterVRTitle ?? label);
  button.setAttribute('aria-label', button.textContent);
}

async function loadCollisionGeometry(exhibition) {
  if (!exhibition?.collision?.url) throw new Error('The Visitor package has no enclosure collision asset.');
  const response = await fetch(new URL(exhibition.collision.url, document.baseURI), { cache: 'force-cache' });
  if (!response.ok) throw new Error('The enclosure collision asset could not be loaded (' + response.status + ').');
  const mesh = parseCollisionGlb(await response.arrayBuffer());
  const radius = Number(exhibition.collision.camera_radius_viewer_units ?? 0.2);
  const guard = createCollisionGuard(mesh, radius);
  return {
    guard,
    coordinateFrame: exhibition.collision.coordinate_frame ?? 'SuperSplat Y-up world',
    acceptedClearance: exhibition.collision.accepted_edge_clearance_common_units ?? null,
    collisionHash: exhibition.collision.sha256 ?? null
  };
}

function findPalmMenuQuad(app) {
  const menu = app.root.findByName?.('XrPalmMenu');
  if (!menu) return null;
  const visit = entity => {
    const material = entity?.render?.material;
    const texture = material?.emissiveMap ?? material?.opacityMap;
    if (material?.clone && texture?.constructor && entity.render?.meshInstances?.length) {
      return { entity, material, texture, meshInstance: entity.render.meshInstances[0] };
    }
    for (const child of entity?.children ?? []) {
      const found = visit(child);
      if (found) return found;
    }
    return null;
  };
  return visit(menu);
}

function createXrBoundaryFeedback(handle, cameraEntity, locale, configuration = {}) {
  const app = handle.app;
  const template = findPalmMenuQuad(app);
  if (!template || !cameraEntity?.constructor || !cameraEntity.addChild) {
    throw new Error('The pinned Viewer XR Palm Menu render surface is unavailable.');
  }

  const fadeDurationMs = Number.isFinite(configuration.fade_duration_ms)
    ? Math.max(120, configuration.fade_duration_ms)
    : 420;
  const maximumVignetteOpacity = Number.isFinite(configuration.maximum_vignette_opacity)
    ? Math.max(0, Math.min(0.8, configuration.maximum_vignette_opacity))
    : 0.38;

  const textureOptions = (canvas, name) => ({
    name,
    width: canvas.width,
    height: canvas.height,
    format: template.texture.format ?? template.texture._format,
    mipmaps: false,
    addressU: template.texture.addressU,
    addressV: template.texture.addressV,
    minFilter: template.texture.minFilter,
    magFilter: template.texture.magFilter
  });
  const makeCanvas = (width, height) => {
    const canvas = document.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    return canvas;
  };
  const makeQuad = (name, canvas, width, height, distance) => {
    const Texture = template.texture.constructor;
    const texture = new Texture(app.graphicsDevice, textureOptions(canvas, name + ' texture'));
    texture.setSource(canvas);
    texture.upload();
    const material = template.material.clone();
    material.emissiveMap = texture;
    material.opacityMap = texture;
    material.opacityMapChannel = 'a';
    material.useLighting = false;
    material.useSkybox = false;
    material.useFog = false;
    material.useTonemap = false;
    material.depthTest = false;
    material.depthWrite = false;
    material.alphaTest = 0.01;
    material.opacity = 0;
    material.update();

    const entity = new cameraEntity.constructor(name);
    entity.addComponent('render', { type: 'plane', castShadows: false, receiveShadows: false });
    entity.render.material = material;
    for (const meshInstance of entity.render.meshInstances ?? []) {
      meshInstance.drawBucket = template.meshInstance.drawBucket;
    }
    entity.setLocalEulerAngles(90, 0, 0);
    entity.setLocalPosition(0, 0, -distance);
    entity.setLocalScale(width, 1, height);
    cameraEntity.addChild(entity);
    return { entity, material, texture, canvas };
  };

  const maskCanvas = makeCanvas(32, 32);
  const maskContext = maskCanvas.getContext('2d');
  if (!maskContext) throw new Error('A 2D canvas context is unavailable for the XR safety view.');
  // Use a visible neutral slate surface, not a near-black blackout.
  maskContext.fillStyle = '#39414a';
  maskContext.fillRect(0, 0, maskCanvas.width, maskCanvas.height);
  const mask = makeQuad('Visitor XR boundary safety view', maskCanvas, 20, 16, 2.05);

  const vignetteCanvas = makeCanvas(256, 256);
  const vignetteContext = vignetteCanvas.getContext('2d');
  if (!vignetteContext) throw new Error('A 2D canvas context is unavailable for the XR boundary vignette.');
  const vignetteGradient = vignetteContext.createRadialGradient(128, 128, 28, 128, 128, 184);
  vignetteGradient.addColorStop(0, 'rgba(5, 8, 12, 0)');
  vignetteGradient.addColorStop(0.58, 'rgba(5, 8, 12, 0.02)');
  vignetteGradient.addColorStop(0.82, 'rgba(5, 8, 12, 0.18)');
  vignetteGradient.addColorStop(1, 'rgba(5, 8, 12, 0.68)');
  vignetteContext.fillStyle = vignetteGradient;
  vignetteContext.fillRect(0, 0, vignetteCanvas.width, vignetteCanvas.height);
  const vignette = makeQuad('Visitor XR boundary vignette', vignetteCanvas, 20, 16, 2.04);

  const cardCanvas = makeCanvas(1024, 512);
  const drawCard = () => {
    const context = cardCanvas.getContext('2d');
    if (!context) throw new Error('A 2D canvas context is unavailable for the XR boundary message.');
    const strings = locale();
    context.clearRect(0, 0, cardCanvas.width, cardCanvas.height);
    context.fillStyle = 'rgba(20, 23, 28, 0.96)';
    context.fillRect(12, 12, 1000, 488);
    context.textAlign = 'center';
    context.textBaseline = 'middle';
    context.fillStyle = '#f6f7f8';
    context.font = '600 44px -apple-system, system-ui, sans-serif';
    context.fillText(strings.boundaryTitle ?? 'Exhibition boundary', 512, 132, 900);
    context.fillStyle = '#d8dce1';
    context.font = '400 30px -apple-system, system-ui, sans-serif';
    context.fillText(strings.boundaryMessage ?? 'You are outside the exhibition space.', 512, 230, 900);
    context.fillText(strings.boundaryHelp ?? 'Select Recenter to return to the exhibition.', 512, 294, 900);
    card.texture.setSource(card.canvas);
    card.texture.upload();
    drawButton();
  };
  const card = makeQuad('Visitor XR boundary guidance', cardCanvas, 1.3, 0.7, 1.58);
  card.entity.setLocalPosition(0, 0.035, -1.58);

  const buttonCanvas = makeCanvas(512, 128);
  const drawButton = () => {
    const context = buttonCanvas.getContext('2d');
    if (!context) throw new Error('A 2D canvas context is unavailable for the XR Recenter control.');
    const strings = locale();
    context.clearRect(0, 0, buttonCanvas.width, buttonCanvas.height);
    context.fillStyle = '#d7e0e8';
    context.fillRect(8, 8, 496, 112);
    context.textAlign = 'center';
    context.textBaseline = 'middle';
    context.fillStyle = '#17212a';
    context.font = '600 38px -apple-system, system-ui, sans-serif';
    context.fillText(strings.recenter ?? 'Recenter', 256, 64, 450);
    button.texture.setSource(button.canvas);
    button.texture.upload();
  };
  const button = makeQuad('Visitor XR Recenter selectable control', buttonCanvas, 0.92, 0.19, 1.5);
  button.entity.setLocalPosition(0, -0.17, -1.5);

  let mode = 'inside';
  let maskOpacity = 0;
  let vignetteOpacity = 0;
  let cardOpacity = 0;
  const setQuadOpacity = (quad, opacity) => {
    const value = Math.max(0, Math.min(1, opacity));
    if (Math.abs((quad.material.opacity ?? 0) - value) < 0.004) return false;
    quad.material.opacity = value;
    quad.material.update();
    return true;
  };
  const setState = (nextMode, vignetteTarget = 0) => {
    mode = ['inside', 'approaching', 'outside'].includes(nextMode) ? nextMode : 'outside';
    targetVignetteOpacity = mode === 'approaching'
      ? Math.max(0, Math.min(maximumVignetteOpacity, vignetteTarget))
      : 0;
    app.renderNextFrame = true;
  };
  let targetVignetteOpacity = 0;
  const damp = (current, target, deltaMs, responseMs) => {
    if (deltaMs <= 0) return current;
    const factor = 1 - Math.exp(-deltaMs / Math.max(1, responseMs));
    const next = current + (target - current) * factor;
    return Math.abs(next - target) < 0.004 ? target : next;
  };
  const update = deltaSeconds => {
    const deltaMs = Math.max(0, Math.min(100, (Number(deltaSeconds) || 0) * 1000));
    const before = [maskOpacity, vignetteOpacity, cardOpacity];
    maskOpacity = damp(maskOpacity, mode === 'outside' ? 1 : 0, deltaMs, fadeDurationMs / 3);
    vignetteOpacity = damp(vignetteOpacity, targetVignetteOpacity, deltaMs, Math.min(180, fadeDurationMs / 3));
    cardOpacity = damp(cardOpacity, mode === 'outside' ? 1 : 0, deltaMs, fadeDurationMs / 2.5);
    const changed = before.some((value, index) => Math.abs(value - [maskOpacity, vignetteOpacity, cardOpacity][index]) > 0.004);

    mask.entity.enabled = mode === 'outside' || maskOpacity > 0.004;
    vignette.entity.enabled = mode === 'approaching' || vignetteOpacity > 0.004;
    card.entity.enabled = mode === 'outside' || cardOpacity > 0.004;
    button.entity.enabled = mode === 'outside' || cardOpacity > 0.004;
    setQuadOpacity(mask, maskOpacity);
    setQuadOpacity(vignette, vignetteOpacity);
    setQuadOpacity(card, cardOpacity);
    setQuadOpacity(button, cardOpacity);
    if (changed) app.renderNextFrame = true;
    return { mode, maskOpacity, vignetteOpacity, cardOpacity, fullyMasked: maskOpacity >= 0.985 };
  };
  const hitTestRecenterRay = (origin, direction) => {
    const noHit = reason => ({ hit: false, reason, localPoint: null, distance: null });
    if (mode !== 'outside' || cardOpacity < 0.35 || !button.entity.enabled) {
      return noHit('recenter-control-not-visible');
    }
    const worldMatrix = button.entity.getWorldTransform?.();
    if (!worldMatrix?.constructor || !worldMatrix?.copy || !worldMatrix?.invert) {
      return noHit('recenter-control-transform-unavailable');
    }
    const originValues = readVector(origin);
    const directionValues = readVector(direction);
    if (!originValues?.every(Number.isFinite) || !directionValues?.every(Number.isFinite)) {
      return noHit('input-ray-unavailable');
    }
    const inverse = new worldMatrix.constructor();
    inverse.copy(worldMatrix).invert();
    const Vec3 = cameraEntity.getPosition().constructor;
    const localOrigin = inverse.transformPoint(new Vec3(...originValues), new Vec3());
    const localDirection = inverse.transformVector(new Vec3(...directionValues), new Vec3());
    if (localOrigin.y <= 0 || localDirection.y >= 0) {
      return noHit('ray-does-not-face-control-front');
    }
    const result = intersectLocalQuadRay(
      [localOrigin.x, localOrigin.y, localOrigin.z],
      [localDirection.x, localDirection.y, localDirection.z]
    );
    return { ...result, localOrigin: [localOrigin.x, localOrigin.y, localOrigin.z] };
  };
  drawCard();
  let destroyed = false;
  const reset = () => {
    mode = 'inside';
    maskOpacity = 0;
    vignetteOpacity = 0;
    cardOpacity = 0;
    targetVignetteOpacity = 0;
    for (const quad of [mask, vignette, card, button]) {
      quad.entity.enabled = false;
      setQuadOpacity(quad, 0);
    }
    app.renderNextFrame = true;
  };
  const destroy = () => {
    if (destroyed) return;
    destroyed = true;
    for (const quad of [button, card, vignette, mask]) {
      quad.entity.destroy();
      quad.material.destroy();
      quad.texture.destroy();
    }
  };
  reset();
  cameraEntity.once?.('destroy', destroy);
  return {
    ready: true,
    setState,
    update,
    reset,
    refreshText: drawCard,
    hitTestRecenterRay,
    snapshot: () => ({ mode, maskOpacity, vignetteOpacity, cardOpacity, fullyMasked: maskOpacity >= 0.985 }),
    destroy,
    renderedBy: 'progressively faded PlayCanvas XR quads using lightweight CanvasTextures; no post-processing',
    control: 'selectable camera-following XR quad raycast from WebXR input sources; existing Palm Menu retained'
  };
}

function installNavigationGuard(handle, geometry) {
  const { cameraEntity, rigEntity } = cameraAndRig(handle);
  const navigation = findScriptInstance(rigEntity?.script, 'xrNavigation');
  if (!navigation) {
    logXr('navigation_guard_unavailable', {
      reason: 'The Viewer camera rig has no accessible xrNavigation script'
    }, handle);
    return { safe: false, mode: 'unavailable', navigation, cameraEntity, rigEntity };
  }

  if (!geometry?.guard) {
    const disabled = disableXrLocomotion(navigation);
    logXr(disabled ? 'navigation_fail_safe' : 'navigation_guard_error', {
      reason: 'The enclosure mesh could not be validated; artificial XR translation has been disabled',
      locomotionDisabled: disabled
    }, handle);
    return { safe: disabled, mode: disabled ? 'locomotion-disabled' : 'unsafe', navigation, cameraEntity, rigEntity };
  }

  const originalUpdate = navigation.update;
  const originalTeleport = navigation.tryTeleport;
  if (typeof originalUpdate !== 'function' || typeof originalTeleport !== 'function') {
    const disabled = disableXrLocomotion(navigation);
    logXr(disabled ? 'navigation_fail_safe' : 'navigation_guard_error', {
      reason: 'The pinned XR navigation methods could not be wrapped',
      locomotionDisabled: disabled
    }, handle);
    return { safe: disabled, mode: disabled ? 'locomotion-disabled' : 'unsafe', navigation, cameraEntity, rigEntity };
  }

  if (!cameraEntity?.getPosition || !rigEntity?.getPosition || !rigEntity?.setPosition) {
    const disabled = disableXrLocomotion(navigation);
    logXr(disabled ? 'navigation_fail_safe' : 'navigation_guard_error', {
      reason: 'The XR rig or camera does not expose the transforms required for enclosure checks',
      locomotionDisabled: disabled
    }, handle);
    return { safe: disabled, mode: disabled ? 'locomotion-disabled' : 'unsafe', navigation, cameraEntity, rigEntity };
  }

  const app = handle.app;
  let lastBlockLog = 0;
  let lastAcceptLog = 0;
  let trackingFailureLogged = false;
  const throttled = (last, interval) => performance.now() - last >= interval;
  const currentPoint = entity => {
    const position = entity?.getPosition?.();
    if (!position) return null;
    const point = readVector(position);
    return point.every(Number.isFinite) ? point : null;
  };
  const currentLocalPoint = entity => {
    const position = entity?.getLocalPosition?.();
    if (!position) return null;
    const point = readVector(position);
    return point.every(Number.isFinite) ? point : null;
  };
  const projectHeadFromRig = (rig, localPosition) => {
    const transform = rig?.getWorldTransform?.();
    if (!Array.isArray(localPosition) || !transform?.transformPoint) return null;
    const projected = transform.transformPoint({
      x: localPosition[0],
      y: localPosition[1],
      z: localPosition[2]
    });
    const worldPosition = projected ? readVector(projected) : null;
    return worldPosition?.every(Number.isFinite) ? worldPosition : null;
  };
  const stopForInvalidPose = source => {
    const disabled = disableXrLocomotion(navigation);
    if (!trackingFailureLogged) {
      trackingFailureLogged = true;
      logXr(disabled ? 'navigation_fail_safe' : 'navigation_guard_error', {
        source,
        reason: 'An XR camera or rig world position was unavailable; artificial locomotion has been disabled',
        locomotionDisabled: disabled
      }, handle);
    }
    return disabled;
  };
  const coordinateMapping = {
    xrPoseSource: 'PlayCanvas XR camera entity world transform, including its parent rig',
    rigPoseSource: 'PlayCanvas XR camera parent rig world transform',
    sceneCoordinateFrame: geometry.coordinateFrame,
    referenceSpaceType: handle.app.xr?.referenceSpaceType ?? null,
    modelSceneUniformScale: handle.__visitorModelSceneScale ?? null,
    collisionModelTransformApplied: false
  };
  const coordinateSnapshot = rig => ({
    referenceSpaceType: handle.app.xr?.referenceSpaceType ?? null,
    cameraLocalPose: cameraLocalPoseSignature(cameraEntity),
    rigWorldTransform: entityTransform(rig)?.world ?? null,
    cameraWorldTransform: entityTransform(cameraEntity)?.world ?? null,
    resultingSceneWorldPoint: currentPoint(cameraEntity),
    collisionFrame: geometry.coordinateFrame,
    conversion: 'PlayCanvas applies the XR local camera pose through the rig world transform; the resulting world point is tested directly against the existing Y-up collision mesh.'
  });
  const recordBlocked = (source, from, to, verdict, movement = {}) => {
    const now = performance.now();
    if (!throttled(lastBlockLog, 250)) return;
    lastBlockLog = now;
    logXr('navigation_blocked', {
      source,
      movementSource: source === 'teleport' || source === 'teleport_postcondition'
        ? 'xr_rig_teleport'
        : 'xr_rig_artificial_locomotion',
      proposedFrom: from,
      proposedTo: to,
      proposedTranslation: to.map((value, index) => value - from[index]),
      reason: verdict.reason,
      boundaryTest: verdict,
      acceptedTranslation: [0, 0, 0],
      rejectedTranslation: to.map((value, index) => value - from[index]),
      coordinateMapping,
      movementPolicy: movement.movementPolicy ?? movement.verdict?.movementPolicy ?? 'swept-segment',
      ...movement,
      collisionAsset: geometry.collisionHash
    }, handle);
  };
  const recordAccepted = (source, from, to, movement = {}) => {
    if (!throttled(lastAcceptLog, 500)) return;
    lastAcceptLog = performance.now();
    const rotationPivot = movement.movementKind === 'camera-pivot-rotation';
    logXr(rotationPivot ? 'navigation_rotation_pivot_accepted' : 'navigation_translation_accepted', {
      source,
      movementSource: source === 'teleport'
        ? 'xr_rig_teleport'
        : rotationPivot ? 'xr_rig_rotation_pivot' : 'xr_rig_artificial_locomotion',
      from,
      to,
      proposedTranslation: to.map((value, index) => value - from[index]),
      boundaryTest: { allowed: true, reason: null },
      acceptedTranslation: to.map((value, index) => value - from[index]),
      rejectedTranslation: [0, 0, 0],
      coordinateMapping,
      ...movement,
      movementPolicy: rotationPivot
        ? 'viewpoint-stationary-camera-pivot'
        : movement.movementPolicy ?? movement.verdict?.movementPolicy ?? 'swept-segment',
      collisionAsset: geometry.collisionHash
    }, handle);
  };

  const predictTeleportHeadPosition = (instance, inputSource) => {
    const record = instance._arcHits?.get?.(inputSource);
    const rigPosition = currentPoint(instance.entity);
    const cameraPosition = projectHeadFromRig(
      instance.entity,
      currentLocalPoint(instance._cameraEntity)
    );
    if (!record?.valid || !record.point || !rigPosition || !cameraPosition ||
        !Number.isFinite(instance._currentGroundY)) return null;
    const point = readVector(record.point);
    if (!point) return null;
    const proposedRig = [
      point[0] - (cameraPosition[0] - rigPosition[0]),
      rigPosition[1] + (point[1] - instance._currentGroundY),
      point[2] - (cameraPosition[2] - rigPosition[2])
    ];
    return [
      cameraPosition[0] + (proposedRig[0] - rigPosition[0]),
      cameraPosition[1] + (proposedRig[1] - rigPosition[1]),
      cameraPosition[2] + (proposedRig[2] - rigPosition[2])
    ];
  };

  try {
    const hooks = installXrNavigationGuard(navigation, {
      rigEntity,
      cameraEntity,
      guard: geometry.guard,
      readWorldPosition: currentPoint,
      readLocalPosition: currentLocalPoint,
      predictHeadFromRig: projectHeadFromRig,
      setRigWorldPosition: (entity, position) => entity.setPosition(...position),
      readGroundHeight: instance => instance._currentGroundY,
      writeGroundHeight: (instance, value) => { instance._currentGroundY = value; },
      requestFrame: () => { app.renderNextFrame = true; },
      predictTeleportHeadPosition,
      coordinateSnapshot: () => coordinateSnapshot(rigEntity),
      onInvalid: movement => stopForInvalidPose(`${movement.source}:${movement.phase}`),
      onDecision: movement => {
        const teleport = movement.source === 'xr_rig_teleport';
        const source = teleport
          ? movement.phase === 'precheck' ? 'teleport' : 'teleport_postcondition'
          : 'smooth_or_vertical_locomotion';
        const predictedHeadDelta = movement.observedAfterHead?.map(
          (value, index) => value - movement.proposedHead[index]
        ) ?? null;
        if (!teleport && predictedHeadDelta && Math.hypot(...predictedHeadDelta) > 0.02 &&
            performance.now() - mismatchLogAt >= 1000) {
          mismatchLogAt = performance.now();
          logXr('navigation_rig_camera_delta_mismatch', {
            movementSource: movement.source,
            reason: 'The observed camera world position differed from its projection through the updated XR rig; the enclosure test used the projected tracked camera position.',
            rigDelta: movement.rigDelta,
            cameraWorldDelta: movement.cameraWorldDelta,
            projectedHeadDelta: predictedHeadDelta,
            predictedCameraWorldPosition: movement.proposedHead,
            coordinateMapping
          }, handle);
        }
        const details = {
          movementPolicy: movement.verdict.movementPolicy ?? 'swept-segment',
          rigDelta: movement.rigDelta,
          cameraWorldDelta: movement.cameraWorldDelta,
          observedCameraWorldPositionAfterUpdate: movement.observedAfterHead,
          predictedCameraWorldPositionFromUpdatedRig: movement.proposedHead,
          destinationInspection: movement.verdict.destinationInspection ?? null,
          xrToSceneConversion: { before: movement.beforeCoordinate, after: movement.afterCoordinate }
        };
        if (movement.verdict.allowed && movement.movementKind === 'camera-pivot-rotation') {
          recordAccepted('rotation_pivot', movement.beforeHead, movement.proposedHead, details);
        } else if (movement.verdict.allowed) {
          recordAccepted(teleport ? 'teleport' : source, movement.beforeHead, movement.proposedHead, details);
        }
        else recordBlocked(source, movement.beforeHead, movement.proposedHead, movement.verdict, details);
      }
    });
    if (!hooks.installed) throw new Error('Pinned xrNavigation update and teleport methods were not both wrapped.');
  } catch (error) {
    const disabled = disableXrLocomotion(navigation);
    logXr(disabled ? 'navigation_fail_safe' : 'navigation_guard_error', {
      reason: 'Could not install the enclosure guard on the XR rig',
      error: describeError(error),
      locomotionDisabled: disabled
    }, handle);
    return { safe: disabled, mode: disabled ? 'locomotion-disabled' : 'unsafe', navigation, cameraEntity, rigEntity };
  }

  const manipulation = findScriptInstance(rigEntity?.script, 'xrManipulation');
  let manipulationSafe = !manipulation;
  if (manipulation) {
    manipulation.enableTranslate = false;
    manipulation.enableRotate = false;
    manipulation.enableScale = false;
    manipulationSafe = manipulation.enableTranslate === false &&
      manipulation.enableRotate === false &&
      manipulation.enableScale === false;
    if (!manipulationSafe && 'enabled' in manipulation) {
      manipulation.enabled = false;
      manipulationSafe = manipulation.enabled === false;
    }
  }
  logXr('navigation_guard_installed', {
    mode: 'swept-artificial-movement-and-validated-teleport-destination',
    xrNavigationFound: true,
    updateHookInstalled: navigation.update !== originalUpdate,
    teleportHookInstalled: navigation.tryTeleport !== originalTeleport,
    artificialTranslationTest: 'tracked camera local position projected through the updated XR rig world matrix, then swept across the complete proposed segment; pure rig-pivot turns preserve the camera center',
    manipulationFound: Boolean(manipulation),
    manipulationDisabled: Boolean(manipulation && manipulationSafe),
    collisionTriangleCount: geometry.guard.triangleCount,
    collisionBounds: geometry.guard.bounds,
    collisionRadius: geometry.guard.radius,
    collisionFrame: geometry.coordinateFrame,
    coordinateMapping,
    guardedMovementSources: [
      { source: 'xrNavigation.update artificial translation', policy: 'swept-segment' },
      { source: 'xrNavigation.tryTeleport', policy: 'destination containment and clearance' }
    ],
    separatelyObservedMovementSource: 'PlayCanvas XR viewer-pose centre plus left/right XRView world poses',
    acceptedClearance: geometry.acceptedClearance,
    sceneScaleAppliedToCollision: false,
    collisionHash: geometry.collisionHash,
    resetSpawnInside: geometry.guard.isValid(
      handle.__visitorResetSpawn?.position ?? [NaN, NaN, NaN]
    )
  }, handle);
  return {
    safe: manipulationSafe,
    mode: 'swept-artificial-movement-and-validated-teleport-destination',
    navigation,
    cameraEntity,
    rigEntity,
    guard: geometry.guard,
    manipulation
  };
}

export async function installXRControls(handle, plan, exhibition, settings) {
  const button = document.querySelector('#enterVR');
  const status = document.querySelector('#xrStatus');
  const languageSelect = document.querySelector('#languageSelect');
  const resetButton = document.querySelector('#resetView');
  if (!button || !status) {
    logXr('ui_error', { reason: 'VR control markup is missing' }, handle);
    return;
  }

  const resetSpawn = settings?.cameras?.[0]?.initial ?? null;
  const diagnosticsEnabled = exhibition?.xr?.diagnostics?.enabled === true;
  handle.__visitorResetSpawn = resetSpawn;
  handle.__visitorExpectedSceneTransform = exhibition?.initial_camera?.model_scene_transform_supersplat ?? null;
  handle.__visitorModelSceneScale = exhibition?.initial_camera?.model_scene_transform_supersplat?.uniformScale ?? null;
  const sceneEntity = handle.app.root.findComponents('gsplat')?.[0]?.entity ?? null;
  logXr('configured_scene_transform_initialized', {
    expectedTransform: exhibition?.initial_camera?.model_scene_transform_supersplat ?? null,
    viewerHomeTransformRebased: handle.__visitorXRSceneHomeRebase?.returned ?? false,
    rebaseApi: 'viewer.rebaseXRHomeTransform()',
    sceneTransform: entityTransform(sceneEntity)
  }, handle);
  let geometry = null;
  let geometryError = null;
  try {
    geometry = await loadCollisionGeometry(exhibition);
  } catch (error) {
    geometryError = describeError(error);
    logXr('collision_geometry_error', { error: geometryError }, handle);
  }

  const navigation = installNavigationGuard(handle, geometry);
  handle.__visitorNavigationGuard = navigation;
  const configuredSafeSpawnPoint = configuredSafeXrSpawnPoint(navigation.guard, resetSpawn);
  let phase = 'desktop';
  let pendingAction = null;
  let openingSpawnReady = false;
  let openingSpawnPreparationPromise = null;
  let lastErrorAction = !plan.buildIntegrityValid
    ? 'build'
    : !navigation.safe || navigation.mode === 'locomotion-disabled' ? 'navigation' : null;
  let entrySnapshot = null;
  let entryCanvasParent = null;
  let recoveryPromise = null;
  let resetPromise = null;
  let resetCallSource = null;
  let sessionEndSource = null;
  let cameraTransitionState = 'idle';
  let lastTransitionObservation = null;
  let transitionTarget = null;
  let transitionStartedAt = null;
  let lastTransitionSampleAt = 0;
  let lastHeadPose = null;
  let lastHeadPoseSampleAt = 0;
  let lastRigPosition = null;
  let lastTrackedHeadOutOfBounds = false;
  let boundaryOutOfBounds = false;
  let boundaryMode = 'inside';
  let safeXrSpawnPoint = null;
  let safeXrGroundHeight = null;
  let safeXrRigPosition = null;
  let latestBoundaryInspection = null;
  let recenterPromise = null;
  let mismatchLogAt = 0;

  instrumentWebXRRequestSession(handle);

  const locale = () => {
    const selected = languageSelect?.value || document.documentElement.lang || 'en';
    return window.__visitorLocales?.[selected] ?? window.__visitorLocales?.en ?? {};
  };
  let boundaryFeedback = null;
  const boundaryFeedbackOptions = exhibition?.xr?.boundary_feedback ?? {};
  const boundaryFeedbackOptionsForClassifier = {
    approachDistance: Number(boundaryFeedbackOptions.approach_distance_viewer_units),
    outsideEnterClearance: Number(boundaryFeedbackOptions.outside_enter_clearance_viewer_units),
    outsideExitClearance: Number(boundaryFeedbackOptions.outside_exit_clearance_viewer_units),
    maximumVignetteOpacity: Number(boundaryFeedbackOptions.maximum_vignette_opacity)
  };
  try {
    boundaryFeedback = createXrBoundaryFeedback(
      handle,
      navigation.cameraEntity,
      locale,
      boundaryFeedbackOptions
    );
    logXr('xr_boundary_feedback_installed', {
      renderer: boundaryFeedback.renderedBy,
      control: boundaryFeedback.control,
      hiddenUntilOutside: true,
      boundaryMaskFollowsTrackedCamera: true,
      progressiveModes: ['inside', 'approaching', 'outside'],
      thresholds: {
        approachDistanceViewerUnits: boundaryFeedbackOptions.approach_distance_viewer_units ?? 0.85,
        outsideEnterClearanceViewerUnits: boundaryFeedbackOptions.outside_enter_clearance_viewer_units ?? 0,
        outsideExitClearanceViewerUnits: boundaryFeedbackOptions.outside_exit_clearance_viewer_units ?? 0.18,
        fadeDurationMs: boundaryFeedbackOptions.fade_duration_ms ?? 420
      },
      selectionModes: ['transient-pointer', 'tracked-pointer', 'gaze', 'screen']
    }, handle);
  } catch (error) {
    logXr('xr_boundary_feedback_unavailable', {
      error: describeError(error),
      policy: 'VR entry remains disabled because tracked-head escape cannot be safely presented or recovered'
    }, handle);
  }
  const xrBoundarySafe = () => Boolean(navigation.safe && boundaryFeedback?.ready &&
    plan.buildIntegrityValid && buildIntegrity?.buildMatches && buildIntegrity?.hashesMatch);
  if (!xrBoundarySafe()) lastErrorAction = plan.buildIntegrityValid ? 'navigation' : 'build';
  const update = () => {
    const strings = locale();
    setButtonState(
      button,
      pendingAction,
      phase,
      Boolean(handle.state.canStartVR),
      handle.state,
      strings,
      xrBoundarySafe() && openingSpawnReady
    );
    if (resetPromise) button.disabled = true;
    if (resetButton) resetButton.disabled = Boolean(resetPromise || openingSpawnPreparationPromise) || phase !== 'desktop';
    status.hidden = !lastErrorAction;
    if (lastErrorAction) {
      status.textContent = {
        enter: strings.enterVRError,
        exit: strings.exitVRError,
        reset: strings.resetError,
        navigation: strings.navigationUnavailable,
        build: strings.xrRuntimeUnavailable
      }[lastErrorAction] ?? strings.exitVRError;
    }
  };
  const { cameraEntity } = cameraAndRig(handle);
  const canvas = handle.app.graphicsDevice.canvas;
  const snapshot = () => diagnosticSnapshot(
    handle,
    resetSpawn,
    phase,
    entryCanvasParent,
    { state: cameraTransitionState, observation: lastTransitionObservation }
  );
  const reportSnapshot = (event, point, extra = {}) => {
    logXr(event, { point, snapshot: snapshot(), ...extra }, handle);
  };

  handle.__visitorFrameStats = { count: 0, lastMonotonicMs: null };
  let xrSessionStartedAt = null;
  let firstTrackingFrameRecorded = false;
  let oneSecondPoseRecorded = false;
  let floorPlacementStarted = false;
  let floorPlacementCompleted = false;
  handle.app.on('xrFloorPlacementStarted', details => {
    floorPlacementStarted = true;
    logXr('xr_floor_placement_started', details ?? {}, handle);
  });
  handle.app.on('xrFloorPlacementComplete', details => {
    floorPlacementCompleted = true;
    logXr('xr_floor_placement_completed', details ?? {}, handle);
  });
  handle.app.on('frameend', () => {
    const now = performance.now();
    handle.__visitorFrameStats.count++;
    handle.__visitorFrameStats.lastMonotonicMs = now;

    if (phase === 'in-vr') {
      const elapsedMs = xrSessionStartedAt === null ? null : now - xrSessionStartedAt;
      const xrViews = cameraEntity?.camera?.xrViews ?? [];
      const localCamera = cameraLocalPoseSignature(cameraEntity)?.position;
      const validTrackingFrame = xrViews.length > 0 && localCamera?.every(Number.isFinite);
      if (!firstTrackingFrameRecorded && validTrackingFrame) {
        firstTrackingFrameRecorded = true;
        reportSnapshot('first_valid_xr_tracking_frame', 'app.frameend', {
          elapsedMs,
          xrViewCount: xrViews.length,
          cameraLocalPose: cameraLocalPoseSignature(cameraEntity),
          referenceSpaceType: handle.app.xr?.referenceSpaceType ?? null,
          referenceSpace: {
            available: Boolean(handle.app.xr?._referenceSpace),
            constructor: handle.app.xr?._referenceSpace?.constructor?.name ?? null
          }
        });
      }
      if (!oneSecondPoseRecorded && elapsedMs !== null && elapsedMs >= 1000) {
        oneSecondPoseRecorded = true;
        reportSnapshot('xr_one_second_pose_sample', 'app.frameend', {
          elapsedMs,
          xrViewCount: xrViews.length,
          floorPlacementStarted,
          floorPlacementCompleted,
          floorPlacementPolicy: handle.state.hasCollision
            ? 'collision geometry supplies floorBelow; no asynchronous splat-floor placement'
            : 'Viewer may perform asynchronous splat-floor placement',
          cameraLocalPose: cameraLocalPoseSignature(cameraEntity)
        });
      }
    }

    const transitionActive = (transitionStartedAt !== null && now - transitionStartedAt < 7000) || phase === 'entering' ||
      phase === 'in-vr' || phase === 'exiting' || phase === 'restoring';
    const frameSampleInterval = phase === 'restoring' ? 250 : 1000;
    if (transitionActive && now - lastTransitionSampleAt >= frameSampleInterval) {
      lastTransitionSampleAt = now;
      logXr('lifecycle_frame_sample', {
        lifecyclePhase: phase,
        transitionState: cameraTransitionState,
        frameEndCount: handle.__visitorFrameStats.count,
        transitionElapsedMs: transitionStartedAt === null ? null : Number((now - transitionStartedAt).toFixed(1)),
        cameraManagerPoseOrTarget: readCameraManagerPose(handle, cameraEntity),
        transitionTarget,
        renderedCameraPose: cameraPoseSignature(cameraEntity),
        rendering: renderingState(handle, cameraEntity, entryCanvasParent)
      }, handle);
    }
  });

  let sceneEnabledBeforeXr = sceneEntity?.enabled ?? true;
  let lastBoundaryTraceAt = 0;
  const onBoundaryFeedbackUpdate = deltaTime => {
    const feedback = boundaryFeedback?.update(deltaTime);
    if (boundaryMode === 'outside' && feedback?.fullyMasked && sceneEntity?.enabled) {
      sceneEntity.enabled = false;
      handle.app.renderNextFrame = true;
      logXr('xr_boundary_scene_masked', {
        boundaryMode,
        feedback,
        sceneEnabled: sceneEntity.enabled,
        policy: 'hide SOG after the neutral safety view has smoothly covered it'
      }, handle);
    }
  };
  handle.app.on('update', onBoundaryFeedbackUpdate);
  const currentRigPoint = () => readVector(navigation.rigEntity?.getPosition?.());
  const applyBoundaryMode = (nextMode, evidence, source = 'tracked_viewpoint', classification = null) => {
    const normalizedMode = ['inside', 'approaching', 'outside'].includes(nextMode) ? nextMode : 'outside';
    const previousMode = boundaryMode;
    boundaryMode = normalizedMode;
    boundaryOutOfBounds = normalizedMode === 'outside';
    if (!boundaryOutOfBounds && sceneEntity) sceneEntity.enabled = sceneEnabledBeforeXr;
    boundaryFeedback?.setState(normalizedMode, classification?.vignetteOpacity ?? 0);
    handle.app.renderNextFrame = true;
    if (previousMode === normalizedMode) return false;
    const event = normalizedMode === 'outside'
      ? 'xr_viewpoint_boundary_entered'
      : previousMode === 'outside'
        ? 'xr_viewpoint_boundary_recovered'
        : 'xr_boundary_feedback_state_changed';
    logXr(event, {
      previousBoundaryMode: previousMode,
      boundaryMode: normalizedMode,
      movementSource: source,
      viewpoint: evidence?.sample ?? null,
      enclosure: evidence?.inspection ?? null,
      classification,
      feedback: boundaryFeedback?.snapshot?.() ?? null,
      sceneEnabled: sceneEntity?.enabled ?? null,
      boundaryWarningVisible: normalizedMode === 'outside',
      safeXrSpawnPoint,
      recoveryAction: normalizedMode === 'outside'
        ? 'select the in-scene Recenter control or use the existing Viewer Palm Menu'
        : null,
      trackedPoseModified: false
    }, handle);
    return true;
  };

  const onXrFramerender = () => {
    if (!handle.app.xr?.active || (phase !== 'entering' && phase !== 'in-vr')) return;
    const sample = sampleXrViewpoints(cameraEntity, navigation.rigEntity);
    const inspection = inspectTrackedViewpoints(navigation.guard, sample);
    const rigPosition = currentRigPoint();
    if (!sample.center || !inspection.center) {
      const failure = { sample, inspection, rigPosition };
      const classification = nextBoundaryFeedbackMode(null, boundaryMode, boundaryFeedbackOptionsForClassifier);
      applyBoundaryMode('outside', failure, 'invalid_xr_viewpoint', classification);
      logXr('xr_viewpoint_sample_invalid', {
        movementSource: 'invalid_xr_tracking_pose',
        sample,
        inspection,
        rigPosition,
        policy: 'fade to the XR safety view; preserve tracking; wait for a valid pose or explicit recenter'
      }, handle);
      return;
    }

    if (inspection.valid && !safeXrSpawnPoint) {
      safeXrSpawnPoint = sample.center.slice();
      if (!Number.isFinite(safeXrGroundHeight) && Number.isFinite(navigation.navigation?._currentGroundY)) {
        safeXrGroundHeight = navigation.navigation._currentGroundY;
      }
      if (!safeXrRigPosition) safeXrRigPosition = rigPosition?.slice?.() ?? null;
      logXr('xr_safe_spawn_captured', {
        point: safeXrSpawnPoint,
        source: 'first valid tracked XR viewpoint inside the configured enclosure clearance',
        rigWorldPosition: rigPosition,
        viewpointSample: sample,
        boundaryInspection: inspection,
        configuredOpeningSpawn: resetSpawn,
        sceneTransformInvariant: sceneTransformInvariant(sceneEntity, handle.__visitorExpectedSceneTransform)
      }, handle);
    }

    const currentHeadPose = cameraLocalPoseSignature(cameraEntity);
    const localMovement = poseDifference(lastHeadPose, currentHeadPose);
    const rigDelta = lastRigPosition && rigPosition
      ? Math.hypot(...rigPosition.map((value, index) => value - lastRigPosition[index]))
      : 0;
    const trackedDelta = Number.isFinite(localMovement?.position) ? localMovement.position : 0;
    const movementSource = trackedDelta > 0.025
      ? 'tracked_head_pose'
      : rigDelta > 0.025
        ? 'xr_rig_translation'
        : 'xr_viewpoint_motion';
    latestBoundaryInspection = { sample, inspection, rigPosition, movementSource };
    const classification = nextBoundaryFeedbackMode(
      inspection.minimumClearance,
      boundaryMode,
      boundaryFeedbackOptionsForClassifier
    );
    const nextMode = recenterPromise && boundaryMode === 'outside'
      ? 'outside'
      : classification.mode;
    applyBoundaryMode(nextMode, latestBoundaryInspection, movementSource, classification);
    lastTrackedHeadOutOfBounds = nextMode === 'outside' && movementSource === 'tracked_head_pose';

    const now = performance.now();
    if (movementSource === 'tracked_head_pose' && now - lastHeadPoseSampleAt >= 250) {
      lastHeadPoseSampleAt = now;
      logXr('xr_tracked_head_pose_changed', {
        movementSource,
        previousCameraLocalPose: lastHeadPose,
        currentCameraLocalPose: currentHeadPose,
        localPoseDelta: localMovement,
        trackedViewpoint: sample,
        enclosureContainment: inspection,
        boundaryWarningVisible: boundaryOutOfBounds,
        boundaryMode,
        trackingPolicy: 'natural head translation and rotation remain untouched; boundary protection is a separate scene-space overlay'
      }, handle);
    }
    if (now - lastBoundaryTraceAt >= 1000) {
      lastBoundaryTraceAt = now;
      logXr('xr_boundary_sample', {
        movementSource,
        trackedViewpoint: sample,
        rigWorldPosition: rigPosition,
        xrNavigationGroundHeight: navigation.navigation?._currentGroundY ?? null,
        enclosureContainment: inspection,
        boundaryWarningVisible: boundaryOutOfBounds,
        boundaryMode,
        boundaryFeedback: boundaryFeedback?.snapshot?.() ?? null,
        sceneEnabled: sceneEntity?.enabled ?? null,
        sceneCoordinateFrame: geometry?.coordinateFrame ?? null,
        modelSceneScale: handle.__visitorModelSceneScale ?? null,
        sceneTransformInvariant: sceneTransformInvariant(sceneEntity, handle.__visitorExpectedSceneTransform)
      }, handle);
    }
    lastHeadPose = currentHeadPose;
    lastRigPosition = rigPosition;
  };
  handle.app.on('framerender', onXrFramerender);

  const recenterXR = (source = 'xr_palm_menu') => {
    if (recenterPromise) return recenterPromise;
    if (!handle.app.xr?.active) {
      logXr('xr_boundary_recenter_rejected', { source, reason: 'no active immersive XR session' }, handle);
      return Promise.resolve(false);
    }
    if (!safeXrSpawnPoint) {
      logXr('xr_boundary_recenter_rejected', {
        source,
        reason: 'no valid in-enclosure XR viewpoint has been captured for this session',
        latestBoundaryInspection
      }, handle);
      return Promise.resolve(false);
    }

    recenterPromise = (async () => {
      const beforeSample = sampleXrViewpoints(cameraEntity, navigation.rigEntity);
      const beforeInspection = inspectTrackedViewpoints(navigation.guard, beforeSample);
      if (beforeInspection.valid) {
        latestBoundaryInspection = {
          sample: beforeSample,
          inspection: beforeInspection,
          rigPosition: currentRigPoint(),
          movementSource: 'explicit_recenter_noop'
        };
        logXr('xr_boundary_recenter_noop', {
          source,
          reason: 'the tracked viewpoint is already inside the enclosure clearance',
          safeXrSpawnPoint,
          beforeSample,
          beforeInspection
        }, handle);
        return true;
      }
      const beforeRig = currentRigPoint();
      const beforeGroundHeight = Number.isFinite(navigation.navigation?._currentGroundY)
        ? navigation.navigation._currentGroundY
        : null;
      const plan = computeRecenterRigPosition(beforeRig, beforeSample.center, safeXrSpawnPoint);
      if (!plan.valid) {
        logXr('xr_boundary_recenter_rejected', {
          source,
          reason: plan.reason,
          beforeSample,
          beforeRig,
          safeXrSpawnPoint
        }, handle);
        return false;
      }
      const groundPlan = computeRecenterGroundHeight(
        safeXrGroundHeight,
        safeXrRigPosition,
        plan.position
      );
      const targetGroundHeight = groundPlan.valid ? groundPlan.groundHeight : null;

      logXr('xr_boundary_recenter_requested', {
        source,
        beforeSample,
        beforeInspection,
        beforeRig,
        targetViewpoint: safeXrSpawnPoint,
        proposedRigPosition: plan.position,
        rigTranslation: plan.translation,
        previousGroundHeight: beforeGroundHeight,
        targetGroundHeight,
        appliedFrame: 'XR rig world position; tracked camera pose and orientation are not assigned'
      }, handle);
      try {
        navigation.rigEntity.setPosition(...plan.position);
        if (Number.isFinite(targetGroundHeight) && navigation.navigation) {
          navigation.navigation._currentGroundY = targetGroundHeight;
        }
        handle.app.renderNextFrame = true;
        const framesPresented = Number(await waitForFrameEnd(handle.app, 2000)) +
          Number(await waitForFrameEnd(handle.app, 2000));
        const afterSample = sampleXrViewpoints(cameraEntity, navigation.rigEntity);
        const afterInspection = inspectTrackedViewpoints(navigation.guard, afterSample);
        const afterRig = currentRigPoint();
        latestBoundaryInspection = {
          sample: afterSample,
          inspection: afterInspection,
          rigPosition: afterRig,
          movementSource: 'explicit_recenter'
        };
        const succeeded = framesPresented >= 1 && afterInspection.valid;
        const verifiedMode = nextBoundaryFeedbackMode(
          afterInspection.minimumClearance,
          'outside',
          boundaryFeedbackOptionsForClassifier
        );
        if (succeeded) {
          applyBoundaryMode(verifiedMode.mode, {
            sample: afterSample,
            inspection: afterInspection,
            rigPosition: afterRig
          }, 'explicit_recenter', verifiedMode);
        }
        logXr(succeeded ? 'xr_boundary_recenter_succeeded' : 'xr_boundary_recenter_failed', {
          source,
          targetViewpoint: safeXrSpawnPoint,
          beforeSample,
          beforeInspection,
          beforeRig,
          proposedRigPosition: plan.position,
          previousGroundHeight: beforeGroundHeight,
          targetGroundHeight,
          afterSample,
          afterInspection,
          finalEyePositionContainment: afterInspection.eyes,
          boundaryModeAfterVerification: succeeded ? verifiedMode.mode : boundaryMode,
          framesPresented,
          rendering: renderingState(handle, cameraEntity, entryCanvasParent),
          sceneTransformInvariant: sceneTransformInvariant(sceneEntity, handle.__visitorExpectedSceneTransform),
          trackedPoseModified: false,
          sceneShown: succeeded
        }, handle);
        if (!succeeded) {
          navigation.rigEntity.setPosition(...beforeRig);
          if (beforeGroundHeight !== null && navigation.navigation) {
            navigation.navigation._currentGroundY = beforeGroundHeight;
          }
          handle.app.renderNextFrame = true;
          logXr('xr_boundary_recenter_rolled_back', {
            source,
            reason: framesPresented < 1
              ? 'no XR frame was presented to verify recenter'
              : 'resulting tracked center or eye poses failed enclosure validation',
            restoredRigPosition: beforeRig,
            restoredGroundHeight: beforeGroundHeight,
            trackedPoseModified: false
          }, handle);
        }
        return succeeded;
      } catch (error) {
        try {
          if (beforeRig) navigation.rigEntity.setPosition(...beforeRig);
          if (beforeGroundHeight !== null && navigation.navigation) {
            navigation.navigation._currentGroundY = beforeGroundHeight;
          }
          handle.app.renderNextFrame = true;
        } catch (rollbackError) {
          logXr('xr_boundary_recenter_rollback_error', {
            source,
            error: describeError(rollbackError),
            restoredRigPosition: beforeRig,
            restoredGroundHeight: beforeGroundHeight
          }, handle);
        }
        logXr('xr_boundary_recenter_error', {
          source,
          error: describeError(error),
          safeXrSpawnPoint,
          beforeSample,
          beforeInspection,
          rigTransform: entityTransform(navigation.rigEntity)
        }, handle);
        return false;
      }
    })().finally(() => {
      recenterPromise = null;
    });
    return recenterPromise;
  };

  const supportedPointerModes = new Set(['transient-pointer', 'tracked-pointer', 'gaze', 'screen']);
  let pendingRecenterPointers = new WeakMap();
  let pointerSession = null;
  const supportedPointer = source => !source?.hand &&
    supportedPointerModes.has(source?.targetRayMode ?? source?.inputSource?.targetRayMode);
  const hitTestPlayCanvasPointer = source => {
    if (!source?.getOrigin || !source?.getDirection) {
      return { hit: false, reason: 'PlayCanvas input ray unavailable' };
    }
    return boundaryFeedback?.hitTestRecenterRay(source.getOrigin(), source.getDirection()) ?? {
      hit: false,
      reason: 'boundary feedback unavailable'
    };
  };
  const navigationSelectFilter = navigation.navigation?._isSystemSelect;
  let navigationSelectFilterInstalled = false;
  if (typeof navigationSelectFilter === 'function') {
    navigation.navigation._isSystemSelect = function (source) {
      if (navigationSelectFilter.call(this, source)) return true;
      const mode = source?.targetRayMode ?? source?.inputSource?.targetRayMode ?? null;
      if (!boundaryOutOfBounds || !supportedPointer(source) || source?.selecting !== true) return false;
      const raycast = hitTestPlayCanvasPointer(source);
      if (!raycast.hit) return false;
      const nativeSource = source.inputSource ?? source;
      pendingRecenterPointers.set(nativeSource, {
        hit: true,
        activated: false,
        inputSourceMode: mode,
        raycast,
        startedAt: performance.now(),
        interceptedViewerTeleport: true
      });
      logXr('xr_boundary_recenter_raycast', {
        phase: 'viewer_selectstart_filter',
        inputSourceMode: mode,
        raycast,
        result: 'recenter_button_hit',
        viewerTeleportSuppressedForThisControl: true
      }, handle);
      return true;
    };
    navigationSelectFilterInstalled = true;
  }

  const nativeEventRay = event => {
    const inputSource = event?.inputSource;
    const referenceSpace = handle.app.xr?._referenceSpace;
    if (!inputSource?.targetRaySpace || !referenceSpace || typeof event?.frame?.getPose !== 'function') {
      return { ray: null, reason: 'native target-ray pose unavailable' };
    }
    let pose;
    try {
      pose = event.frame.getPose(inputSource.targetRaySpace, referenceSpace);
    } catch (error) {
      return { ray: null, reason: describeError(error) };
    }
    const position = readVector(pose?.transform?.position);
    const orientation = readQuaternion(pose?.transform?.orientation);
    const localDirection = xrTargetRayDirection(orientation);
    const rigTransform = navigation.rigEntity?.getWorldTransform?.();
    const Vec3 = navigation.rigEntity?.getPosition?.()?.constructor;
    if (!position?.every(Number.isFinite) || !localDirection || !rigTransform?.transformPoint ||
        !rigTransform?.transformVector || typeof Vec3 !== 'function') {
      return { ray: null, reason: 'native target-ray transform could not be converted to the Viewer world frame' };
    }
    const origin = rigTransform.transformPoint(new Vec3(...position), new Vec3());
    const direction = rigTransform.transformVector(new Vec3(...localDirection), new Vec3());
    direction.normalize?.();
    return {
      ray: { origin, direction },
      referenceSpaceType: handle.app.xr?.referenceSpaceType ?? null,
      nativeOrigin: position,
      nativeDirection: localDirection
    };
  };
  const eventRaycast = event => {
    const result = nativeEventRay(event);
    return {
      ...result,
      raycast: result.ray && boundaryFeedback
        ? boundaryFeedback.hitTestRecenterRay(result.ray.origin, result.ray.direction)
        : { hit: false, reason: result.reason ?? 'boundary feedback unavailable' }
    };
  };
  const onNativeSelectStart = event => {
    const inputSource = event?.inputSource;
    if (!inputSource || !supportedPointer(inputSource)) return;
    const tested = eventRaycast(event);
    const hit = boundaryOutOfBounds && tested.raycast.hit;
    if (hit) {
      pendingRecenterPointers.set(inputSource, {
        hit: true,
        activated: false,
        inputSourceMode: inputSource.targetRayMode,
        raycast: tested.raycast,
        startedAt: performance.now(),
        interceptedViewerTeleport: navigationSelectFilterInstalled
      });
    }
    logXr('xr_boundary_input_selectstart', {
      inputPath: 'native XRSession event',
      inputSourceMode: inputSource.targetRayMode,
      profiles: inputSource.profiles ?? [],
      handedness: inputSource.handedness ?? null,
      boundaryMode,
      ray: tested.ray ? {
        origin: readVector(tested.ray.origin),
        direction: readVector(tested.ray.direction),
        referenceSpaceType: tested.referenceSpaceType
      } : null,
      raycast: tested.raycast,
      recenterButtonArmed: Boolean(hit),
      viewerTeleportSuppressedForThisControl: Boolean(hit && navigationSelectFilterInstalled)
    }, handle);
  };
  const onNativeSelect = event => {
    const inputSource = event?.inputSource;
    if (!inputSource || !supportedPointer(inputSource)) return;
    const tested = eventRaycast(event);
    const pending = pendingRecenterPointers.get(inputSource);
    const activate = boundaryOutOfBounds && (Boolean(pending?.hit) || tested.raycast.hit);
    logXr('xr_boundary_input_select', {
      inputPath: 'native XRSession event',
      inputSourceMode: inputSource.targetRayMode,
      profiles: inputSource.profiles ?? [],
      boundaryMode,
      raycast: tested.raycast,
      pointerDownHit: Boolean(pending?.hit),
      activationRequested: activate,
      headOffsetPolicy: 'recenter computes the delta from the tracked viewer centre, not the rig origin'
    }, handle);
    if (!activate || pending?.activated) return;
    const pressed = pending ?? { hit: true, inputSourceMode: inputSource.targetRayMode };
    pressed.activated = true;
    pendingRecenterPointers.set(inputSource, pressed);
    logXr('xr_boundary_recenter_button_activated', {
      inputSourceMode: inputSource.targetRayMode,
      raycast: tested.raycast,
      pointerDownRaycast: pending?.raycast ?? null,
      trackedViewpoint: latestBoundaryInspection?.sample ?? null,
      rigPosition: currentRigPoint(),
      explicitUserSelection: true
    }, handle);
    void recenterXR(`xr_${inputSource.targetRayMode}`).then(result => {
      logXr('xr_boundary_recenter_input_result', {
        inputSourceMode: inputSource.targetRayMode,
        result,
        boundaryState: getBoundaryState(),
        finalCenterAndEyeContainment: latestBoundaryInspection?.inspection ?? null
      }, handle);
    });
  };
  const onNativeSelectEnd = event => {
    const inputSource = event?.inputSource;
    if (!inputSource || !supportedPointer(inputSource)) return;
    const pending = pendingRecenterPointers.get(inputSource);
    logXr('xr_boundary_input_selectend', {
      inputPath: 'native XRSession event',
      inputSourceMode: inputSource.targetRayMode,
      pointerDownHit: Boolean(pending?.hit),
      recenterActivated: Boolean(pending?.activated),
      selectCompletedEventObserved: Boolean(pending?.activated)
    }, handle);
    if (!pending) return;
    clearTimeout(pending.cleanupTimer);
    pending.cleanupTimer = setTimeout(() => pendingRecenterPointers.delete(inputSource), 750);
  };
  const detachPointerSession = () => {
    if (!pointerSession) return;
    pointerSession.removeEventListener('selectstart', onNativeSelectStart);
    pointerSession.removeEventListener('select', onNativeSelect);
    pointerSession.removeEventListener('selectend', onNativeSelectEnd);
    pointerSession = null;
    pendingRecenterPointers = new WeakMap();
    logXr('xr_boundary_pointer_session_detached', {}, handle);
  };
  const attachPointerSession = () => {
    const session = handle.app.xr?.session;
    if (!session || pointerSession === session) return;
    detachPointerSession();
    pointerSession = session;
    session.addEventListener('selectstart', onNativeSelectStart);
    session.addEventListener('select', onNativeSelect);
    session.addEventListener('selectend', onNativeSelectEnd);
    logXr('xr_boundary_pointer_session_attached', {
      eventSource: 'native XRSession selectstart/select/selectend',
      inputModes: [...supportedPointerModes],
      control: 'camera-following PlayCanvas Recenter quad'
    }, handle);
  };
  handle.app.xr?.on('start', attachPointerSession);
  handle.app.xr?.on('end', detachPointerSession);
  if (handle.app.xr?.active) attachPointerSession();

  if (typeof navigationSelectFilter === 'function') {
    logXr('xr_boundary_pointer_control_installed', {
      navigationSelectFilterInstalled,
      raycastSource: 'Viewer xrNavigation._isSystemSelect receives PlayCanvas XRInputSource ray on selectstart',
      activationSource: 'native XRSession select event; transient-pointer included',
      preservesOtherSelectionInputs: true,
      onlyConsumesASelectionThatHitsTheVisibleRecenterControl: true
    }, handle);
  } else {
    logXr('xr_boundary_pointer_control_unavailable', {
      reason: 'The pinned Viewer XR navigation system-select hook was not available; native select diagnostics remain active'
    }, handle);
  }

  const getBoundaryState = () => ({
    buildId,
    active: Boolean(handle.app.xr?.active),
    outOfBounds: boundaryOutOfBounds,
    boundaryMode,
    warningVisible: boundaryOutOfBounds && (boundaryFeedback?.snapshot?.().cardOpacity ?? 0) >= 0.35,
    safeXrSpawnPoint,
    safeXrRigPosition,
    safeXrGroundHeight,
    latestBoundaryInspection,
    feedback: boundaryFeedback?.snapshot?.() ?? null,
    pointerControlAvailable: Boolean(boundaryFeedback?.ready && navigationSelectFilterInstalled),
    navigationGuardMode: navigation.mode,
    feedbackReady: Boolean(boundaryFeedback?.ready)
  });

  const fullDebugApi = {
    buildId,
    dump() {
      let records = persistentTrace;
      try {
        const stored = JSON.parse(localStorage.getItem(traceStorageKey) ?? '[]');
        if (Array.isArray(stored)) records = stored;
      } catch {
        // Return the in-memory trace when persistent storage cannot be read.
      }
      console.info('[visitor-xr-trace]', JSON.stringify(records, null, 2));
      return records;
    },
    clear() {
      persistentTrace = [];
      try { localStorage.removeItem(traceStorageKey); } catch { /* in-memory trace was still cleared */ }
      logXr('debug_trace_cleared', {}, handle);
    },
    snapshot,
    openingSpawnStatus: () => ({ ready: openingSpawnReady, resetSpawn }),
    boundaryState: getBoundaryState,
    recenter: source => recenterXR(source ?? 'xr_debug_api'),
    buildIntegrity: () => buildIntegrity,
    async end() {
      logXr('debug_api_end_requested', {
        source: 'window.__visitorXRDebug.end()',
        callsExistingViewerEndXR: true,
        snapshot: snapshot()
      }, handle);
      if (typeof handle.endXR !== 'function') {
        throw new Error('The current Viewer does not expose endXR().');
      }
      sessionEndSource = 'debug_api';
      try {
        const result = await handle.endXR();
        logXr('debug_api_end_resolved', { source: 'window.__visitorXRDebug.end()', result: result ?? null }, handle);
        return result;
      } catch (error) {
        logXr('debug_api_end_error', { error: describeError(error) }, handle);
        throw error;
      } finally {
        sessionEndSource = null;
      }
    }
  };
  window.__visitorXRDebug = diagnosticsEnabled ? fullDebugApi : {
    recenter: fullDebugApi.recenter,
    boundaryState: getBoundaryState
  };
  logXr('debug_api_installed', {
    global: 'window.__visitorXRDebug',
    enabledByConfiguration: diagnosticsEnabled,
    methods: diagnosticsEnabled
      ? ['dump', 'clear', 'snapshot', 'openingSpawnStatus', 'boundaryState', 'recenter', 'buildIntegrity', 'end']
      : ['recenter', 'boundaryState'],
    endDelegatesToExistingViewer: true,
    traceStorageKey
  }, handle);

  handle.events.on('inputEvent', (eventName, originalEvent) => {
    if (eventName !== 'reset') return;
    transitionStartedAt = performance.now();
    transitionTarget = resetSpawn;
    cameraTransitionState = 'camera-manager-reset-input-event-dispatched';
    lastTransitionObservation = {
      source: resetCallSource ?? 'viewer_input',
      state: cameraTransitionState,
      frameEndCount: handle.__visitorFrameStats.count,
      cameraManagerPoseOrTarget: readCameraManagerPose(handle, cameraEntity),
      renderedCameraPose: cameraPoseSignature(cameraEntity),
      at: new Date().toISOString()
    };
    logXr('camera_manager_reset_input_event_dispatched', {
      source: resetCallSource ?? 'viewer_input',
      eventName,
      originalEventType: originalEvent?.type ?? null,
      effectiveResetTarget: transitionTarget,
      frameEndCount: handle.__visitorFrameStats.count,
      cameraManagerPoseOrTarget: readCameraManagerPose(handle, cameraEntity),
      renderedCameraPose: cameraPoseSignature(cameraEntity),
      navigationMode: handle.state.cameraMode
    }, handle);
  });

  handle.events.on('navigateComplete', () => {
    cameraTransitionState = 'viewer-navigation-complete';
    lastTransitionObservation = {
      source: 'Viewer navigateComplete event',
      state: cameraTransitionState,
      cameraWorldPose: cameraPoseSignature(cameraEntity),
      cameraManagerPoseOrTarget: readCameraManagerPose(handle, cameraEntity),
      at: performance.now()
    };
    reportSnapshot('camera_transition_completed', 'viewer:navigateComplete');
  });

  const originalResetCamera = handle.resetCamera.bind(handle);
  handle.resetCamera = (...args) => {
    const source = resetCallSource ?? 'viewer_api';
    cameraTransitionState = 'requested';
    transitionStartedAt = performance.now();
    transitionTarget = resetSpawn;
    lastTransitionObservation = { source, state: cameraTransitionState, at: performance.now() };
    reportSnapshot('reset_camera_requested', source, { resetSpawn });
    try {
      const result = originalResetCamera(...args);
      logXr('reset_camera_returned', {
        source,
        immediateReturn: result ?? null,
        navigationMode: handle.state.cameraMode,
        transition: 'pending-camera-settle',
        cameraWorldPose: cameraPoseSignature(cameraEntity),
        cameraManagerPoseOrTarget: readCameraManagerPose(handle, cameraEntity),
        frameEndCount: handle.__visitorFrameStats.count
      }, handle);
      return result;
    } catch (error) {
      cameraTransitionState = 'error';
      lastTransitionObservation = { source, state: cameraTransitionState, error: describeError(error) };
      logXr('reset_camera_error', { source, error: describeError(error) }, handle);
      throw error;
    }
  };

  const updateClipPlanes = values => {
    if (!cameraEntity?.camera || !values) return;
    cameraEntity.camera.nearClip = values.cameraNearClip ?? values.nearClip;
    cameraEntity.camera.farClip = values.cameraFarClip ?? values.farClip;
  };

  const waitForXrTeardown = async () => {
    await nextAnimationFrame();
    await nextAnimationFrame();
    const started = performance.now();
    while (handle.app.xr?.active && performance.now() - started < 6000) {
      await delay(40);
    }
    if (handle.app.xr?.active) {
      throw new Error('XR teardown did not finish; desktop camera restoration was held to avoid racing the Viewer.');
    }
    await nextAnimationFrame();
    await nextAnimationFrame();
  };

  const beginCameraRecovery = async reason => {
    if (recoveryPromise) {
      logXr('recovery_event_coalesced', { source: reason, phase }, handle);
      return recoveryPromise;
    }
    if (!entrySnapshot || phase === 'desktop') {
      logXr('recovery_event_ignored', { source: reason, phase }, handle);
      return;
    }

    const exitSource = sessionEndSource ?? (phase === 'exiting' ? 'visitor_button' : 'system_or_browser');

    phase = 'restoring';
    pendingAction = null;
    cameraTransitionState = 'waiting-for-xr-teardown';
    lastTransitionObservation = { source: reason, state: cameraTransitionState, at: performance.now() };
    reportSnapshot('recovery_started', reason, { exitSource });
    update();

    recoveryPromise = (async () => {
      const priorAutoRender = entrySnapshot.snapshot.rendering.autoRender;
      try {
        await waitForXrTeardown();
        if (entryCanvasParent && canvas.parentElement !== entryCanvasParent) {
          entryCanvasParent.prepend(canvas);
          logXr('canvas_parent_restored', {
            source: reason,
            canvasParent: canvas.parentElement?.id || canvas.parentElement?.className || null
          }, handle);
        }

        handle.app.autoRender = true;
        handle.app.renderNextFrame = true;
        const firstFrame = await waitForFrameEnd(handle.app, 3000);
        const secondFrame = await waitForFrameEnd(handle.app, 3000);
        cameraTransitionState = 'observing-desktop-pose';
        const desktopPoseSettle = await waitForCameraSettled(handle.app, cameraEntity, 0, 2500);
        lastTransitionObservation = {
          source: reason,
          state: desktopPoseSettle.settled ? 'desktop-pose-settled' : 'desktop-pose-not-settled',
          settle: desktopPoseSettle
        };
        const currentPose = cameraPoseSignature(cameraEntity);
        const entryPose = entrySnapshot.snapshot.cameraWorldPose;
        const difference = poseDifference(entryPose, currentPose);
        const poseValid = navigation.guard?.isValid(currentPose?.position ?? []) ?? true;
        logXr('camera_manager_after_xr_sync', {
          source: reason,
          managerPoseAvailable: readCameraManagerPose(handle, cameraEntity).available,
          synchronizedCameraPose: currentPose,
          entryPose,
          difference,
          poseValid,
          desktopPoseSettle,
          framesRendered: Number(firstFrame) + Number(secondFrame),
          snapshot: snapshot()
        }, handle);

        if (!firstFrame || !secondFrame) {
          throw new Error('The desktop renderer did not produce the required post-XR frames.');
        }

        if (!desktopPoseSettle.settled || typeof difference !== 'object' || !difference ||
            difference.position > 0.1 || difference.angle > 0.035 || !poseValid) {
          handle.state.inputEnabled = true;
          handle.state.cameraMode = 'fly';
          resetCallSource = 'xr_exit_recovery';
          const result = handle.resetCamera();
          resetCallSource = null;
          const settle = resetSpawn
            ? await waitForConfiguredSpawn({
              app: handle.app,
              cameraEntity,
              resetSpawn,
              getCameraManagerState: () => handle.getCameraManagerState?.() ?? null,
              timeoutMs: 7000,
              minimumElapsedMs: 1000
            })
            : await waitForCameraSettled(handle.app, cameraEntity, 1050, 5000);
          const matchesSpawn = matchesResetSpawn(cameraEntity, resetSpawn);
          const invariant = sceneTransformInvariant(
            handle.app.root.findComponents('gsplat')?.[0]?.entity ?? null,
            handle.__visitorExpectedSceneTransform
          );
          const failureReasons = configuredResetFailureReasons(settle, true, matchesSpawn, invariant);
          logXr('xr_exit_reset_fallback', {
            source: reason,
            immediateReturn: result ?? null,
            settle,
            resetSpawn,
            matchesResetSpawn: matchesSpawn,
            failureReasons,
            sceneTransformInvariant: invariant,
            snapshot: snapshot()
          }, handle);
          if (failureReasons.length) {
            throw new Error(`The Viewer camera reset after XR exit failed: ${failureReasons.join(', ')}.`);
          }
          cameraTransitionState = 'reset-settled';
          lastTransitionObservation = { source: reason, state: cameraTransitionState, settle };
        } else {
          cameraTransitionState = 'desktop-pose-settled';
        }

        updateClipPlanes(entrySnapshot.snapshot.rendering);
        handle.app.renderNextFrame = true;
        const clipRestoreFrame = await waitForFrameEnd(handle.app, 3000);
        if (!clipRestoreFrame) throw new Error('The desktop renderer did not present after restoring clip planes.');
        const finalSnapshot = snapshot();
        if (!finalSnapshot.sceneTransformInvariant?.matches) {
          openingSpawnReady = false;
          throw new Error('The configured SOG transform changed during XR and failed its home-transform invariant.');
        }
        logXr('rendering_after_session_termination', {
          source: reason,
          exitSource,
          restoredNearClip: finalSnapshot.rendering.cameraNearClip,
          restoredFarClip: finalSnapshot.rendering.cameraFarClip,
          entryNearClip: entrySnapshot.snapshot.rendering.cameraNearClip,
          entryFarClip: entrySnapshot.snapshot.rendering.cameraFarClip,
          cameraMatchesEntry: Boolean(difference && difference.position <= 0.1 && difference.angle <= 0.035),
          cameraMatchesResetSpawn: matchesResetSpawn(cameraEntity, resetSpawn),
          sceneTransformInvariant: finalSnapshot.sceneTransformInvariant,
          snapshot: finalSnapshot
        }, handle);

        phase = 'desktop';
        entrySnapshot = null;
        entryCanvasParent = null;
        lastErrorAction = !xrBoundarySafe() || navigation.mode === 'locomotion-disabled'
          ? 'navigation'
          : null;
        cameraTransitionState = 'desktop-ready';
        handle.app.autoRender = priorAutoRender;
        handle.app.renderNextFrame = true;
        logXr('recovery_completed', {
          source: reason,
          exitSource,
          rendering: renderingState(handle, cameraEntity, null),
          cameraWorldPose: cameraPoseSignature(cameraEntity),
          cameraManagerPoseOrTarget: readCameraManagerPose(handle, cameraEntity),
          sceneTransformInvariant: sceneTransformInvariant(
            handle.app.root.findComponents('gsplat')?.[0]?.entity ?? null,
            handle.__visitorExpectedSceneTransform
          ),
          navigationMode: handle.state.cameraMode
        }, handle);
        sessionEndSource = null;
        update();
      } catch (error) {
        openingSpawnReady = false;
        lastErrorAction = 'exit';
        cameraTransitionState = 'recovery-error';
        lastTransitionObservation = { source: reason, state: cameraTransitionState, error: describeError(error) };
        logXr('recovery_error', {
          source: reason,
          exitSource,
          error: describeError(error),
          snapshot: snapshot()
        }, handle);
        sessionEndSource = null;
        if (!handle.app.xr?.active) {
          phase = 'desktop';
          handle.app.autoRender = true;
          handle.app.renderNextFrame = true;
          update();
        }
      }
    })().finally(() => {
      recoveryPromise = null;
    });
    return recoveryPromise;
  };

  const runResetTransaction = source => {
    if (resetPromise) return resetPromise;
    if (phase !== 'desktop') {
      logXr('reset_camera_rejected', { source, reason: 'XR lifecycle is not in desktop state', phase }, handle);
      return Promise.resolve(false);
    }
    const priorAutoRender = handle.app.autoRender;
    resetPromise = (async () => {
      handle.app.autoRender = true;
      handle.app.renderNextFrame = true;
      handle.state.inputEnabled = true;
      handle.state.cameraMode = 'fly';
      resetCallSource = source;
      try {
        handle.resetCamera();
      } finally {
        resetCallSource = null;
      }
      const settle = await waitForConfiguredSpawn({
        app: handle.app,
        cameraEntity,
        resetSpawn,
        getCameraManagerState: () => handle.getCameraManagerState?.() ?? null,
        timeoutMs: 7000,
        minimumElapsedMs: 1000
      });
      handle.app.renderNextFrame = true;
      const presented = await waitForFrameEnd(handle.app, 3000);
      const matchesSpawn = matchesResetSpawn(cameraEntity, resetSpawn);
      const invariant = sceneTransformInvariant(
        handle.app.root.findComponents('gsplat')?.[0]?.entity ?? null,
        handle.__visitorExpectedSceneTransform
      );
      const failureReasons = configuredResetFailureReasons(settle, presented, matchesSpawn, invariant);
      cameraTransitionState = settle.settled ? 'reset-settled' : 'reset-not-settled';
      lastTransitionObservation = { source, state: cameraTransitionState, settle, matchesSpawn, presented, failureReasons };
      logXr('reset_camera_result', {
        source,
        settle,
        presented,
        matchesSpawn,
        failureReasons,
        resetSpawn,
        cameraWorldPose: cameraPoseSignature(cameraEntity),
        cameraManagerPoseOrTarget: readCameraManagerPose(handle, cameraEntity),
        sceneTransform: entityTransform(handle.app.root.findComponents('gsplat')?.[0]?.entity ?? null),
        sceneTransformInvariant: invariant,
        xrState: { mode: handle.state.xrMode ?? null, active: Boolean(handle.app.xr?.active) },
        visibilityState: document.visibilityState,
        frameEndCount: handle.__visitorFrameStats.count,
        navigationMode: handle.state.cameraMode,
        rendering: renderingState(handle, cameraEntity, null)
      }, handle);
      const succeeded = failureReasons.length === 0;
      openingSpawnReady = succeeded;
      lastErrorAction = succeeded
        ? (!xrBoundarySafe() || navigation.mode === 'locomotion-disabled' ? 'navigation' : null)
        : 'reset';
      return succeeded;
    })().catch(error => {
      logXr('reset_camera_error', { source, error: describeError(error) }, handle);
      openingSpawnReady = false;
      lastErrorAction = 'reset';
      cameraTransitionState = 'reset-error';
      lastTransitionObservation = { source, state: cameraTransitionState, error: describeError(error) };
      return false;
    }).finally(() => {
      handle.app.autoRender = priorAutoRender;
      handle.app.renderNextFrame = true;
      resetPromise = null;
      transitionStartedAt = null;
      update();
    });
    update();
    return resetPromise;
  };

  const prepareOpeningSpawn = () => {
    if (openingSpawnPreparationPromise) return openingSpawnPreparationPromise;
    if (openingSpawnReady) return Promise.resolve(true);
    const priorAutoRender = handle.app.autoRender;
    openingSpawnPreparationPromise = (async () => {
      handle.app.autoRender = true;
      handle.app.renderNextFrame = true;
      handle.state.inputEnabled = true;
      handle.state.cameraMode = 'fly';
      resetCallSource = 'opening_spawn_initialization';
      try {
        handle.resetCamera();
      } finally {
        resetCallSource = null;
      }
      const settle = await waitForConfiguredSpawn({
        app: handle.app,
        cameraEntity,
        resetSpawn,
        getCameraManagerState: () => handle.getCameraManagerState?.() ?? null,
        timeoutMs: 10000,
        minimumElapsedMs: 1000,
        onSample: sample => {
          if (sample.frames === 1 || sample.readyFrames === 1 || sample.readyFrames === 3) {
            logXr('opening_spawn_settle_sample', {
              ...sample,
              cameraWorldPose: cameraPoseSignature(cameraEntity),
              cameraLocalTransform: entityTransform(cameraEntity),
              rigTransform: entityTransform(cameraAndRig(handle).rigEntity),
              sceneTransform: entityTransform(sceneEntity),
              sceneTransformInvariant: sceneTransformInvariant(sceneEntity, handle.__visitorExpectedSceneTransform)
            }, handle);
          }
        }
      });
      handle.app.renderNextFrame = true;
      const presented = await waitForFrameEnd(handle.app, 3000);
      const matchesSpawn = matchesResetSpawn(cameraEntity, resetSpawn);
      const invariant = sceneTransformInvariant(sceneEntity, handle.__visitorExpectedSceneTransform);
      const failureReasons = configuredResetFailureReasons(settle, presented, matchesSpawn, invariant);
      openingSpawnReady = failureReasons.length === 0;
      if (!openingSpawnReady) {
        lastErrorAction = 'reset';
        cameraTransitionState = 'opening-spawn-not-settled';
        lastTransitionObservation = { source: 'opening_spawn_initialization', settle, failureReasons };
      } else {
        lastErrorAction = !xrBoundarySafe() ? 'navigation' : null;
        cameraTransitionState = 'opening-spawn-settled';
        lastTransitionObservation = { source: 'opening_spawn_initialization', settle, matchesSpawn, presented };
      }
      logXr('opening_spawn_result', {
        settle,
        presented,
        matchesSpawn,
        failureReasons,
        expectedResetSpawn: resetSpawn,
        renderedCameraPose: cameraPoseSignature(cameraEntity),
        cameraManagerPoseOrTarget: readCameraManagerPose(handle, cameraEntity),
        sceneTransform: entityTransform(sceneEntity),
        sceneTransformInvariant: invariant,
        frameEndCount: handle.__visitorFrameStats.count,
        rendering: renderingState(handle, cameraEntity, null)
      }, handle);
      return openingSpawnReady;
    })().catch(error => {
      openingSpawnReady = false;
      lastErrorAction = 'reset';
      cameraTransitionState = 'opening-spawn-error';
      lastTransitionObservation = { source: 'opening_spawn_initialization', error: describeError(error) };
      logXr('opening_spawn_error', { error: describeError(error), snapshot: snapshot() }, handle);
      return false;
    }).finally(() => {
      handle.app.renderNextFrame = true;
      handle.app.autoRender = priorAutoRender;
      openingSpawnPreparationPromise = null;
      update();
    });
    update();
    return openingSpawnPreparationPromise;
  };

  handle.prepareOpeningSpawn = prepareOpeningSpawn;
  if (diagnosticsEnabled) window.__visitorXRDebug.prepareOpeningSpawn = prepareOpeningSpawn;

  resetButton?.addEventListener('click', event => {
    event.preventDefault();
    event.stopImmediatePropagation();
    if (resetPromise || phase !== 'desktop') return;
    void runResetTransaction('visitor_reset_button');
    const canvasElement = handle.app.graphicsDevice.canvas;
    if (canvasElement) {
      canvasElement.tabIndex = 0;
      canvasElement.focus?.({ preventScroll: true });
    }
  }, true);

  handle.events.on('hasVR:changed', (value, previous) => {
    logXr('capability_changed', { property: 'hasVR', value, previous }, handle);
    update();
  });
  handle.events.on('canStartVR:changed', (value, previous) => {
    logXr('capability_changed', { property: 'canStartVR', value, previous }, handle);
    update();
  });
  handle.events.on('xrMode:changed', (mode, previousMode) => {
    logXr('xr_mode_changed', {
      mode,
      previousMode,
      exitSource: previousMode === 'vr'
        ? sessionEndSource ?? (phase === 'exiting' ? 'visitor_button' : 'system_or_browser')
        : null,
      lifecyclePhase: phase,
      snapshot: snapshot()
    }, handle);
    if (mode === 'vr') {
      phase = 'in-vr';
      cameraTransitionState = 'suspended-in-xr';
      pendingAction = null;
      lastErrorAction = !xrBoundarySafe() || navigation.mode === 'locomotion-disabled'
        ? 'navigation'
        : null;
      if (!entrySnapshot) {
        entryCanvasParent = canvas?.parentElement ?? null;
        entrySnapshot = {
          snapshot: snapshot(),
          resetSpawn
        };
        logXr('entry_snapshot_late', { snapshot: entrySnapshot.snapshot }, handle);
      }
      update();
    } else if (previousMode === 'vr') {
      void beginCameraRecovery('xrMode:changed');
      update();
    }
  });
  handle.app.xr?.on('start', () => {
    phase = 'in-vr';
    sceneEnabledBeforeXr = sceneEntity?.enabled ?? true;
    boundaryOutOfBounds = false;
    boundaryMode = 'inside';
    safeXrSpawnPoint = configuredSafeSpawnPoint?.slice() ?? null;
    safeXrGroundHeight = Number.isFinite(navigation.navigation?._currentGroundY)
      ? navigation.navigation._currentGroundY
      : null;
    safeXrRigPosition = currentRigPoint();
    if (safeXrSpawnPoint) {
      logXr('xr_safe_spawn_configured', {
        point: safeXrSpawnPoint,
        source: 'accepted Visitor opening camera position, validated against the existing enclosure clearance',
        boundaryInspection: navigation.guard.inspect(safeXrSpawnPoint),
        trackedCameraLocalPoseWillBePreserved: true,
        openingGroundHeight: safeXrGroundHeight,
        openingRigPosition: safeXrRigPosition
      }, handle);
    }
    latestBoundaryInspection = null;
    lastHeadPose = null;
    lastRigPosition = null;
    lastTrackedHeadOutOfBounds = false;
    boundaryFeedback?.reset();
    xrSessionStartedAt = performance.now();
    firstTrackingFrameRecorded = false;
    oneSecondPoseRecorded = false;
    floorPlacementStarted = false;
    floorPlacementCompleted = false;
    pendingAction = null;
    reportSnapshot('xr_session_started', 'app.xr:start', {
      sessionStartSource: 'visitor_button_or_viewer',
      sessionStartedAt: new Date().toISOString(),
      referenceSpaceType: handle.app.xr?.referenceSpaceType ?? null,
      referenceSpace: {
        available: Boolean(handle.app.xr?._referenceSpace),
        constructor: handle.app.xr?._referenceSpace?.constructor?.name ?? null
      },
      floorPlacementPolicy: handle.state.hasCollision
        ? 'collision geometry supplies floorBelow; no asynchronous splat-floor placement'
        : 'Viewer may perform asynchronous splat-floor placement'
    });
    if (handle.state.hasCollision) {
      logXr('xr_floor_placement_policy', {
        mode: 'synchronous-existing-collision-floor',
        asynchronousSplatFloorQueryExpected: false
      }, handle);
    }
    update();
  });
  handle.app.xr?.on('end', () => {
    if (sceneEntity) sceneEntity.enabled = sceneEnabledBeforeXr;
    boundaryOutOfBounds = false;
    boundaryMode = 'inside';
    boundaryFeedback?.reset();
    safeXrSpawnPoint = null;
    safeXrGroundHeight = null;
    safeXrRigPosition = null;
    latestBoundaryInspection = null;
    reportSnapshot('xr_session_ended', 'app.xr:end', {
      exitSource: sessionEndSource ?? (phase === 'exiting' ? 'visitor_button' : 'system_or_browser')
    });
    void beginCameraRecovery('app.xr:end');
  });

  button.addEventListener('click', () => {
    const active = handle.state.xrMode === 'vr';
    if (resetPromise || pendingAction || (phase !== 'desktop' && !(phase === 'in-vr' && active))) return;
    const action = active ? 'exit' : 'enter';
    if (!active && !handle.state.canStartVR) {
      logXr('session_entry_unavailable', { reason: 'canStartVR is false' }, handle);
      update();
      return;
    }
    if (!xrBoundarySafe()) {
      logXr('session_entry_unavailable', {
        reason: 'The XR rig guard or in-session tracked-viewpoint feedback is unavailable',
        navigationMode: navigation.mode,
        boundaryFeedbackReady: Boolean(boundaryFeedback?.ready)
      }, handle);
      lastErrorAction = 'enter';
      update();
      return;
    }

    if (action === 'enter' && typeof handle.startXR !== 'function') {
      lastErrorAction = 'enter';
      logXr('session_error', { action, error: describeError(new Error('The loaded Viewer does not expose startXR().')) }, handle);
      update();
      return;
    }
    if (action === 'exit' && typeof handle.endXR !== 'function') {
      lastErrorAction = 'exit';
      logXr('session_error', { action, error: describeError(new Error('The loaded Viewer does not expose endXR().')) }, handle);
      update();
      return;
    }

    status.hidden = true;
    lastErrorAction = !xrBoundarySafe() || navigation.mode === 'locomotion-disabled'
      ? 'navigation'
      : null;
    pendingAction = action;
    if (action === 'enter') {
      phase = 'entering';
      entryCanvasParent = canvas?.parentElement ?? null;
      entrySnapshot = {
        snapshot: snapshot(),
        snapshotTime: performance.now(),
        resetSpawn
      };
      reportSnapshot('session_start_requested', 'visitor_button', {
        resetSpawn,
        navigationMode: handle.state.cameraMode,
        guardMode: navigation.mode
      });
    } else {
      phase = 'exiting';
      sessionEndSource = 'visitor_button';
      logXr('session_end_requested', {
        mode: 'vr',
        source: 'visitor_button',
        snapshot: snapshot()
      }, handle);
    }
    update();

    let request;
    try {
      if (action === 'enter') {
        reportSnapshot('before_startXR', 'visitor_button_before_handle.startXR', {
          method: 'viewer.startXR("vr")',
          cameraManagerPoseOrTarget: readCameraManagerPose(handle, cameraEntity),
          renderedCameraPose: cameraPoseSignature(cameraEntity),
          sceneTransform: entityTransform(sceneEntity)
        });
      }
      request = action === 'exit' ? handle.endXR() : handle.startXR('vr');
    } catch (error) {
      lastErrorAction = action;
      cameraTransitionState = action === 'enter' ? 'entry-error' : 'exit-error';
      lastTransitionObservation = { source: 'visitor_button', state: cameraTransitionState, error: describeError(error) };
      pendingAction = null;
      phase = 'desktop';
      entrySnapshot = null;
      entryCanvasParent = null;
      if (action === 'exit') sessionEndSource = null;
      logXr('session_error', { action, error: describeError(error) }, handle);
      update();
      return;
    }

    Promise.resolve(request).then(() => {
      pendingAction = null;
      logXr(action === 'enter' ? 'session_start_resolved' : 'session_end_resolved', {
        mode: 'vr',
        source: 'visitor_button',
        lifecyclePhase: phase
      }, handle);
      if (action === 'enter' && phase === 'entering') phase = 'in-vr';
      update();
    }).catch(error => {
      if (action === 'exit') sessionEndSource = null;
      lastErrorAction = action;
      if (action === 'enter' && handle.state.xrMode !== 'vr') {
        cameraTransitionState = 'entry-error';
        lastTransitionObservation = { source: 'visitor_button', state: cameraTransitionState, error: describeError(error) };
      }
      pendingAction = null;
      logXr('session_error', { action, error: describeError(error), snapshot: snapshot() }, handle);
      if (action === 'enter' && handle.state.xrMode !== 'vr') {
        phase = 'desktop';
        entrySnapshot = null;
        entryCanvasParent = null;
      } else if (action === 'exit' && handle.state.xrMode === 'vr') {
        phase = 'in-vr';
      }
      update();
    });
  });

  languageSelect?.addEventListener('change', () => setTimeout(() => {
    boundaryFeedback?.refreshText();
    update();
  }, 0));
  logXr('renderer_compatibility', {
    requestedRenderer: plan.renderer,
    actualRenderer: handle.app.graphicsDevice?.isWebGPU ? 'webgpu' : handle.app.graphicsDevice?.isWebGL2 ? 'webgl2' : 'unknown',
    immersiveVrSupported: plan.immersiveVrSupported,
    rendererSupportsVR: Boolean(handle.state.canStartVR),
    reason: plan.reason,
    collisionGeometryError: geometryError
  }, handle);
  logXr('capability', {
    xrApiAvailable: plan.xrApiAvailable,
    immersiveVrSupported: plan.immersiveVrSupported,
    buildIntegrityValid: plan.buildIntegrityValid,
    rendererSupportsVR: Boolean(handle.state.canStartVR),
    navigationGuardSafe: navigation.safe,
    navigationGuardMode: navigation.mode
  }, handle);
  update();
}

function matchesResetSpawn(cameraEntity, resetSpawn) {
  if (!cameraEntity || !resetSpawn?.position || !resetSpawn?.target) return false;
  const signature = cameraPoseSignature(cameraEntity);
  const positionError = Math.hypot(
    signature.position[0] - resetSpawn.position[0],
    signature.position[1] - resetSpawn.position[1],
    signature.position[2] - resetSpawn.position[2]
  );
  const forward = readVector(cameraEntity.forward);
  const expected = [
    resetSpawn.target[0] - resetSpawn.position[0],
    resetSpawn.target[1] - resetSpawn.position[1],
    resetSpawn.target[2] - resetSpawn.position[2]
  ];
  const forwardLength = Math.hypot(...forward);
  const expectedLength = Math.hypot(...expected);
  const alignment = forwardLength && expectedLength
    ? (forward[0] * expected[0] + forward[1] * expected[1] + forward[2] * expected[2]) / (forwardLength * expectedLength)
    : -1;
  return positionError <= 0.05 && alignment >= Math.cos(Math.PI / 180);
}
