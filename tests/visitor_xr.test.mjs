import assert from 'node:assert/strict';
import test from 'node:test';

import { configuredResetFailureReasons, inspectConfiguredSpawn } from '../templates/visitor/xr/camera-readiness.mjs';
import { createCollisionGuard } from '../templates/visitor/xr/collision-geometry.mjs';
import {
  computeRecenterRigPosition,
  inspectTrackedViewpoints,
  nextBoundaryFeedbackMode
} from '../templates/visitor/xr/xr-boundary.mjs';
import { disableXrLocomotion, installXrNavigationGuard } from '../templates/visitor/xr/xr-navigation-guard.mjs';

function boxGuard(radius = 0.2) {
  const vertices = [
    [-2, 0, -2], [2, 0, -2], [2, 0, 2], [-2, 0, 2],
    [-2, 3, -2], [2, 3, -2], [2, 3, 2], [-2, 3, 2]
  ];
  const faces = [
    [0, 1, 2], [0, 2, 3], [4, 6, 5], [4, 7, 6],
    [0, 5, 1], [0, 4, 5], [3, 2, 6], [3, 6, 7],
    [0, 3, 7], [0, 7, 4], [1, 5, 6], [1, 6, 2]
  ];
  const triangles = faces.map(face => face.map(index => vertices[index]));
  return createCollisionGuard({ watertight: true, triangles }, radius);
}

test('Engine enclosure guard checks interior, wall, floor, ceiling and swept crossings', () => {
  const guard = boxGuard();
  assert.equal(guard.inspect([0, 1.5, 0]).valid, true);
  assert.equal(guard.inspect([1.7, 1.5, 0]).valid, true);
  assert.equal(guard.inspect([1.81, 1.5, 0]).valid, false);
  assert.equal(guard.inspect([0, 0.1, 0]).valid, false);
  assert.equal(guard.inspect([0, 2.9, 0]).valid, false);
  for (const target of [[3, 1.5, 0], [-3, 1.5, 0], [0, 1.5, 3], [0, 1.5, -3],
                        [0, -1, 0], [0, 4, 0]]) {
    assert.equal(guard.segmentAllowed([0, 1.5, 0], target).allowed, false, JSON.stringify(target));
  }
  assert.equal(guard.segmentAllowed([0, 1.5, 0], [1, 1.5, 0]).allowed, true);
  assert.equal(guard.inspect([Number.NaN, 1, 0]).valid, false);
});

test('tracked stereo eye containment and explicit recenter preserve tracked pose offset', () => {
  const guard = boxGuard();
  const result = inspectTrackedViewpoints(guard, {
    center: [1.6, 1.5, 0],
    eyes: [
      { eye: 'left', position: [1.82, 1.5, 0] },
      { eye: 'right', position: [1.52, 1.5, 0] }
    ]
  });
  assert.equal(result.valid, false);
  assert.equal(result.checkedEyeCount, 2);
  const recenter = computeRecenterRigPosition([2, 0, 0], [2.2, 1.5, 0], [0, 1.5, 0]);
  assert.equal(recenter.valid, true);
  assert.ok(Math.abs(recenter.position[0] + 0.2) < 1e-12);
  assert.deepEqual(recenter.position.slice(1), [0, 0]);
  assert.deepEqual(recenter.translation, [-2.2, 0, 0]);
});

test('boundary feedback has approach and outside hysteresis', () => {
  assert.equal(nextBoundaryFeedbackMode(1, 'inside').mode, 'inside');
  assert.equal(nextBoundaryFeedbackMode(0.5, 'inside').mode, 'approaching');
  assert.equal(nextBoundaryFeedbackMode(-0.01, 'approaching').mode, 'outside');
  assert.equal(nextBoundaryFeedbackMode(0.1, 'outside').mode, 'outside');
  assert.equal(nextBoundaryFeedbackMode(0.2, 'outside').mode, 'approaching');
});

test('active XR locomotion update and teleport are wrapped by movement guards', () => {
  const guard = boxGuard();
  const rig = { position: [0, 0, 0] };
  const camera = { position: [0, 1, 0], local: [0, 1, 0] };
  const navigation = {
    enableMove: true,
    enableTeleport: true,
    enableSnapVertical: true,
    _currentGroundY: 0,
    requested: [0, 0, 0],
    teleportCalls: 0,
    update() {
      this.requested.forEach((value, i) => { rig.position[i] += value; });
      return 'updated';
    },
    tryTeleport(source) {
      this.teleportCalls++;
      rig.position = [source.destination[0], source.destination[1] - 1, source.destination[2]];
      return 'teleported';
    }
  };
  const decisions = [];
  const installed = installXrNavigationGuard(navigation, {
    rigEntity: rig,
    cameraEntity: camera,
    guard,
    readWorldPosition: entity => entity.position.slice(),
    readLocalPosition: entity => entity.local.slice(),
    predictHeadFromRig: entity => [entity.position[0], entity.position[1] + camera.local[1], entity.position[2]],
    setRigWorldPosition: (entity, position) => { entity.position = position.slice(); },
    readGroundHeight: entity => entity._currentGroundY,
    writeGroundHeight: (entity, value) => { entity._currentGroundY = value; },
    requestFrame() {},
    predictTeleportHeadPosition: (_navigation, source) => source.destination,
    coordinateSnapshot: () => ({ frame: 'fixture-world' }),
    onDecision: decision => decisions.push(decision),
    onInvalid() {}
  });
  assert.equal(installed.installed, true);
  navigation.requested = [0.5, 0, 0];
  navigation.update(1 / 60);
  assert.deepEqual(rig.position, [0.5, 0, 0]);
  navigation.requested = [3, 0, 0];
  navigation.update(1 / 60);
  assert.deepEqual(rig.position, [0.5, 0, 0]);
  assert.equal(decisions.at(-1).source, 'xr_rig_artificial_locomotion');
  assert.equal(decisions.at(-1).verdict.allowed, false);
  assert.equal(navigation.tryTeleport({ destination: [3, 1, 0] }), undefined);
  assert.equal(navigation.teleportCalls, 0);
  assert.equal(navigation.tryTeleport({ destination: [1, 1, 0] }), 'teleported');
  assert.equal(navigation.teleportCalls, 1);
  assert.equal(decisions.at(-1).source, 'xr_rig_teleport');
  assert.equal(decisions.at(-1).verdict.allowed, true);
});

test('invalid navigation API fails closed and camera readiness requires manager target', () => {
  assert.equal(disableXrLocomotion({ enableMove: true, enableTeleport: true, enableSnapVertical: true }), true);
  assert.equal(disableXrLocomotion(null), false);
  const spawn = { position: [0, 1, 0], target: [0, 1, -2], fov: 70 };
  const camera = {
    getPosition: () => ({ x: 0, y: 1, z: 0 }),
    forward: { x: 0, y: 0, z: -1 },
    camera: { fov: 70 }
  };
  const inspect = inspectConfiguredSpawn(camera, spawn, {
    targetPose: { position: [0, 1, 0] },
    transitionProgress: 1
  });
  assert.equal(inspect.ready, true);
  assert.deepEqual(configuredResetFailureReasons({ settled: true }, true, true, { matches: true }), []);
  assert.deepEqual(configuredResetFailureReasons({ settled: false }, false, false, { matches: false }), [
    'camera_transition_not_settled', 'render_frame_not_presented', 'reset_spawn_mismatch', 'scene_transform_mismatch'
  ]);
});
