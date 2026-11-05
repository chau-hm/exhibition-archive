from __future__ import annotations

import math
import struct

from exhibition_archive.backends import (
    _project_colmap_observation,
    parse_spirula_sfm_reprojection_summary,
    validate_colmap_directory,
)


def _write_component(path, *, offsets=(0.0, 0.0), point_error=0.0, include_tracks=True,
                     model_id=4):
    path.mkdir(parents=True)
    parameters = (100.0, 110.0, 50.0, 40.0, 0.01, -0.001, 0.002, -0.003)
    with (path / "cameras.bin").open("wb") as stream:
        stream.write(struct.pack("<Q", 1))
        stream.write(struct.pack("<iiQQ", 1, model_id, 100, 80))
        stream.write(struct.pack("<8d", *parameters))
    point_xyz = (0.5, 0.2, 5.0)
    poses = [(0.0, 0.0, 0.0), (-0.25, 0.05, 0.0)]
    with (path / "images.bin").open("wb") as stream:
        stream.write(struct.pack("<Q", 2))
        for index, (name, pose_t, offset) in enumerate(zip(("a.jpg", "b.jpg"), poses, offsets), 1):
            stream.write(struct.pack("<I7dI", index, 1.0, 0.0, 0.0, 0.0,
                                     *pose_t, 1))
            stream.write(name.encode() + b"\0")
            if include_tracks:
                camera_point = (point_xyz[0] + pose_t[0], point_xyz[1] + pose_t[1], point_xyz[2])
                projected = _project_colmap_observation(model_id, parameters, camera_point)
                x, y = projected[0] + offset, projected[1]
                stream.write(struct.pack("<Q2dQ", 1, x, y, 1))
            else:
                stream.write(struct.pack("<Q", 0))
    with (path / "points3D.bin").open("wb") as stream:
        stream.write(struct.pack("<Q", 1))
        track_count = 2 if include_tracks else 0
        stream.write(struct.pack("<Q3d3BdQ", 1, *point_xyz, 120, 130, 140, point_error, track_count))
        if include_tracks:
            stream.write(struct.pack("<4i", 1, 0, 2, 0))
    return [{"relative_path": "a.jpg", "image_id": "a.jpg"},
            {"relative_path": "b.jpg", "image_id": "b.jpg"}]


def _gate(maximum=2.0):
    return {"minimum_registered_images": 2, "minimum_registered_ratio": 1.0,
            "maximum_mean_point_reprojection_error_px": maximum}


def test_opencv_zero_point_error_fields_are_recomputed_from_tracks(tmp_path):
    inventory = _write_component(tmp_path / "sparse", offsets=(0.8, 1.2), point_error=0.0)

    result = validate_colmap_directory(tmp_path / "sparse", inventory, _gate())
    component = result["components"][0]

    assert component["mean_stored_points3D_error_px"] == 0.0
    assert component["mean_point_reprojection_error_px"] == 1.0
    assert component["reprojection_metrics"]["quality_gate_source"] == "recomputed_from_colmap_camera_models_poses_points_and_tracks"
    assert component["reprojection_metrics"]["quality_gate_validation_status"] == "VERIFIED"
    assert component["reprojection_metrics"]["observation_error_px"]["mean"] == 1.0
    assert component["reprojection_metrics"]["observation_error_px"]["count"] == 2
    assert "REPROJECTION_ERROR_ZERO_OR_UNINFORMATIVE" not in result["warnings"]


def test_a_genuinely_zero_reprojection_error_is_validated_not_called_uninformative(tmp_path):
    inventory = _write_component(tmp_path / "sparse")

    result = validate_colmap_directory(tmp_path / "sparse", inventory, _gate())

    assert result["components"][0]["mean_point_reprojection_error_px"] == 0.0
    assert result["components"][0]["reprojection_metrics"]["quality_gate_validation_status"] == "VERIFIED"
    assert "REPROJECTION_ERROR_UNKNOWN" not in result["warnings"]
    assert "REPROJECTION_ERROR_ZERO_OR_UNINFORMATIVE" not in result["warnings"]


def test_mean_per_point_threshold_keeps_existing_two_pixel_boundary(tmp_path):
    inventory = _write_component(tmp_path / "at-limit", offsets=(2.0, 2.0))
    at_limit = validate_colmap_directory(tmp_path / "at-limit", inventory, _gate())
    inventory = _write_component(tmp_path / "over-limit", offsets=(2.0002, 2.0002))
    over_limit = validate_colmap_directory(tmp_path / "over-limit", inventory, _gate())

    assert at_limit["components"][0]["mean_point_reprojection_error_px"] == 2.0
    assert "MEAN_POINT_REPROJECTION_ERROR_ABOVE_CONFIGURED_LIMIT" not in at_limit["warnings"]
    assert over_limit["components"][0]["mean_point_reprojection_error_px"] > 2.0
    assert "MEAN_POINT_REPROJECTION_ERROR_ABOVE_CONFIGURED_LIMIT" in over_limit["warnings"]


def test_unavailable_recomputation_does_not_turn_uninitialized_zero_into_zero_metric(tmp_path):
    inventory = _write_component(tmp_path / "sparse", include_tracks=False, point_error=0.0)

    result = validate_colmap_directory(tmp_path / "sparse", inventory, _gate())

    component = result["components"][0]
    assert component["mean_point_reprojection_error_px"] is None
    assert component["reprojection_metrics"]["quality_gate_validation_status"] == "UNAVAILABLE_OR_UNINFORMATIVE"
    assert "REPROJECTION_ERROR_UNKNOWN" in result["warnings"]


def test_documented_native_colmap_metric_is_used_only_when_recomputation_is_unsupported(tmp_path):
    inventory = _write_component(tmp_path / "sparse", include_tracks=False, point_error=1.25, model_id=5)

    result = validate_colmap_directory(tmp_path / "sparse", inventory, _gate())

    metric = result["components"][0]["reprojection_metrics"]
    assert metric["quality_gate_value_px"] == 1.25
    assert metric["quality_gate_source"] == "colmap_points3D_error_fields"
    assert metric["quality_gate_validation_status"] == "VERIFIED_FROM_NATIVE_COLMAP_POINT_ERROR_FIELDS"


def test_spirula_observation_summary_is_verified_independently_of_point_gate(tmp_path):
    inventory = _write_component(tmp_path / "sparse", offsets=(0.0, 2.0))
    log = tmp_path / "spirula.log"
    log.write_text("Reprojection error: mean 1.000 px, median 1.000 px, over 2 observations\n", encoding="utf-8")
    summary = parse_spirula_sfm_reprojection_summary(log)

    verified = validate_colmap_directory(tmp_path / "sparse", inventory, _gate(),
                                          backend_reprojection_summary=summary)
    assert verified["backend_observation_reprojection_summary"]["validation_status"] == "VERIFIED"
    assert verified["components"][0]["mean_point_reprojection_error_px"] == 1.0

    mismatched = {**summary, "mean_px": 1.1}
    rejected = validate_colmap_directory(tmp_path / "sparse", inventory, _gate(),
                                         backend_reprojection_summary=mismatched)
    assert rejected["backend_observation_reprojection_summary"]["validation_status"] == "REJECTED_MISMATCH"
    assert rejected["components"][0]["mean_point_reprojection_error_px"] == 1.0
