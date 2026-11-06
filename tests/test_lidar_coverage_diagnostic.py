from __future__ import annotations

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("open3d")

from tools.diagnostics import lidar_sfm_coverage as diagnostic


def _grid(side: int = 5) -> np.ndarray:
    return np.asarray(
        [(x, y, z) for x in range(side) for y in range(side) for z in range(side)],
        dtype=np.float64,
    )


def test_similarity_transform_application_and_inverse():
    angle = np.deg2rad(30)
    rotation = np.asarray(
        [[np.cos(angle), -np.sin(angle), 0], [np.sin(angle), np.cos(angle), 0], [0, 0, 1]],
        dtype=np.float64,
    )
    transform = np.eye(4)
    transform[:3, :3] = 2.5 * rotation
    transform[:3, 3] = [3, -4, 1]
    inverse = np.linalg.inv(transform)
    points = np.asarray([[1, 2, 3], [-1, 0, 2]], dtype=np.float64)

    aligned = diagnostic.apply_transform(points, transform)
    recovered = diagnostic.apply_transform(aligned, inverse)

    np.testing.assert_allclose(recovered, points, atol=1e-12)
    assert diagnostic.validate_similarity_pair(transform, inverse, 2.5)["valid"]


def test_saved_transform_pair_rejects_inconsistent_inverse():
    with pytest.raises(ValueError, match="not mutual inverses"):
        diagnostic.validate_similarity_pair(np.eye(4), np.diag([2, 1, 1, 1]))


def test_redundant_rotation_field_is_checked_without_changing_matrix_result():
    saved_rotation = np.eye(3) * 1.02
    result = diagnostic.validate_similarity_pair(np.eye(4), np.eye(4), 1.0, saved_rotation)

    assert result["valid"] is True
    assert result["auxiliary_rotation_metadata_matches_matrix"] is False
    assert result["auxiliary_rotation_metadata_max_abs_error"] == pytest.approx(0.02)


def test_engine_validation_stride_sample_matches_existing_screen():
    sfm_indices = diagnostic.engine_validation_sample_indices(63_824)
    lidar_indices = diagnostic.engine_validation_sample_indices(209_587)

    assert len(sfm_indices) == 9_118
    assert len(lidar_indices) == 9_981
    assert sfm_indices[-1] < 63_824
    assert lidar_indices[-1] < 209_587


def test_bidirectional_nearest_neighbor_distances():
    source = np.asarray([[0, 0, 0], [2, 0, 0]], dtype=np.float64)
    target = np.asarray([[0.1, 0, 0], [2.1, 0, 0]], dtype=np.float64)

    forward = diagnostic.nearest_neighbor_distances(source, target)
    reverse = diagnostic.nearest_neighbor_distances(target, source)

    np.testing.assert_allclose(forward, [0.1, 0.1], atol=1e-8)
    np.testing.assert_allclose(reverse, [0.1, 0.1], atol=1e-8)


def test_full_overlap_has_complete_bidirectional_support():
    points = _grid(4)
    forward = diagnostic.nearest_neighbor_distances(points, points)
    reverse = diagnostic.nearest_neighbor_distances(points, points)
    table = diagnostic.overlap_table(forward, reverse, [1e-8])

    assert table[0]["sfm_to_lidar_fraction"] == 1
    assert table[0]["lidar_to_sfm_fraction"] == 1


def test_partial_overlap_is_directionally_asymmetric():
    source = np.asarray([(x, 0, 0) for x in range(6)], dtype=np.float64)
    target = source[:3]
    forward = diagnostic.nearest_neighbor_distances(source, target)
    reverse = diagnostic.nearest_neighbor_distances(target, source)
    table = diagnostic.overlap_table(forward, reverse, [0.01])[0]

    assert table["sfm_to_lidar_fraction"] == pytest.approx(0.5)
    assert table["lidar_to_sfm_fraction"] == 1


def test_non_overlapping_clouds_have_no_threshold_support():
    source = np.asarray([[0, 0, 0], [1, 0, 0]], dtype=np.float64)
    target = np.asarray([[10, 0, 0], [11, 0, 0]], dtype=np.float64)
    forward = diagnostic.nearest_neighbor_distances(source, target)
    reverse = diagnostic.nearest_neighbor_distances(target, source)
    table = diagnostic.overlap_table(forward, reverse, [0.1])[0]

    assert table["sfm_to_lidar_fraction"] == 0
    assert table["lidar_to_sfm_fraction"] == 0


