"""Confidence-gated mapping from COLMAP world coordinates to a Y-up viewer."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any

from .navigation import quaternion_wxyz_to_rotation


TARGET_VIEWER_UP = [0.0, 1.0, 0.0]
IDENTITY_QUATERNION_XYZW = [0.0, 0.0, 0.0, 1.0]
CAMERA_UP_CLUSTER_RADIUS_DEGREES = 25.0
MIN_CAMERA_POSES = 8
MIN_CONSENSUS_FRACTION = 0.70
MAX_INLIER_P90_DEVIATION_DEGREES = 20.0


def _unit(vector: Iterable[float]) -> list[float]:
    values = [float(value) for value in vector]
    if len(values) != 3 or not all(math.isfinite(value) for value in values):
        raise ValueError("Expected three finite vector components")
    length = math.sqrt(sum(value * value for value in values))
    if length <= 1e-12:
        raise ValueError("Cannot normalize a zero vector")
    return [value / length for value in values]


def _dot(first: Iterable[float], second: Iterable[float]) -> float:
    return sum(a * b for a, b in zip(first, second))


def _cross(first: Iterable[float], second: Iterable[float]) -> list[float]:
    a, b = list(first), list(second)
    return [a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0]]


def _angle_degrees(first: Iterable[float], second: Iterable[float]) -> float:
    cosine = max(-1.0, min(1.0, _dot(first, second)))
    return math.degrees(math.acos(cosine))


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    location = (len(ordered) - 1) * percentile
    lower = math.floor(location)
    upper = math.ceil(location)
    fraction = location - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def quaternion_xyzw_from_vectors(source: Iterable[float], target: Iterable[float]) -> list[float]:
    """Return the proper rotation taking ``source`` to ``target``."""
    first, second = _unit(source), _unit(target)
    cosine = max(-1.0, min(1.0, _dot(first, second)))
    if cosine > 1.0 - 1e-12:
        return list(IDENTITY_QUATERNION_XYZW)
    if cosine < -1.0 + 1e-12:
        # Pick the least-aligned cardinal axis for a stable 180-degree turn.
        cardinal = min(([1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]),
                       key=lambda axis: abs(_dot(first, axis)))
        axis = _unit(_cross(first, cardinal))
        return [axis[0], axis[1], axis[2], 0.0]
    axis = _cross(first, second)
    quaternion = [axis[0], axis[1], axis[2], 1.0 + cosine]
    norm = math.sqrt(sum(value * value for value in quaternion))
    return [value / norm for value in quaternion]


def rotate_vector_xyzw(quaternion: Iterable[float], vector: Iterable[float]) -> list[float]:
    """Rotate a vector by a unit XYZW quaternion without changing its length."""
    q = [float(value) for value in quaternion]
    if len(q) != 4 or not all(math.isfinite(value) for value in q):
        raise ValueError("Expected four finite XYZW quaternion components")
    q_length = math.sqrt(sum(value * value for value in q))
    if q_length <= 1e-12:
        raise ValueError("Cannot rotate with a zero quaternion")
    x, y, z, w = [value / q_length for value in q]
    v = [float(value) for value in vector]
    if len(v) != 3 or not all(math.isfinite(value) for value in v):
        raise ValueError("Expected three finite vector components")
    twice_cross = [2.0 * value for value in _cross([x, y, z], v)]
    correction = _cross([x, y, z], twice_cross)
    return [v[i] + w * twice_cross[i] + correction[i] for i in range(3)]


def estimate_y_up_transform(camera_rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Estimate scene up from robust COLMAP camera-up consensus.

    COLMAP stores a world-to-camera quaternion in WXYZ order. Its camera-local
    +Y axis points down the image, so camera up in world coordinates is the
    negative second row of that rotation matrix. The estimated direction is a
    camera-orientation proposal, not a physical-gravity or floor-plane claim.
    """
    camera_ups: list[list[float]] = []
    for row in camera_rows:
        try:
            rotation = quaternion_wxyz_to_rotation(row["qvec_wxyz"])
            camera_up = _unit([-float(component) for component in rotation[1]])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        camera_ups.append(camera_up)

    count = len(camera_ups)
    medoid_index = None
    inliers: list[list[float]] = []
    candidate_up = None
    median_deviation = p90_deviation = 0.0
    if count:
        medoid_index = min(
            range(count),
            key=lambda index: sum(_angle_degrees(camera_ups[index], candidate)
                                  for candidate in camera_ups),
        )
        medoid = camera_ups[medoid_index]
        inliers = [up for up in camera_ups
                   if _angle_degrees(medoid, up) <= CAMERA_UP_CLUSTER_RADIUS_DEGREES]
        if inliers:
            try:
                candidate_up = _unit([sum(up[axis] for up in inliers) for axis in range(3)])
                deviations = [_angle_degrees(candidate_up, up) for up in inliers]
                median_deviation = _percentile(deviations, 0.5)
                p90_deviation = _percentile(deviations, 0.9)
            except ValueError:
                candidate_up = None

    fraction = len(inliers) / count if count else 0.0
    confident = (candidate_up is not None and count >= MIN_CAMERA_POSES and
                 len(inliers) >= MIN_CAMERA_POSES and fraction >= MIN_CONSENSUS_FRACTION and
                 p90_deviation <= MAX_INLIER_P90_DEVIATION_DEGREES)
    rotation = (quaternion_xyzw_from_vectors(candidate_up, TARGET_VIEWER_UP)
                if confident and candidate_up is not None else list(IDENTITY_QUATERNION_XYZW))
    status = ("CAMERA_UP_PROPOSAL_APPLIED_REVIEW_REQUIRED" if confident
              else "ORIENTATION_REVIEW_REQUIRED")
    if count < MIN_CAMERA_POSES:
        reason = "fewer_than_minimum_registered_camera_poses"
    elif candidate_up is None:
        reason = "no_stable_camera_up_consensus"
    elif fraction < MIN_CONSENSUS_FRACTION:
        reason = "camera_up_consensus_below_minimum_fraction"
    elif p90_deviation > MAX_INLIER_P90_DEVIATION_DEGREES:
        reason = "camera_up_consensus_dispersion_too_high"
    else:
        reason = None

    return {
        "status": status,
        "method": "robust registered-camera local-up medoid consensus",
        "camera_pose_convention": (
            "COLMAP qvec_wxyz is world-to-camera; local camera +Z is forward; "
            "local camera +Y is image-down; camera center is -R^T t"
        ),
        "interpretation": "Camera orientation evidence only; not physical gravity, a ground plane, or navigation approval.",
        "camera_pose_count": count,
        "consensus_pose_count": len(inliers),
        "consensus_fraction": fraction,
        "cluster_radius_degrees": CAMERA_UP_CLUSTER_RADIUS_DEGREES,
        "minimum_camera_pose_count": MIN_CAMERA_POSES,
        "minimum_consensus_fraction": MIN_CONSENSUS_FRACTION,
        "median_inlier_deviation_degrees": median_deviation,
        "p90_inlier_deviation_degrees": p90_deviation,
        "maximum_p90_inlier_deviation_degrees": MAX_INLIER_P90_DEVIATION_DEGREES,
        "candidate_up_colmap_xyz": candidate_up,
        "target_up_viewer_xyz": list(TARGET_VIEWER_UP),
        "rotation_quaternion_xyzw": rotation,
        "applied": confident,
        "fallback": None if confident else (
            "Identity model transform and raw camera pose; assume viewer +Y-up and require orientation review."
        ),
        "reason": reason,
    }


def map_pose_to_viewer(pose: Mapping[str, Any], rotation_quaternion_xyzw: Iterable[float]) -> dict[str, Any]:
    """Map camera position and target through the same scene rotation."""
    result = dict(pose)
    for key in ("position", "target"):
        if key in pose:
            result[key] = rotate_vector_xyzw(rotation_quaternion_xyzw, pose[key])
    return result
