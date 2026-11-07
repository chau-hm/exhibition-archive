from __future__ import annotations

import json
import math

import pytest

from exhibition_archive.navigation import colmap_camera_center, quaternion_wxyz_to_rotation
from exhibition_archive.preview import _build_visitor_config
from exhibition_archive.viewer_orientation import (
    IDENTITY_QUATERNION_XYZW,
    TARGET_VIEWER_UP,
    estimate_y_up_transform,
    map_pose_to_viewer,
    quaternion_xyzw_from_vectors,
    rotate_vector_xyzw,
)


def _unit(vector):
    length = math.sqrt(sum(value * value for value in vector))
    return [value / length for value in vector]


def _quaternion_wxyz_from_matrix(matrix):
    """Build a WXYZ quaternion from a proper rotation matrix for fixtures."""
    m = matrix
    trace = m[0][0] + m[1][1] + m[2][2]
    if trace > 0:
        scale = math.sqrt(trace + 1.0) * 2.0
        w = 0.25 * scale
        x = (m[2][1] - m[1][2]) / scale
        y = (m[0][2] - m[2][0]) / scale
        z = (m[1][0] - m[0][1]) / scale
    elif m[0][0] > m[1][1] and m[0][0] > m[2][2]:
        scale = math.sqrt(1.0 + m[0][0] - m[1][1] - m[2][2]) * 2.0
        w = (m[2][1] - m[1][2]) / scale
        x = 0.25 * scale
        y = (m[0][1] + m[1][0]) / scale
        z = (m[0][2] + m[2][0]) / scale
    elif m[1][1] > m[2][2]:
        scale = math.sqrt(1.0 + m[1][1] - m[0][0] - m[2][2]) * 2.0
        w = (m[0][2] - m[2][0]) / scale
        x = (m[0][1] + m[1][0]) / scale
        y = 0.25 * scale
        z = (m[1][2] + m[2][1]) / scale
    else:
        scale = math.sqrt(1.0 + m[2][2] - m[0][0] - m[1][1]) * 2.0
        w = (m[1][0] - m[0][1]) / scale
        x = (m[0][2] + m[2][0]) / scale
        y = (m[1][2] + m[2][1]) / scale
        z = 0.25 * scale
    norm = math.sqrt(w * w + x * x + y * y + z * z)
    return [component / norm for component in (w, x, y, z)]


def _camera_row(up, forward, image_size=(5712, 4284), image_name="fixture.jpg"):
    up = _unit(up)
    forward = _unit([forward[i] - up[i] * sum(forward[j] * up[j] for j in range(3))
                      for i in range(3)])
    right = _unit([forward[1] * up[2] - forward[2] * up[1],
                   forward[2] * up[0] - forward[0] * up[2],
                   forward[0] * up[1] - forward[1] * up[0]])
    down = [-value for value in up]
    rotation = [right, down, forward]
    qvec = _quaternion_wxyz_from_matrix(rotation)
    return {"qvec_wxyz": qvec, "camera_center": [0.0, 0.0, 0.0],
            "image_name": image_name, "image_size": list(image_size)}


def _close_vector(actual, expected, tolerance=1e-8):
    assert actual == pytest.approx(expected, abs=tolerance)


def _matrix_from_xyzw(quaternion):
    x, y, z, w = quaternion
    length = math.sqrt(x * x + y * y + z * z + w * w)
    x, y, z, w = [value / length for value in quaternion]
    return [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]


def _determinant3(matrix):
    a, b, c = matrix
    return (a[0] * (b[1] * c[2] - b[2] * c[1]) -
            a[1] * (b[0] * c[2] - b[2] * c[0]) +
            a[2] * (b[0] * c[1] - b[1] * c[0]))


def _assert_rotation_invariants(quaternion):
    matrix = _matrix_from_xyzw(quaternion)
    rows = matrix
    for row in rows:
        assert sum(value * value for value in row) == pytest.approx(1.0, abs=1e-9)
    for i in range(3):
        for j in range(i + 1, 3):
            assert sum(rows[i][axis] * rows[j][axis] for axis in range(3)) == pytest.approx(0.0, abs=1e-9)
    assert _determinant3(matrix) == pytest.approx(1.0, abs=1e-9)


