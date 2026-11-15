const POSITION_TOLERANCE = 0.05;
const HEADING_TOLERANCE_RADIANS = Math.PI / 180;
const TRANSITION_COMPLETE = 0.99999;

export function configuredResetFailureReasons(settle, presented, matchesSpawn, sceneInvariant) {
  const reasons = [];
  if (!settle?.settled) reasons.push(settle?.reason ?? 'camera_transition_not_settled');
  if (!presented) reasons.push('render_frame_not_presented');
  if (!matchesSpawn) {
    const inspection = settle?.lastInspection;
    const inspectionReasonStart = reasons.length;
    if (!inspection) {
      reasons.push('reset_spawn_mismatch');
    } else {
      if (!inspection.progressAvailable) reasons.push('camera_manager_state_unavailable');
      else if (!inspection.transitionComplete) reasons.push('camera_manager_transition_incomplete');
      if (!inspection.managerTargetAvailable) reasons.push('camera_manager_target_unavailable');
      else if (!inspection.targetMatches) reasons.push('camera_manager_target_mismatch');
      if (!inspection.positionMatches) reasons.push('spawn_position_mismatch');
      if (!inspection.headingMatches) reasons.push('spawn_heading_mismatch');
      if (!inspection.fovMatches) reasons.push('spawn_fov_mismatch');
      if (reasons.length === inspectionReasonStart) reasons.push('reset_spawn_mismatch');
    }
  }
  if (sceneInvariant && !sceneInvariant.matches) reasons.push('scene_transform_mismatch');
  return [...new Set(reasons)];
}

function vector(value) {
  if (Array.isArray(value)) return value.length >= 3 ? value.slice(0, 3).map(Number) : null;
  if (!value || ![value.x, value.y, value.z].every(Number.isFinite)) return null;
  return [value.x, value.y, value.z];
}

export function inspectConfiguredSpawn(cameraEntity, resetSpawn, cameraManagerState) {
  const position = vector(cameraEntity?.getPosition?.());
  const forward = vector(cameraEntity?.forward);
  const expectedPosition = vector(resetSpawn?.position);
  const target = vector(resetSpawn?.target);
  const targetPosition = vector(cameraManagerState?.targetPose?.position);
  const progress = cameraManagerState?.transitionProgress;
  const expectedFov = Number(resetSpawn?.fov);
  const renderedFov = Number(cameraEntity?.camera?.fov);

  let positionError = Infinity;
  let targetPositionError = Infinity;
  let headingErrorRadians = Infinity;
  if (position && expectedPosition) {
    positionError = Math.hypot(...position.map((value, index) => value - expectedPosition[index]));
  }
  if (targetPosition && expectedPosition) {
    targetPositionError = Math.hypot(...targetPosition.map((value, index) => value - expectedPosition[index]));
  }
  if (forward && target && expectedPosition) {
    const expected = target.map((value, index) => value - expectedPosition[index]);
    const forwardLength = Math.hypot(...forward);
    const expectedLength = Math.hypot(...expected);
    if (forwardLength > 0 && expectedLength > 0) {
      const alignment = forward.reduce((sum, value, index) => sum + value * expected[index], 0) /
        (forwardLength * expectedLength);
      headingErrorRadians = Math.acos(Math.max(-1, Math.min(1, alignment)));
    }
  }

  const progressAvailable = typeof progress === 'number' && Number.isFinite(progress);
  const managerTargetAvailable = Boolean(targetPosition);
  const positionMatches = positionError <= POSITION_TOLERANCE;
  const targetMatches = managerTargetAvailable && targetPositionError <= POSITION_TOLERANCE;
  const headingMatches = headingErrorRadians <= HEADING_TOLERANCE_RADIANS;
  const fovAvailable = Number.isFinite(expectedFov) && Number.isFinite(renderedFov);
  const fovError = fovAvailable ? Math.abs(renderedFov - expectedFov) : null;
  const fovMatches = !Number.isFinite(expectedFov) || (fovAvailable && fovError <= 0.1);
  const transitionComplete = progressAvailable && progress >= TRANSITION_COMPLETE;
  return {
    ready: transitionComplete && targetMatches && positionMatches && headingMatches && fovMatches,
    transitionComplete,
    progressAvailable,
    managerTargetAvailable,
    progress: progressAvailable ? progress : null,
    positionError,
    targetPositionError,
    headingErrorRadians,
    renderedFov: fovAvailable ? renderedFov : null,
    expectedFov: Number.isFinite(expectedFov) ? expectedFov : null,
    fovError,
    positionTolerance: POSITION_TOLERANCE,
    headingToleranceRadians: HEADING_TOLERANCE_RADIANS,
    positionMatches,
    targetMatches,
    headingMatches,
    fovMatches,
    renderedPosition: position,
    expectedPosition,
    renderedForward: forward,
    expectedTarget: target
  };
}

function nextFrameEnd(app, timeoutMs) {
  return new Promise(resolve => {
    let finished = false;
    const finish = rendered => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      app.off?.('frameend', onFrameEnd);
      resolve(rendered);
    };
    const onFrameEnd = () => finish(true);
    const timer = setTimeout(() => finish(false), timeoutMs);
    app.on('frameend', onFrameEnd);
  });
}

export async function waitForConfiguredSpawn({
  app,
  cameraEntity,
  resetSpawn,
  getCameraManagerState,
  timeoutMs = 7000,
  minimumElapsedMs = 1000,
  requiredFrames = 3,
  onSample = null
}) {
  const started = performance.now();
  let frames = 0;
  let readyFrames = 0;
  let lastInspection = null;

  while (performance.now() - started < timeoutMs) {
    const remaining = timeoutMs - (performance.now() - started);
    if (!await nextFrameEnd(app, Math.min(1500, remaining))) {
      if (performance.now() - started < timeoutMs - 5) {
        return {
          settled: false,
          reason: 'renderer_no_frames',
          frames,
          readyFrames,
          lastInspection,
          elapsedMs: performance.now() - started
        };
      }
      break;
    }
    frames++;
    let managerState = null;
    try {
      managerState = getCameraManagerState?.() ?? null;
    } catch {
      managerState = null;
    }
    lastInspection = inspectConfiguredSpawn(cameraEntity, resetSpawn, managerState);
    readyFrames = lastInspection.ready ? readyFrames + 1 : 0;
    onSample?.({ frames, readyFrames, managerState, inspection: lastInspection });
    if (performance.now() - started >= minimumElapsedMs && readyFrames >= requiredFrames) {
      return {
        settled: true,
        reason: 'configured-spawn-and-camera-manager-settled',
        frames,
        readyFrames,
        managerState,
        lastInspection,
        elapsedMs: performance.now() - started
      };
    }
  }

  const reason = !lastInspection?.progressAvailable
    ? 'camera_manager_state_unavailable'
    : !lastInspection?.transitionComplete
      ? 'camera_manager_transition_incomplete'
    : !lastInspection?.managerTargetAvailable
      ? 'camera_manager_target_unavailable'
      : !lastInspection?.targetMatches
        ? 'camera_manager_target_mismatch'
        : !lastInspection?.positionMatches
          ? 'spawn_position_mismatch'
          : !lastInspection?.headingMatches
            ? 'spawn_heading_mismatch'
            : 'spawn_fov_mismatch';
  return {
    settled: false,
    reason,
    frames,
    readyFrames,
    managerState: (() => {
      try { return getCameraManagerState?.() ?? null; } catch { return null; }
    })(),
    lastInspection,
    elapsedMs: performance.now() - started
  };
}
