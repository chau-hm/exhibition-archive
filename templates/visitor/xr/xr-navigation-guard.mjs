const EPSILON = 1e-5;

function delta(from, to) {
  return to.map((value, index) => value - from[index]);
}

function length(vector) {
  return Math.hypot(...vector);
}

function add(from, translation) {
  return from.map((value, index) => value + translation[index]);
}

export function disableXrLocomotion(navigation) {
  if (!navigation) return false;
  navigation.enableMove = false;
  navigation.enableTeleport = false;
  navigation.enableSnapVertical = false;
  return navigation.enableMove === false &&
    navigation.enableTeleport === false &&
    navigation.enableSnapVertical === false;
}

export function installXrNavigationGuard(navigation, {
  rigEntity,
  cameraEntity,
  guard,
  readWorldPosition,
  readLocalPosition,
  predictHeadFromRig,
  setRigWorldPosition,
  readGroundHeight,
  writeGroundHeight,
  requestFrame,
  predictTeleportHeadPosition,
  coordinateSnapshot,
  onDecision,
  onInvalid
}) {
  const originalUpdate = navigation?.update;
  const originalTeleport = navigation?.tryTeleport;
  if (typeof originalUpdate !== 'function' || typeof originalTeleport !== 'function') {
    return { installed: false, originalUpdate, originalTeleport };
  }

  const current = () => {
    const rig = readWorldPosition(rigEntity);
    const observedHead = readWorldPosition(cameraEntity);
    const cameraLocalPosition = readLocalPosition?.(cameraEntity) ?? null;
    const projectedHead = predictHeadFromRig
      ? predictHeadFromRig(rigEntity, cameraLocalPosition)
      : null;
    const head = predictHeadFromRig
      ? Array.isArray(projectedHead) && projectedHead.length === 3 && projectedHead.every(Number.isFinite)
        ? projectedHead
        : null
      : observedHead;
    return { rig, head, observedHead, cameraLocalPosition };
  };
  const destinationVerdict = point => {
    const inspection = guard.inspect?.(point) ?? null;
    const allowed = inspection ? inspection.valid : guard.isValid(point);
    return {
      allowed,
      reason: allowed ? null : inspection?.reason ?? 'teleport destination is outside enclosure or within collision radius',
      movementPolicy: 'destination-only',
      destinationInspection: inspection
    };
  };
  const rollback = (beforeRig, beforeGroundHeight) => {
    if (beforeRig) setRigWorldPosition(rigEntity, beforeRig);
    if (Number.isFinite(beforeGroundHeight)) writeGroundHeight(navigation, beforeGroundHeight);
    requestFrame();
  };
  const invalid = (source, phase, beforeRig, beforeGroundHeight, observed) => {
    if (beforeRig) rollback(beforeRig, beforeGroundHeight);
    onInvalid({ source, phase, beforeRig, observed });
  };
  const inspectRigMove = (source, before, beforeGroundHeight, after, beforeCoordinate, phase = 'postcondition') => {
    if (!after.rig || !after.head) {
      invalid(source, phase, before.rig, beforeGroundHeight, after);
      return null;
    }
    const rigDelta = delta(before.rig, after.rig);
    if (length(rigDelta) < EPSILON) return { moved: false, rigDelta };

    // The Visitor projects the tracked camera-local pose through the updated
    // rig matrix. That cancels the root translation from camera-pivot turning.
    // Isolated callers without the projection callback retain the translation fallback.
    const proposedHead = predictHeadFromRig ? after.head : add(before.head, rigDelta);
    const viewpointDelta = delta(before.head, proposedHead);
    const movementKind = source === 'xr_rig_artificial_locomotion' &&
      length(viewpointDelta) <= EPSILON && length(rigDelta) > EPSILON
      ? 'camera-pivot-rotation'
      : 'translation';
    const teleport = source === 'xr_rig_teleport';
    const verdict = teleport
      ? destinationVerdict(proposedHead)
      : { ...guard.segmentAllowed(before.head, proposedHead), movementPolicy: 'swept-segment' };
    const afterCoordinate = coordinateSnapshot();
    if (!verdict.allowed) rollback(before.rig, beforeGroundHeight);
    onDecision({
      source,
      phase,
      beforeRig: before.rig,
      afterRig: after.rig,
      beforeHead: before.head,
      beforeCameraLocalPosition: before.cameraLocalPosition,
      beforeObservedHead: before.observedHead,
      afterHead: after.observedHead,
      observedAfterHead: after.observedHead,
      projectedAfterHead: after.head,
      rigDelta,
      cameraWorldDelta: before.observedHead && after.observedHead
        ? delta(before.observedHead, after.observedHead)
        : delta(before.head, after.head),
      viewpointDelta,
      movementKind,
      proposedHead,
      verdict,
      acceptedTranslation: verdict.allowed ? rigDelta : [0, 0, 0],
      rejectedTranslation: verdict.allowed ? [0, 0, 0] : rigDelta,
      beforeCoordinate,
      afterCoordinate
    });
    return { moved: true, rigDelta, proposedHead, verdict };
  };

  navigation.update = function (deltaTime) {
    const before = current();
    if (!before.rig || !before.head) {
      invalid('xr_rig_artificial_locomotion', 'before-update', before.rig, readGroundHeight(navigation), before);
      return undefined;
    }
    const beforeGroundHeight = readGroundHeight(navigation);
    const beforeCoordinate = coordinateSnapshot();
    const result = originalUpdate.call(this, deltaTime);
    const after = current();
    inspectRigMove('xr_rig_artificial_locomotion', before, beforeGroundHeight, after, beforeCoordinate);
    return result;
  };

  navigation.tryTeleport = function (inputSource) {
    const before = current();
    if (!before.rig || !before.head) {
      invalid('xr_rig_teleport', 'before-teleport', before.rig, readGroundHeight(navigation), before);
      return undefined;
    }
    const beforeGroundHeight = readGroundHeight(navigation);
    const beforeCoordinate = coordinateSnapshot();
    const predicted = predictTeleportHeadPosition(this, inputSource);
    if (predicted) {
      const verdict = destinationVerdict(predicted);
      if (!verdict.allowed) {
        onDecision({
          source: 'xr_rig_teleport',
          phase: 'precheck',
          beforeRig: before.rig,
          afterRig: before.rig,
          beforeHead: before.head,
          afterHead: before.head,
          rigDelta: [0, 0, 0],
          cameraWorldDelta: [0, 0, 0],
          proposedHead: predicted,
          verdict,
          acceptedTranslation: [0, 0, 0],
          rejectedTranslation: delta(before.head, predicted),
          beforeCoordinate,
          afterCoordinate: coordinateSnapshot()
        });
        requestFrame();
        return undefined;
      }
    }

    const result = originalTeleport.call(this, inputSource);
    const after = current();
    inspectRigMove('xr_rig_teleport', before, beforeGroundHeight, after, beforeCoordinate);
    return result;
  };

  return {
    installed: true,
    originalUpdate,
    originalTeleport,
    wrappedUpdate: navigation.update,
    wrappedTeleport: navigation.tryTeleport
  };
}