def test_colmap_wxyz_world_to_camera_basis_and_camera_center():
    # Identity means COLMAP camera right=+X, image-down=+Y, forward=+Z.
    rotation = quaternion_wxyz_to_rotation([1.0, 0.0, 0.0, 0.0])
    assert rotation == [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    up = [-component for component in rotation[1]]
    assert up == [0.0, -1.0, 0.0]
    assert rotation[2] == [0.0, 0.0, 1.0]

    # A 90-degree camera-local roll encoded in COLMAP WXYZ order.
    half = math.sqrt(0.5)
    rolled = quaternion_wxyz_to_rotation([half, 0.0, 0.0, half])
    _close_vector(rolled[0], [0.0, -1.0, 0.0])
    _close_vector(rolled[1], [1.0, 0.0, 0.0])
    _close_vector(rolled[2], [0.0, 0.0, 1.0])
    _close_vector(colmap_camera_center([half, 0.0, 0.0, half], [1.0, 2.0, 3.0]),
                  [-2.0, 1.0, -3.0])


@pytest.mark.parametrize("world_up,forward", [
    ([0.0, 1.0, 0.0], [0.0, 0.0, -1.0]),
    ([0.0, 0.0, 1.0], [0.0, 1.0, 0.0]),
    ([1.0, 0.0, 0.0], [0.0, 0.0, 1.0]),
    ([0.3, -0.4, 0.8], [0.7, 0.4, 0.2]),
])
def test_camera_up_consensus_maps_arbitrary_world_orientation_to_y_up(world_up, forward):
    rows = [_camera_row(world_up, [forward[0] + index * 0.003,
                                   forward[1] - index * 0.002,
                                   forward[2] + index * 0.001],
                        image_name=f"view-{index}.jpg") for index in range(12)]
    orientation = estimate_y_up_transform(rows)
    assert orientation["status"] == "CAMERA_UP_PROPOSAL_APPLIED_REVIEW_REQUIRED"
    assert orientation["camera_pose_count"] == 12
    assert orientation["consensus_pose_count"] == 12
    assert orientation["applied"] is True
    _close_vector(orientation["candidate_up_colmap_xyz"], _unit(world_up))
    _close_vector(rotate_vector_xyzw(orientation["rotation_quaternion_xyzw"], _unit(world_up)),
                  TARGET_VIEWER_UP)
    _assert_rotation_invariants(orientation["rotation_quaternion_xyzw"])


def test_identity_and_half_turn_are_proper_rotations():
    _close_vector(rotate_vector_xyzw(IDENTITY_QUATERNION_XYZW, [1.0, 2.0, 3.0]),
                  [1.0, 2.0, 3.0])
    half_turn = quaternion_xyzw_from_vectors([0.0, -1.0, 0.0], [0.0, 1.0, 0.0])
    _close_vector(rotate_vector_xyzw(half_turn, [0.0, -1.0, 0.0]), [0.0, 1.0, 0.0])
    _assert_rotation_invariants(half_turn)


def test_robust_consensus_tolerates_nonzero_roll_outliers():
    world_up = _unit([0.2, 0.4, 0.89])
    rows = [_camera_row(world_up, [0.1 + index * 0.02, 1.0, -0.3])
            for index in range(16)]
    rows.extend(_camera_row([1.0, 0.0, 0.0], [0.0, 0.0, 1.0], image_name=f"rolled-{index}.jpg")
                for index in range(3))
    orientation = estimate_y_up_transform(rows)
    assert orientation["applied"] is True
    assert orientation["consensus_pose_count"] == 16
    assert orientation["consensus_fraction"] == pytest.approx(16 / 19)
    _close_vector(orientation["candidate_up_colmap_xyz"], world_up)


def test_insufficient_pose_consensus_uses_identity_and_flags_review():
    rows = [_camera_row([0.0, 0.0, 1.0], [0.0, 1.0, 0.0], image_name=f"z-{i}.jpg")
            for i in range(4)]
    rows.extend(_camera_row([1.0, 0.0, 0.0], [0.0, 0.0, 1.0], image_name=f"x-{i}.jpg")
                for i in range(4))
    orientation = estimate_y_up_transform(rows)
    assert orientation["status"] == "ORIENTATION_REVIEW_REQUIRED"
    assert orientation["applied"] is False
    assert orientation["reason"] == "camera_up_consensus_below_minimum_fraction"
    assert orientation["rotation_quaternion_xyzw"] == IDENTITY_QUATERNION_XYZW
    assert "orientation review" in orientation["fallback"].lower()


def test_portrait_and_landscape_rasters_do_not_change_pose_frame_math():
    rows = [_camera_row([0.0, 0.0, 1.0], [1.0, 0.0, 0.0],
                        image_size=(5712, 4284) if index % 2 == 0 else (3024, 4032),
                        image_name=f"view-{index}.jpg") for index in range(12)]
    orientation = estimate_y_up_transform(rows)
    assert orientation["applied"] is True
    _close_vector(orientation["candidate_up_colmap_xyz"], [0.0, 0.0, 1.0])


@pytest.mark.parametrize("target_direction", [
    [0.0, 0.0, 1.0], [0.0, 0.0, -1.0], [0.001, 1.0, 0.0],
    [0.0, 1.0, 1e-12], [0.0, -1.0, 0.0],
])
def test_pose_mapping_preserves_forward_distance_even_near_vertical(target_direction):
    orientation = estimate_y_up_transform(
        [_camera_row([0.0, 0.0, 1.0], [0.0, 1.0, 0.0], image_name=f"up-{i}.jpg")
         for i in range(12)]
    )
    source_pose = {"position": [3.0, -2.0, 7.0],
                   "target": [3.0 + target_direction[0], -2.0 + target_direction[1],
                              7.0 + target_direction[2]], "fov": 70.75}
    mapped = map_pose_to_viewer(source_pose, orientation["rotation_quaternion_xyzw"])
    source_forward = [source_pose["target"][i] - source_pose["position"][i] for i in range(3)]
    mapped_forward = [mapped["target"][i] - mapped["position"][i] for i in range(3)]
    expected_forward = rotate_vector_xyzw(orientation["rotation_quaternion_xyzw"], source_forward)
    _close_vector(mapped_forward, expected_forward)
    assert math.sqrt(sum(value * value for value in mapped_forward)) == pytest.approx(
        math.sqrt(sum(value * value for value in source_forward)))
    assert mapped["fov"] == source_pose["fov"]
    assert all(math.isfinite(value) for value in mapped["position"] + mapped["target"])


def test_build_visitor_config_shares_model_transform_and_camera_pose(tmp_path, monkeypatch):
    from exhibition_archive import navigation

    output = tmp_path / "output"
    work = output / "work"
    work.mkdir(parents=True)
    reconstruction = tmp_path / "sfm"
    reconstruction.mkdir()
    (work / "sfm-summary.json").write_text(
        json.dumps({"reconstruction_directory": str(reconstruction)}), encoding="utf-8")
    rows = []
    for index in range(12):
        row = _camera_row([0.0, 0.0, 1.0], [1.0, 0.2 + index * 0.01, 0.0],
                          image_name=f"IMG_{index:04}.jpg")
        row["camera_center"] = [float(index), -2.0, 4.0]
        rows.append(row)
    monkeypatch.setattr(navigation, "read_colmap_camera_poses", lambda _path: rows)
    config = {"project_id": "orientation-fixture", "output_directory": "output",
              "integrations": {"sfm": {"directory": "sfm"}}}

    settings, camera = _build_visitor_config(tmp_path, config)

    assert camera["source"]["image_name"] in {row["image_name"] for row in rows}
    assert camera["orientation"]["applied"] is True
    assert settings["cameras"][0]["initial"] == camera["pose_supersplat"]
    assert camera["model_scene_transform_supersplat"]["rotationQuaternionXYZW"] == camera["orientation"]["rotation_quaternion_xyzw"]
    _close_vector(rotate_vector_xyzw(camera["orientation"]["rotation_quaternion_xyzw"],
                                     camera["orientation"]["candidate_up_colmap_xyz"]),
                  TARGET_VIEWER_UP)
