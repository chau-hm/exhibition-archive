function readPoint(value) {
  if (!value) return null;
  const point = [value.x, value.y, value.z];
  return point.every(component => typeof component === 'number' && Number.isFinite(component))
    ? point
    : null;
}

function validVector(value) {
  return Array.isArray(value) && value.length === 3 && value.every(Number.isFinite);
}

/**
 * Provide the already accepted Visitor opening camera as a recovery target
 * before the first tracked frame has been classified. This makes recovery
 * available even when tracking starts outside the room on the first frame.
 */
export function configuredSafeXrSpawnPoint(guard, resetSpawn) {
  const point = resetSpawn?.position;
  if (!validVector(point) || typeof guard?.isValid !== 'function' || !guard.isValid(point)) return null;
  return point.slice();
}

/** Preserve the floor anchor relative to a rig recenter translation. */
export function computeRecenterGroundHeight(openingGroundHeight, openingRigPosition, targetRigPosition) {
  if (!Number.isFinite(openingGroundHeight) || !validVector(openingRigPosition) || !validVector(targetRigPosition)) {
    return { valid: false, groundHeight: null };
  }
  return {
    valid: true,
    groundHeight: openingGroundHeight + targetRigPosition[1] - openingRigPosition[1]
  };
}

/**
 * Read the tracked head centre and the actual WebXR eye positions in the same
 * PlayCanvas world frame used by the enclosure mesh. The XR manager writes the
 * viewer pose to the camera entity and exposes the per-eye XRViews on its camera.
 */
export function sampleXrViewpoints(cameraEntity, rigEntity) {
  const center = readPoint(cameraEntity?.getPosition?.());
  if (!center) return { center: null, eyes: [], source: 'unavailable' };

  const cameraLocal = readPoint(cameraEntity?.getLocalPosition?.());
  const rigWorld = rigEntity?.getWorldTransform?.();
  const xrViews = cameraEntity?.camera?.xrViews ?? [];
  const eyes = [];
  for (const view of xrViews) {
    const rawView = view?._xrView ?? view;
    const localEye = readPoint(rawView?.transform?.position);
    const eye = rawView?.eye;
    if (!localEye || (eye !== 'left' && eye !== 'right') || !rigWorld?.transformPoint) continue;
    const worldEye = readPoint(rigWorld.transformPoint({
      x: localEye[0],
      y: localEye[1],
      z: localEye[2]
    }));
    if (worldEye) eyes.push({ eye, position: worldEye });
  }

  return {
    center,
    eyes,
    source: eyes.length >= 2
      ? 'PlayCanvas XR viewer-pose centre plus left/right XRView world poses'
      : 'PlayCanvas XR camera entity world position; per-eye XRView poses unavailable',
    cameraLocal,
    eyeOffsetsIncluded: eyes.length >= 2,
    referenceSpace: 'local-floor XR reference space transformed through the XR rig world matrix'
  };
}

/**
 * Compute the one-time XR rig translation that moves the tracked eye centre to
 * an explicit safe viewpoint while leaving the headset's current orientation
 * and local tracking pose untouched.
 */
export function computeRecenterRigPosition(currentRig, currentViewpoint, targetViewpoint) {
  if (!validVector(currentRig) || !validVector(currentViewpoint) || !validVector(targetViewpoint)) {
    return { valid: false, reason: 'invalid-position' };
  }
  const translation = targetViewpoint.map((value, index) => value - currentViewpoint[index]);
  return {
    valid: true,
    translation,
    position: currentRig.map((value, index) => value + translation[index]),
    targetViewpoint: targetViewpoint.slice()
  };
}

export function inspectTrackedViewpoints(guard, sample) {
  if (!guard || typeof guard.inspect !== 'function' || !sample?.center) {
    return {
      valid: false,
      reason: 'guard-or-viewpoint-unavailable',
      center: null,
      eyes: [],
      minimumClearance: null
    };
  }
  const center = guard.inspect(sample.center);
  const eyes = (sample.eyes ?? []).map(({ eye, position }) => ({
    eye,
    position: position?.slice?.() ?? position,
    ...guard.inspect(position)
  }));
  const clearances = [center, ...eyes]
    .map(result => result.clearance)
    .filter(Number.isFinite);
  const valid = center.valid && eyes.every(result => result.valid);
  return {
    valid,
    reason: valid ? null : center.reason ?? eyes.find(result => !result.valid)?.reason ?? 'outside enclosure',
    center,
    eyes,
    minimumClearance: clearances.length ? Math.min(...clearances) : null,
    checkedEyeCount: eyes.length,
    stereoCheckApplied: eyes.length >= 2
  };
}