def test_unequal_density_exposes_directional_overlap_difference():
    sparse = np.asarray([[0, 0, 0], [1, 0, 0]], dtype=np.float64)
    dense = np.asarray([(x, 0, 0) for x in np.linspace(0, 1, 11)], dtype=np.float64)
    sparse_to_dense = diagnostic.nearest_neighbor_distances(sparse, dense)
    dense_to_sparse = diagnostic.nearest_neighbor_distances(dense, sparse)
    table = diagnostic.overlap_table(sparse_to_dense, dense_to_sparse, [0.04])[0]

    assert table["sfm_to_lidar_fraction"] == 1
    assert table["lidar_to_sfm_fraction"] < table["sfm_to_lidar_fraction"]


def test_residual_percentiles_and_spatial_outlier_are_reported():
    values = np.concatenate([np.linspace(0, 1, 100), [100]])
    stats = diagnostic.distance_statistics(values)

    assert stats["count"] == 101
    assert stats["p95"] == pytest.approx(np.percentile(values, 95))
    assert stats["maximum"] == 100
    assert diagnostic.distance_statistics(np.asarray([0.2, 0.4]))["median"] == pytest.approx(0.3)


def test_coordinate_normalization_uses_the_declared_bbox_diagonal():
    raw = np.asarray([0.5, 1.0, 2.0])
    np.testing.assert_allclose(diagnostic.coordinate_normalized_distance(raw, 2), [0.25, 0.5, 1])
    with pytest.raises(ValueError, match="positive and finite"):
        diagnostic.coordinate_normalized_distance(raw, 0)


def test_report_generation_writes_a_diagnostic_only_classification(tmp_path):
    points = _grid(5)
    sfm_summary = diagnostic.point_cloud_summary(points, "SfM")
    lidar_summary = diagnostic.point_cloud_summary(points, "DepthEye LiDAR")
    alignment = {
        "accepted": False,
        "status": "REJECTED",
        "scale_lidar_units_per_sfm_unit": 1.0,
        "similarity_transform_sfm_to_lidar_row_major": np.eye(4).tolist(),
        "similarity_transform_lidar_to_sfm_row_major": np.eye(4).tolist(),
        "global_registration": {"feature_correspondence_count": 12},
        "registration_evidence": {
            "forward_overlap_fraction": 1.0,
            "reverse_overlap_fraction": 1.0,
            "bidirectional_p95_normalized_residual": 0.0,
            "feature_fit_residuals": {"p95": 0.0},
            "local_similarity_refinement": {
                "iterations": 12,
                "maximum_iterations": 12,
                "final_correspondence_count": 100,
                "converged_by_parameter_delta": False,
            },
        },
        "validation_policy": {"maximum_normalized_p95_residual": 0.03},
        "input_point_counts": {"sfm_sample_used": len(points)},
    }
    metrics, arrays = diagnostic.analyze_geometry(
        points,
        points,
        np.asarray([[0.5, 0.5, 0.5], [3.5, 3.5, 3.5]]),
        alignment,
        sfm_summary,
        lidar_summary,
    )
    provenance = {
        "lidar_artifact_identity": {"selected_path": "/fixture/scan.ply"},
        "sfm_summary": {"path": "/fixture/sfm-summary.json", "sha256": "a" * 64},
    }
    summary = {
        "project_id": "synthetic",
        "attempt_directory": "/fixture/sfm-attempt",
        "execution_status": "COMPLETED",
        "quality_gate": "SFM_VALID",
        "registered_image_count": 2,
        "sparse_point_count": len(points),
    }
    inspection = {"selected": {"point_count": len(points)}}

    report = diagnostic.render_report(
        metrics,
        provenance,
        summary,
        inspection,
        alignment,
        tmp_path,
        "python diagnostic.py --output-dir /tmp/diagnostic",
    )
    report_path = tmp_path / "lidar-partial-coverage-report.md"
    report_path.write_text(report, encoding="utf-8")
    generated = report_path.read_text(encoding="utf-8")

    assert "LiDAR–SfM partial-coverage diagnostic" in generated
    assert "REJECTED_CANDIDATE — DIAGNOSTIC_ONLY" in generated
    assert "INSUFFICIENT_EVIDENCE" in generated or "PROMISING_PARTIAL_ALIGNMENT" in generated
    assert "python diagnostic.py" in generated
    assert arrays["sfm_in_lidar"].shape == points.shape