/**
 * Classify tracked-viewpoint feedback with separate enter and release thresholds.
 * `clearance` is measured after the existing camera-radius margin is applied.
 */
export function nextBoundaryFeedbackMode(clearance, previousMode = 'inside', options = {}) {
  const enterClearance = Number.isFinite(options.outsideEnterClearance)
    ? options.outsideEnterClearance
    : 0;
  const releaseClearance = Math.max(
    enterClearance,
    Number.isFinite(options.outsideExitClearance) ? options.outsideExitClearance : 0.18
  );
  const approachDistance = Math.max(
    enterClearance + 1e-3,
    Number.isFinite(options.approachDistance) ? options.approachDistance : 0.85
  );
  const maximumVignetteOpacity = Math.max(0, Math.min(1,
    Number.isFinite(options.maximumVignetteOpacity) ? options.maximumVignetteOpacity : 0.38));

  let mode;
  if (!Number.isFinite(clearance)) {
    mode = 'outside';
  } else if (previousMode === 'outside') {
    mode = clearance < releaseClearance
      ? 'outside'
      : clearance < approachDistance ? 'approaching' : 'inside';
  } else if (clearance <= enterClearance) {
    mode = 'outside';
  } else {
    mode = clearance < approachDistance ? 'approaching' : 'inside';
  }

  const vignetteIntensity = mode === 'approaching'
    ? Math.max(0, Math.min(1,
      (approachDistance - clearance) / (approachDistance - enterClearance)))
    : 0;
  return {
    mode,
    clearance: Number.isFinite(clearance) ? clearance : null,
    vignetteIntensity,
    vignetteOpacity: vignetteIntensity * maximumVignetteOpacity,
    thresholds: {
      outsideEnterClearance: enterClearance,
      outsideExitClearance: releaseClearance,
      approachDistance,
      maximumVignetteOpacity
    }
  };
}

/** Return the forward-ray intersection with a local XZ-plane quad. */
export function intersectLocalQuadRay(origin, direction, bounds = {}) {
  if (!validVector(origin) || !validVector(direction)) {
    return { hit: false, reason: 'invalid-ray', distance: null, localPoint: null };
  }
  const halfWidth = Number.isFinite(bounds.halfWidth) ? Math.max(0, bounds.halfWidth) : 0.5;
  const halfHeight = Number.isFinite(bounds.halfHeight) ? Math.max(0, bounds.halfHeight) : 0.5;
  const denominator = direction[1];
  if (Math.abs(denominator) < 1e-7) {
    return { hit: false, reason: 'parallel-ray', distance: null, localPoint: null };
  }
  const distance = -origin[1] / denominator;
  if (!Number.isFinite(distance) || distance < 0) {
    return { hit: false, reason: 'quad-behind-ray', distance, localPoint: null };
  }
  const localPoint = origin.map((value, index) => value + direction[index] * distance);
  const hit = Math.abs(localPoint[0]) <= halfWidth && Math.abs(localPoint[2]) <= halfHeight;
  return {
    hit,
    reason: hit ? null : 'outside-control-bounds',
    distance,
    localPoint
  };
}

/** WebXR target rays point along local -Z before reference-space conversion. */
export function xrTargetRayDirection(orientation) {
  if (!Array.isArray(orientation) || orientation.length !== 4 || !orientation.every(Number.isFinite)) return null;
  const [x, y, z, w] = orientation;
  const direction = [
    -2 * (x * z + w * y),
    2 * (w * x - y * z),
    2 * (x * x + y * y) - 1
  ].map(value => Math.abs(value) < 1e-15 ? 0 : value);
  return direction.every(Number.isFinite) ? direction : null;
}
