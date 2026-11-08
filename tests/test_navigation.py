from __future__ import annotations

import hashlib
import json
import math
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

from exhibition_archive.navigation import (
    _components,
    _reconcile_camera_trajectory,
    approve_navigation_artifact,
    colmap_camera_center,
    generate_navigation,
    navigation_segment_allowed,
    read_colmap_reconstruction,
    validate_navigation_artifact,
    validate_guided_path,
)
from exhibition_archive.state import ProjectState
from exhibition_archive.navigation_review import invalidate_navigation_approval
from exhibition_archive.workflow import (
    _latest_navigation_baseline,
    run_navigation_generation_only,
    run_workflow,
    stage_fingerprint,
)


def source_for(points, *, floor=True, ceiling=True, frame_verified=True):
    source = {
        "source_reconstruction": {"identity": {"reconstruction_id": "synthetic-fixture"}},
        "coordinate_frame": {
            "coordinates_are_viewer": True,
            "viewer_frame": "synthetic viewer frame",
            "scale_status": "UNKNOWN_NON_METRIC",
            "source_to_viewer": {"uniform_scale": 1, "rotation": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                                 "translation": [0, 0, 0], "verified": frame_verified},
        },
        "stations": [{"stationId": f"station-{i:03d}", "centerCommonViewer": [x, y, h], "trusted": True}
                     for i, (x, y, h) in enumerate(points)],
    }
    if floor:
        source["floor_plane"] = {"normal": [0, 0, 1], "offset": 0, "coordinate_frame": "viewer",
                                 "classification": "GEOMETRY_SUPPORTED", "source": "synthetic plane fixture"}
    if ceiling:
        source["ceiling"] = {"height_along_floor_normal_units": 4, "classification": "GEOMETRY_SUPPORTED"}
    return source


def outdoor_source(scale=1.0, *, source_z_up=False):
    """Irregular, non-metric outdoor camera path with local tracked geometry."""
    source = source_for([], floor=False, ceiling=False)
    source["test_scale"] = scale
    source["coordinate_frame"]["navigation_up_viewer"] = [0, 1, 0]
    source["coordinate_frame"]["navigation_reference_axis_viewer"] = [1, 0, 0]
    camera_xy = [(x, z) for x in range(6) for z in range(6) if not (x >= 4 and z >= 4)]
    if source_z_up:
        source["stations"] = [{"stationId": f"camera-{i:03d}", "center": [x * scale, z * scale, 2 * scale],
                               "trusted": True, "trust_reason": "synthetic registered camera"}
                              for i, (x, z) in enumerate(camera_xy)]
    else:
        source["stations"] = [{"stationId": f"camera-{i:03d}", "centerCommonViewer": [x * scale, 2 * scale, z * scale],
                               "trusted": True, "trust_reason": "synthetic registered camera"}
                              for i, (x, z) in enumerate(camera_xy)]
    points = []
    for x_index in range(11):
        for z_index in range(11):
            x, z = x_index * 0.5, z_index * 0.5
            if x >= 4 and z >= 4:
                continue
            for y in (0.0, 1.0, 2.0, 3.0, 4.0):
                points.append([x * scale, z * scale, y * scale] if source_z_up
                              else [x * scale, y * scale, z * scale])
    # These high-track points are deliberately disconnected distant background.
    points.extend([[1000 * scale, 500 * scale, 1000 * scale] for _ in range(12)])
    source["sparse_points"] = points
    source["sparse_point_tracks"] = [4] * len(points)
    source["sparse_point_count"] = len(points)
    return source


class CameraPoseTests(unittest.TestCase):
    def test_colmap_camera_center_is_negative_transposed_rotation_times_translation(self):
        self.assertEqual(colmap_camera_center([1, 0, 0, 0], [-1, -2, -3]), [1.0, 2.0, 3.0])
        q = [math.sqrt(0.5), 0, 0, math.sqrt(0.5)]
        for actual, expected in zip(colmap_camera_center(q, [2, -1, -3]), [1.0, 2.0, 3.0]):
            self.assertAlmostEqual(actual, expected, places=12)

    def test_source_to_viewer_transform_and_floor_up_are_explicit_not_assumed(self):
        rotation = [[1, 0, 0], [0, 0, -1], [0, 1, 0]]
        source = {
            "coordinate_frame": {"scale_status": "UNKNOWN_NON_METRIC", "source_to_viewer": {
                "uniform_scale": 2, "rotation": rotation, "translation": [10, 20, 30], "verified": True}},
            "floor_plane": {"normal": [0, 0, 1], "offset": 0, "coordinate_frame": "source", "classification": "GEOMETRY_SUPPORTED"},
            "ceiling": {"value": 8, "classification": "REVIEW_CANDIDATE"},
            "stations": [{"stationId": str(i), "center": [x, y, 2], "trusted": True}
                         for i, (x, y) in enumerate((x, y) for x in range(4) for y in range(4))],
        }
        artifact = generate_navigation(source, {"camera_clearance_units": 0})
        self.assertEqual(artifact["coordinate_frame"]["viewer_to_navigation"]["up_viewer"], [0.0, -1.0, 0.0])
        self.assertAlmostEqual(artifact["pose_evidence"]["candidate_camera_centers"][0]["viewer_center"][0], 10.0)
        self.assertAlmostEqual(artifact["pose_evidence"]["candidate_camera_centers"][0]["navigation_local_uvh"][2], 4.0)

    def test_colmap_binary_reader_keeps_image_ids_intrinsics_and_sparse_points(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "cameras.bin").write_bytes(struct.pack("<QiiQQ3d", 1, 1, 0, 1920, 1080, 1000, 960, 540))
            image = (struct.pack("<Q", 1) + struct.pack("<I4d3dI", 1, 1, 0, 0, 0, -1, -2, -3, 1) + b"frame-17.jpg\0" + struct.pack("<Q", 0))
            (root / "images.bin").write_bytes(image)
            (root / "points3D.bin").write_bytes(struct.pack("<Q", 0))
            result = read_colmap_reconstruction(root)
            self.assertEqual(result["cameras"][0]["image_id"], 1)
            self.assertEqual(result["cameras"][0]["image_name"], "frame-17.jpg")
            self.assertEqual(result["cameras"][0]["camera_center"], [1.0, 2.0, 3.0])
            self.assertEqual(result["camera_models"][0]["parameters"], [1000, 960, 540])


class CameraTrajectoryReconciliationTests(unittest.TestCase):
    @staticmethod
    def _stations(xs, *, timestamp_offset=0):
        return [{"station_id": f"station-{i:03d}", "image_name": f"IMG_{i:03d}.jpg",
                 "uvh": [float(x), 0.0, 2.0], "center_viewer": [float(x), 2.0, 0.0],
                 "forward_viewer": [0.0, 0.0, -1.0], "capture_timestamp": timestamp_offset + i,
                 "registered": True, "triangulated_observation_count": 80}
                for i, x in enumerate(xs)]

    def test_locally_supported_capture_gap_is_a_virtual_review_bridge(self):
        stations = self._stations([0, 1, 2, 3.8, 4.8, 5.8])
        spatial = _components(stations, 1.5)
        self.assertEqual(len(spatial), 2)

        reconciled, report = _reconcile_camera_trajectory(stations, spatial, 1.5, {})

        edge, = report["sequence"]["component_transition_edges"]
        self.assertEqual(len(reconciled), 1)
        self.assertEqual(sum(map(len, reconciled)), len(stations))  # no camera station was fabricated
        self.assertEqual(edge["bridge_assessment"], "INFERRED_VIRTUAL_CAPTURE_GAP_REQUIRES_REVIEW")
        self.assertAlmostEqual(edge["horizontal_distance_units"], 1.8)
        self.assertAlmostEqual(report["bridge_rule"]["baseline_p95_step_units"], 1.0)
        self.assertEqual(report["bridge_rule"]["baseline_step_count"], 4)
        self.assertTrue(report["bridge_rule"]["all_accepted_bridge_edges_require_human_review"])

    def test_distant_disconnected_route_is_not_used_to_inflate_its_own_bridge_limit(self):
        stations = self._stations([0, 1, 2, 15, 16, 17])
        spatial = _components(stations, 1.5)

        reconciled, report = _reconcile_camera_trajectory(stations, spatial, 1.5, {})

        edge, = report["sequence"]["component_transition_edges"]
        self.assertEqual(len(reconciled), 2)
        self.assertEqual(edge["bridge_assessment"], "SPATIAL_COMPONENTS_REMAIN_SEPARATE")
        self.assertEqual(report["bridge_rule"]["distance_limit_units"], 2.0)
        self.assertGreater(edge["horizontal_distance_units"], report["bridge_rule"]["distance_limit_units"])

    def test_ambiguous_capture_order_cannot_reconcile_spatial_components(self):
        stations = self._stations([0, 1, 2, 3.8, 4.8, 5.8])
        stations[-1]["capture_timestamp"] = stations[-2]["capture_timestamp"]
        spatial = _components(stations, 1.5)

        reconciled, report = _reconcile_camera_trajectory(stations, spatial, 1.5, {})

        self.assertEqual(report["capture_order_status"], "UNAVAILABLE_OR_AMBIGUOUS")
        self.assertEqual(report["ordered_station_ids"], [])
        self.assertEqual(len(reconciled), 2)
        self.assertEqual(report["sequence"]["component_transition_edges"], [])

    def test_explicit_timestamps_define_order_and_open_u_route_is_not_closed(self):
        stations = self._stations([0, 1, 2, 2, 2, 1, 0])
        stations = [dict(station, uvh=[station["uvh"][0], float(y), 2.0],
                         center_viewer=[station["center_viewer"][0], 2.0, float(y)])
                    for station, y in zip(stations, [0, 0, 0, 1, 2, 2, 2])]
        stations.reverse()  # input/COLMAP order is deliberately unrelated to capture order
        spatial = _components(stations, 1.1)

        reconciled, report = _reconcile_camera_trajectory(stations, spatial, 1.1, {})

        self.assertEqual(report["ordered_image_names"], [f"IMG_{i:03d}.jpg" for i in range(7)])
        self.assertEqual(len(reconciled), 1)
        endpoint = report["sequence"]["route_endpoint_gap"]
        self.assertEqual(endpoint["classification"], "OPEN_CAPTURE_ROUTE_ENDPOINTS; NOT_CONNECTED")
        self.assertEqual(endpoint["horizontal_distance_units"], 2.0)
        self.assertEqual(report["sequence"]["component_transition_edges"], [])

    def test_almost_closed_camera_loop_reports_its_open_endpoints_without_closure(self):
        angles = [math.radians(22.5 + 7.5 * index) for index in range(43)]
        stations = [{"station_id": f"ring-{index:02d}", "image_name": f"capture-{index:02d}.jpg",
                     "uvh": [10 * math.cos(angle), 10 * math.sin(angle), 2.0],
                     "center_viewer": [10 * math.cos(angle), 2.0, 10 * math.sin(angle)],
                     "forward_viewer": [-math.cos(angle), 0.0, -math.sin(angle)],
                     "capture_order_index": index, "registered": True,
                     "triangulated_observation_count": 100}
                    for index, angle in enumerate(angles)]
        spacing = 20 * math.sin(math.radians(7.5) / 2)
        radius = spacing * 2.5
        spatial = _components(stations, radius)

        reconciled, report = _reconcile_camera_trajectory(stations, spatial, radius, {})

        sequence = report["sequence"]
        endpoint = sequence["route_endpoint_gap"]
        self.assertEqual(report["capture_order_status"], "VERIFIED_EXPLICIT_ORDER")
        self.assertEqual(len(reconciled), 1)
        self.assertEqual(len(sequence["consecutive_pairs"]), len(stations) - 1)
        self.assertGreater(sequence["polar_angular_coverage_degrees_about_camera_centroid"], 300)
        self.assertEqual(endpoint["classification"], "OPEN_CAPTURE_ROUTE_ENDPOINTS; NOT_CONNECTED")
        self.assertEqual(sequence["component_transition_edges"], [])

    def test_complete_circular_capture_path_reports_closure_from_observed_positions(self):
        angles = [2 * math.pi * index / 48 for index in range(49)]
        stations = [{"station_id": f"circle-{index:02d}", "image_name": f"capture-{index:02d}.jpg",
                     "uvh": [8 * math.cos(angle), 8 * math.sin(angle), 2.0],
                     "center_viewer": [8 * math.cos(angle), 2.0, 8 * math.sin(angle)],
                     "forward_viewer": [-math.cos(angle), 0.0, -math.sin(angle)],
                     "capture_order_index": index, "registered": True,
                     "triangulated_observation_count": 100}
                    for index, angle in enumerate(angles)]
        spacing = 16 * math.sin(math.pi / 48)
        spatial = _components(stations, spacing * 2.5)

        reconciled, report = _reconcile_camera_trajectory(stations, spatial, spacing * 2.5, {})

        sequence = report["sequence"]
        self.assertEqual(len(reconciled), 1)
        self.assertEqual(sequence["distinct_camera_positions"], 48)
        self.assertEqual(sequence["duplicate_or_near_duplicate_positions"], 1)
        self.assertGreater(sequence["polar_angular_coverage_degrees_about_camera_centroid"], 350)
        self.assertIsNone(sequence["route_endpoint_gap"])
        self.assertEqual(sequence["nonadjacent_path_self_intersections"], 0)

    def test_two_unrelated_camera_loops_do_not_join_on_capture_order_alone(self):
        stations = []
        for loop_index, center_x in enumerate((0.0, 30.0)):
            for index in range(16):
                angle = 2 * math.pi * index / 16
                x, y = center_x + 4 * math.cos(angle), 4 * math.sin(angle)
                stations.append({"station_id": f"loop-{loop_index}-{index}",
                                 "image_name": f"loop-{loop_index}-{index}.jpg",
                                 "uvh": [x, y, 2.0], "center_viewer": [x, 2.0, y],
                                 "forward_viewer": [-math.cos(angle), 0.0, -math.sin(angle)],
                                 "capture_order_index": loop_index * 16 + index,
                                 "registered": True, "triangulated_observation_count": 100})
        spatial = _components(stations, 2.5)

        reconciled, report = _reconcile_camera_trajectory(stations, spatial, 2.5, {})

        self.assertEqual(len(spatial), 2)
        self.assertEqual(len(reconciled), 2)
        edge, = report["sequence"]["component_transition_edges"]
        self.assertEqual(edge["bridge_assessment"], "SPATIAL_COMPONENTS_REMAIN_SEPARATE")
        self.assertGreater(edge["horizontal_distance_units"], edge["bridge_limit_units"])

    def test_nonuniform_near_collinear_path_reports_spacing_and_pca_degeneracy(self):
        xs = [0.0, 0.05, 0.11, 0.18, 1.0, 2.8, 2.8, 3.2, 4.1]
        stations = self._stations(xs)
        spatial = _components(stations, 2.0)

        reconciled, report = _reconcile_camera_trajectory(stations, spatial, 2.0, {})

        sequence = report["sequence"]
        self.assertEqual(len(reconciled), 1)
        self.assertEqual(sequence["distinct_camera_positions"], len(xs) - 1)
        self.assertEqual(sequence["duplicate_or_near_duplicate_positions"], 1)
        self.assertGreater(sequence["horizontal_distance_units"]["maximum"],
                           sequence["horizontal_distance_units"]["median"] * 5)
        self.assertLess(sequence["pca_minor_to_major_ratio"], 1e-8)


class NavigationBaselineTests(unittest.TestCase):
    def test_repeated_v2_proposals_keep_comparison_anchored_to_original_v1(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            proposals = root / "output" / "navigation" / "proposals"
            proposals.mkdir(parents=True)
            legacy = proposals / "navigation-proposal-v000014.json"
            legacy.write_text(json.dumps({"schema_version": "exhibition-archive.navigation.v1",
                                          "id": "original-v1",
                                          "regions": [{"id": "v1-region"}]}), encoding="utf-8")
            digest = hashlib.sha256(legacy.read_bytes()).hexdigest()
            anchor = {"path": str(legacy.resolve()), "proposal_id": "original-v1", "sha256": digest}
            for version in (17, 23):
                document = {"schema_version": "exhibition-archive.navigation.v1",
                            "id": f"v2-{version}",
                            "coverage_diagnostics": {"trajectory_reconciliation": {
                                "baseline_proposal": anchor}},
                            "regions": [{"id": f"v2-region-{version}"}]}
                (proposals / f"navigation-proposal-v{version:06d}.json").write_text(
                    json.dumps(document), encoding="utf-8")

            baseline = _latest_navigation_baseline(root, {"output_directory": "output"})

        self.assertIsNotNone(baseline)
        self.assertEqual(baseline["document"]["id"], "original-v1")
        self.assertEqual(Path(baseline["path"]).name, "navigation-proposal-v000014.json")
        self.assertEqual(baseline["sha256"], digest)


class GeometryGenerationTests(unittest.TestCase):
    def test_rectangular_room_produces_review_only_supported_region(self):
        points = [[x, y, 2] for x in range(6) for y in range(5)]
        artifact = generate_navigation(source_for(points), {"camera_clearance_units": 0})
        self.assertEqual(len(artifact["regions"]), 1)
        self.assertEqual(artifact["regions"][0]["algorithm"], "ALPHA_SHAPE")
        self.assertEqual(artifact["status"], "NAVIGATION_NEEDS_REVIEW")
        self.assertFalse(artifact["validation"]["public_ready"])
        self.assertEqual(artifact["validation"]["initial_camera_validation"]["status"], "PASS")
        self.assertTrue(all(item["all_stations_inside_or_on"] for item in artifact["validation"]["station_containment"]))

    def test_l_shape_is_concave_and_rejects_shortcut_through_missing_corner(self):
        points = [[x, y, 2] for x in range(6) for y in range(6) if x < 3 or y < 3]
        artifact = generate_navigation(source_for(points), {"camera_clearance_units": 0})
        region = artifact["regions"][0]
        self.assertEqual(region["algorithm"], "ALPHA_SHAPE")
        self.assertEqual(region["topology"], "CONCAVE_ALPHA_SHAPE")
        self.assertFalse(navigation_segment_allowed(artifact, [2, 5, 1], [5, 2, 1]))
        self.assertTrue(navigation_segment_allowed(artifact, [1, 4, 1], [2, 4, 1]))

    def test_swept_geometry_handles_fast_near_collinear_tangent_and_tiny_moves(self):
        points = [[x, y, 2] for x in range(6) for y in range(6) if x < 3 or y < 3]
        artifact = generate_navigation(source_for(points), {"camera_clearance_units": 0})
        self.assertFalse(navigation_segment_allowed(artifact, [2, 5, 1], [5, 2.000000000001, 1]))
        self.assertTrue(navigation_segment_allowed(artifact, [1, 1, 1], [3, 3, 1]))  # touches the reflex vertex
        self.assertFalse(navigation_segment_allowed(artifact, [1, 1, 1], [3, 3, 1], margin=0.01))
        self.assertTrue(navigation_segment_allowed(artifact, [2, 2, 1], [2 + 1e-12, 2, 1]))
        self.assertTrue(navigation_segment_allowed(artifact, [2, 2, 1], [2, 2, 1]))
        huge = json.loads(json.dumps(artifact))
        for region in huge["regions"]:
            region["polygon_uv"] = [[coordinate * 1_000_000 for coordinate in point] for point in region["polygon_uv"]]
        self.assertFalse(navigation_segment_allowed(huge, [2_000_000, 5_000_000, 1], [5_000_000, 2_000_000, 1]))
        self.assertFalse(navigation_segment_allowed(artifact, [float("nan"), 1, 1], [1, 1, 1]))
        self.assertFalse(navigation_segment_allowed(artifact, [None, 1, 1], [1, 1, 1]))

    def test_exact_floor_and_ceiling_contact_are_allowed_but_crossings_are_blocked(self):
        points = [[x, y, 2] for x in range(6) for y in range(5)]
        artifact = generate_navigation(source_for(points), {"camera_clearance_units": 0})
        self.assertTrue(navigation_segment_allowed(artifact, [1, 1, 0], [2, 1, 0]))
        self.assertTrue(navigation_segment_allowed(artifact, [1, 1, 4], [2, 1, 4]))
        self.assertFalse(navigation_segment_allowed(artifact, [2, 2, 1], [2, 2, -100]))
        self.assertFalse(navigation_segment_allowed(artifact, [2, 2, 1], [2, 2, 100]))

    def test_separate_camera_clusters_stay_as_separate_regions(self):
        room_a = [[x, y, 2] for x in range(3) for y in range(3)]
        room_b = [[x + 10, y, 2] for x in range(3) for y in range(3)]
        artifact = generate_navigation(source_for(room_a + room_b), {"camera_clearance_units": 0})
        self.assertEqual(len(artifact["regions"]), 2)
        self.assertEqual(artifact["transitions"], [])
        self.assertFalse(navigation_segment_allowed(artifact, [1, 1, 1], [11, 1, 1]))

    def test_doorway_requires_a_reviewed_portal_and_wall_crossing_is_rejected(self):
        points = []
        points += [{"stationId": f"a-{x}-{y}", "centerCommonViewer": [x, y, 2], "region_id": "a"}
                   for x in (0, 1, 2) for y in (0, 1, 2)]
        points += [{"stationId": f"b-{x}-{y}", "centerCommonViewer": [x, y, 2], "region_id": "b"}
                   for x in (2, 3, 4) for y in (0, 1, 2)]
        source = source_for([])
        source["stations"] = points
        source["transitions"] = [{"from_region": "region-01", "to_region": "region-02",
                                  "segment_uv": [[2, 0.6], [2, 1.4]], "classification": "REVIEW_PROPOSED", "approved": False}]
        artifact = generate_navigation(source, {"camera_clearance_units": 0.1})
        self.assertEqual(len(artifact["regions"]), 2)
        self.assertFalse(any("transition_portal_not_on_both_region_boundaries" in error for error in artifact["validation"]["errors"]))
        self.assertFalse(navigation_segment_allowed(artifact, [1.5, 1, 1], [2.5, 1, 1]))
        misplaced = json.loads(json.dumps(artifact))
        misplaced["transitions"][0]["portal"]["segment_uv"] = [[10, 10], [11, 11]]
        self.assertTrue(any("transition_portal_not_on_both_region_boundaries" in error for error in validate_navigation_artifact(misplaced)["errors"]))
        across_room = json.loads(json.dumps(artifact))
        across_room["transitions"][0]["portal"]["segment_uv"] = [[1.5, 0.6], [3.5, 1.4]]
        self.assertTrue(any("transition_portal_not_on_both_region_boundaries" in error for error in validate_navigation_artifact(across_room)["errors"]))
        reviewed = json.loads(json.dumps(artifact))
        reviewed["transitions"][0]["approved"] = True
        self.assertTrue(navigation_segment_allowed(reviewed, [1.5, 1, 1], [2.5, 1, 1]))
        self.assertFalse(navigation_segment_allowed(reviewed, [1.5, 0.2, 1], [2.5, 0.2, 1]))

    def test_unobserved_ceiling_and_multi_level_spread_require_review(self):
        points = [[x, y, h] for h in (2, 5) for x in range(4) for y in range(4)]
        artifact = generate_navigation(source_for(points, ceiling=False), {"camera_clearance_units": 0})
        self.assertEqual(artifact["status"], "NAVIGATION_INSUFFICIENT_GEOMETRY")
        self.assertTrue(artifact["space_assumption"]["multi_level_suspected"])
        self.assertIn("CEILING_UNOBSERVED", [item["code"] for item in artifact["review"]["warnings"]])
        with self.assertRaisesRegex(ValueError, "technically invalid"):
            approve_navigation_artifact(artifact, "Reviewer", "reviewed", timestamp="2026-10-29T00:00:00Z", source_artifact_sha256="0" * 64)
        sparse_coverage = generate_navigation(source_for([[0, 0, 2], [1, 0, 2]]), {"camera_clearance_units": 0})
        self.assertEqual(sparse_coverage["status"], "NAVIGATION_INSUFFICIENT_GEOMETRY")

    def test_isolated_pose_outlier_is_excluded_but_preserved(self):
        points = [[x, y, 2] for x in range(5) for y in range(5)]
        source = source_for(points + [[100, 100, 2]])
        artifact = generate_navigation(source, {"camera_clearance_units": 0})
        self.assertEqual(artifact["pose_evidence"]["excluded_pose_count"], 1)
        self.assertEqual(artifact["pose_evidence"]["excluded_poses"][0]["reason"], "isolated_spatial_pose_outlier")

    def test_unclassified_camera_poses_remain_provisional_until_explicit_review(self):
        points = [[x, y, 2] for x in range(5) for y in range(5)]
        source = source_for(points)
        for station in source["stations"]:
            station.pop("trusted")
        artifact = generate_navigation(source, {"camera_clearance_units": 0})
        self.assertEqual(artifact["pose_evidence"]["trusted_camera_centers"], [])
        self.assertEqual(len(artifact["pose_evidence"]["provisional_camera_centers"]), 25)
        with self.assertRaisesRegex(ValueError, "provisional camera pose"):
            approve_navigation_artifact(artifact, "Reviewer", "reviewed", timestamp="2026-10-29T00:00:00Z", source_artifact_sha256="0" * 64)

        reviewed_candidate = json.loads(json.dumps(artifact))
        reviewed_candidate["pose_evidence"]["provisional_pose_reviewed"] = True
        for center in reviewed_candidate["pose_evidence"]["candidate_camera_centers"]:
            center["pose_trust"] = "TRUSTED_BY_REVIEW"
        approved = approve_navigation_artifact(reviewed_candidate, "Reviewer", "verified stations", timestamp="2026-10-29T00:00:00Z", source_artifact_sha256="0" * 64)
        self.assertEqual(artifact["pose_evidence"]["provisional_camera_centers"][0]["pose_trust"], "PROVISIONAL")
        self.assertEqual(len(approved["pose_evidence"]["trusted_camera_centers"]), 25)
        self.assertEqual(approved["status"], "NAVIGATION_APPROVED_BY_REVIEW")

    def test_sparse_points_are_diagnostic_and_never_auto_classified_as_walls(self):
        points = [[x, y, 2] for x in range(5) for y in range(5)]
        source = source_for(points)
        source["sparse_points"] = [[0, 0, 0], [2, 2, 1], [10, 10, 0]]
        artifact = generate_navigation(source, {"camera_clearance_units": 0})
        evidence = artifact["regions"][0]["evidence"]["sparse_geometry_observation"]
        self.assertEqual(evidence["status"], "OBSERVED_UNCLASSIFIED")
        self.assertEqual(evidence["finite_points_in_navigation_frame"], 3)
        self.assertEqual(evidence["architectural_classification"], "NOT_PERFORMED")
        self.assertIn("low density is not evidence of empty space", evidence["interpretation"])
        self.assertTrue(all(not boundary["geometry_supported"] for boundary in artifact["regions"][0]["boundary_segments"]))

    def test_default_camera_must_fit_reviewed_floor_ceiling_and_boundary(self):
        points = [[x, y, 2] for x in range(5) for y in range(5)]
        artifact = generate_navigation(source_for(points), {"camera_clearance_units": 0})
        artifact["regions"][0]["vertical_bounds"]["ceiling"]["value"] = 1.5
        check = validate_navigation_artifact(artifact)
        self.assertEqual(check["status"], "INVALID")
        self.assertEqual(check["initial_camera_validation"]["error"], "default_camera_outside_vertical_bounds")

    def test_photo_sfm_proposes_outdoor_limits_without_inventing_walls_or_a_ceiling(self):
        source = outdoor_source(scale=1.0)
        artifact = generate_navigation(source, {"scene_type": "outdoor", "camera_clearance_units": 0})
        self.assertEqual(artifact["status"], "NAVIGATION_NEEDS_REVIEW")
        self.assertEqual(len(artifact["regions"]), 1)
        region = artifact["regions"][0]
        self.assertEqual(region["geometry_candidates"]["ground_plane"]["status"], "PROPOSED")
        self.assertGreaterEqual(region["geometry_candidates"]["ground_plane"]["evidence"]["inlier_count"], 30)
        self.assertIsNone(region["vertical_bounds"]["floor"]["value"])
        self.assertIsNone(region["vertical_bounds"]["ceiling"])
        self.assertFalse(region["exploration_limits"]["physical_ceiling_inferred"])
        self.assertGreater(region["exploration_limits"]["maximum_height"]["value"],
                           region["exploration_limits"]["minimum_height"]["value"])
        self.assertEqual(artifact["coverage_assumptions"]["physical_walls_or_ceiling_inferred"], False)
        self.assertEqual(artifact["validation"]["initial_camera_validation"]["status"], "PASS")

        low = region["exploration_limits"]["minimum_height"]["value"]
        high = region["exploration_limits"]["maximum_height"]["value"]
        inside = [2.0 * source["test_scale"], -2.0 * source["test_scale"], (low + high) / 2.0]
        self.assertTrue(navigation_segment_allowed(artifact, inside, [inside[0] + source["test_scale"], inside[1], inside[2] + source["test_scale"]]))
        self.assertFalse(navigation_segment_allowed(artifact, inside, [inside[0] + 1000 * source["test_scale"], inside[1], inside[2]]))
        self.assertFalse(navigation_segment_allowed(artifact, inside, [inside[0], inside[1], high + source["test_scale"]]))
        self.assertGreater(region["evidence"]["sparse_geometry_observation"]["points_with_track_length_minimum_inside_candidate"], 0)

        review_candidate = json.loads(json.dumps(artifact))
        review_candidate["review"]["ground_plane_reviewed"] = True
        approved = approve_navigation_artifact(review_candidate, "Local reviewer", "Reviewed camera support and exploration limits.",
                                               timestamp="2026-11-07T00:00:00Z", source_artifact_sha256="b" * 64)
        self.assertEqual(approved["status"], "NAVIGATION_APPROVED_BY_REVIEW")
        envelope = approved["viewer_compatibility"]["envelope"]
        self.assertIsNone(envelope["floor"])
        self.assertIsNone(envelope["ceiling"])
        self.assertFalse(envelope["explorationLimits"]["physicalCeiling"])

    def test_far_background_points_and_nonmetric_scale_do_not_expand_camera_boundary(self):
        source = outdoor_source(scale=0.01)
        expected_extent = max(abs(p["centerCommonViewer"][0]) for p in source["stations"])
        artifact = generate_navigation(source, {"scene_type": "auto", "camera_clearance_units": 0})
        self.assertEqual(artifact["status"], "NAVIGATION_NEEDS_REVIEW")
        region = artifact["regions"][0]
        max_boundary = max(max(abs(coordinate) for coordinate in point) for point in region["polygon_uv"])
        self.assertLess(max_boundary, expected_extent * 2)
        self.assertLess(region["exploration_limits"]["maximum_height"]["value"], 0.1)
        self.assertGreater(artifact["coverage_diagnostics"]["sparse_point_count"],
                           region["evidence"]["sparse_geometry_observation"]["points_with_track_length_minimum_inside_candidate"])

    def test_missing_or_degenerate_ground_evidence_stays_insufficient(self):
        source = outdoor_source(scale=1.0)
        source["sparse_points"] = [[100 + i, 0, 100 + i] for i in range(40)]
        source["sparse_point_tracks"] = [4] * len(source["sparse_points"])
        artifact = generate_navigation(source, {"scene_type": "outdoor", "camera_clearance_units": 0})
        self.assertEqual(artifact["status"], "NAVIGATION_INSUFFICIENT_GEOMETRY")
        self.assertEqual(artifact["regions"], [])
        self.assertEqual(artifact["coverage_diagnostics"]["camera_used_count"], 0)
        self.assertEqual(artifact["pose_evidence"]["excluded_pose_count"], 0)
        self.assertEqual(len(artifact["pose_evidence"]["registered_camera_position_candidates"]),
                         artifact["pose_evidence"]["input_pose_count"])
        self.assertEqual(artifact["pose_evidence"]["geometry_unbounded_station_count"],
                         artifact["pose_evidence"]["input_pose_count"])
        self.assertTrue(artifact["coverage_diagnostics"]["excluded_camera_components"])
        excluded = artifact["coverage_diagnostics"]["excluded_camera_components"][0]
        self.assertTrue(excluded["component_id"].startswith("camera-component-"))
        exclusion_warning = next(item for item in artifact["review"]["warnings"]
                                 if item["code"] == "CAMERA_COMPONENT_EXCLUDED_INSUFFICIENT_GEOMETRY")
        self.assertIn(excluded["component_id"], exclusion_warning["message"])

        collinear = source_for([[x, 0, 2] for x in range(12)], floor=False, ceiling=False)
        collinear["coordinate_frame"]["navigation_up_viewer"] = [0, 0, 1]
        self.assertEqual(generate_navigation(collinear)["status"], "NAVIGATION_INSUFFICIENT_GEOMETRY")

    def test_arbitrary_source_rotation_uses_one_right_handed_viewer_frame(self):
        rotation = [[1, 0, 0], [0, 0, 1], [0, -1, 0]]  # source +Z maps to Viewer +Y
        source = outdoor_source(scale=1.0, source_z_up=True)
        source["coordinate_frame"] = {"convention": "SYNTHETIC_Z_UP_SFM", "scale_status": "UNKNOWN_NON_METRIC",
                                       "navigation_up_viewer": [0, 1, 0],
                                       "source_to_viewer": {"uniform_scale": 1.0, "rotation": rotation,
                                                             "translation": [3, 4, 5], "verified": True}}
        artifact = generate_navigation(source, {"scene_type": "outdoor", "camera_clearance_units": 0})
        self.assertEqual(artifact["coordinate_frame"]["viewer_to_navigation"]["up_viewer"], [0.0, 1.0, 0.0])
        center = artifact["pose_evidence"]["candidate_camera_centers"][0]["viewer_center"]
        raw = source["stations"][0]["center"]
        self.assertAlmostEqual(center[0], raw[0] + 3)
        self.assertAlmostEqual(center[1], raw[2] + 4)
        self.assertAlmostEqual(center[2], -raw[1] + 5)
        self.assertTrue(artifact["coordinate_frame"]["viewer_transform_verified"])

    def test_default_camera_outside_proposal_is_rejected(self):
        artifact = generate_navigation(outdoor_source(), {"scene_type": "outdoor", "camera_clearance_units": 0})
        default_id = artifact["viewer_compatibility"]["stations"]["defaultStationId"]
        default = next(row for row in artifact["pose_evidence"]["candidate_camera_centers"] if row["station_id"] == default_id)
        default["navigation_local_uvh"][0] += 100
        check = validate_navigation_artifact(artifact)
        self.assertEqual(check["initial_camera_validation"]["status"], "INVALID")
        self.assertEqual(check["initial_camera_validation"]["error"], "default_camera_outside_navigation_region")

    def test_aligned_lidar_plane_is_recorded_as_a_review_only_navigation_input(self):
        points = [[x, y, 2] for x, y in [(0, 0), (1, 0), (2, 0), (2, 1), (1, 2), (0, 2), (0, 1)]]
        source = source_for(points, ceiling=False)
        source["floor_plane"] = {"normal": [0, 0, 1], "offset": 0, "coordinate_frame": "viewer",
                                 "classification": "PROPOSED_REQUIRES_HUMAN_GROUND_IDENTIFICATION",
                                 "source": "aligned synthetic LiDAR candidate",
                                 "evidence": {"plane_inlier_count": 250}}
        source["aligned_lidar_geometry_used"] = True
        source["aligned_lidar_diagnostics"] = {"status": "ALIGNED_GEOMETRY_CONSUMED_FOR_NAVIGATION_DIAGNOSTICS",
                                                "navigation_consumed": True, "sampled_point_count": 300}

        artifact = generate_navigation(source, {"scene_type": "outdoor", "camera_clearance_units": 0})

        self.assertTrue(artifact["coverage_assumptions"]["aligned_lidar_used"])
        self.assertEqual(artifact["coverage_assumptions"]["ground_plane"],
                         "PROPOSED_REQUIRES_HUMAN_IDENTIFICATION")
        self.assertTrue(artifact["coverage_assumptions"]["aligned_lidar_diagnostics"]["navigation_consumed"])
        self.assertIn("GROUND_PLANE_PROPOSAL_REQUIRES_HUMAN_IDENTIFICATION",
                      [warning["code"] for warning in artifact["validation"]["warnings"]])
        self.assertFalse(artifact["review"]["approved"])

    def test_approval_keeps_inferred_ceiling_classification_explicit(self):
        points = [[x, y, 2] for x in range(5) for y in range(5)]
        artifact = generate_navigation(source_for(points), {"camera_clearance_units": 0})
        artifact["regions"][0]["vertical_bounds"]["ceiling"]["classification"] = "INFERRED"
        approved = approve_navigation_artifact(artifact, "validation-reviewer", "Validation-only ceiling review.",
                                              timestamp="2026-10-29T00:00:00Z", source_artifact_sha256="a" * 64)
        ceiling = approved["regions"][0]["vertical_bounds"]["ceiling"]
        self.assertEqual(ceiling["classification"], "INFERRED")
        self.assertEqual(ceiling["review_status"], "REVIEWED")
        self.assertEqual(approved["viewer_compatibility"]["envelope"]["ceiling"]["classification"], "INFERRED")

    def test_guide_paths_are_swept_and_can_be_rejected(self):
        points = [[x, y, 2] for x in range(6) for y in range(6) if x < 3 or y < 3]
        artifact = generate_navigation(source_for(points), {"camera_clearance_units": 0})
        result = validate_guided_path(artifact, [[2, 5, 1], [5, 2, 1]])
        self.assertFalse(result["valid"])
        self.assertEqual(result["invalid_segment_indices"], [0])


class NavigationReviewAuditTests(unittest.TestCase):
    def test_changed_navigation_inputs_revoke_approval_and_retain_audit_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = ProjectState(Path(tmp))
            prior = {"decision": "APPROVE", "reviewer": "Validation reviewer", "candidate_sha256": "abc123",
                     "navigation_input_fingerprint": "old-fingerprint"}
            state.db.execute("INSERT INTO review_gates(gate_id,status,required,updated_at,decision_json) VALUES(?,?,?,?,?)",
                             ("navigation-approval", "APPROVED", 1, "2026-01-01T00:00:00Z", json.dumps(prior)))
            state.db.commit()
            record = invalidate_navigation_approval(state, "Boundary inputs changed", "new-fingerprint")
            gate = state.db.execute("SELECT status,decision_json FROM review_gates WHERE gate_id='navigation-approval'").fetchone()
            history = state.db.execute("SELECT reviewer,decision,payload_json FROM review_history WHERE gate_id='navigation-approval'").fetchone()
            self.assertEqual(gate["status"], "INVALIDATED")
            self.assertEqual(record["previous_decision"], prior)
            self.assertEqual(record["current_navigation_input_fingerprint"], "new-fingerprint")
            self.assertEqual(history["reviewer"], "Exhibition Archive Engine")
            self.assertEqual(history["decision"], "INVALIDATED")
            self.assertEqual(json.loads(history["payload_json"])["previous_decision"], prior)
            self.assertIsNone(invalidate_navigation_approval(state, "Repeated invalidation", "new-fingerprint"))
            state.close()


class WorkflowIntegrationTests(unittest.TestCase):
    def _write_minimal_colmap(self, root: Path):
        root.mkdir(parents=True)
        (root / "cameras.bin").write_bytes(struct.pack("<Q", 0))
        (root / "images.bin").write_bytes(struct.pack("<Q", 0))
        (root / "points3D.bin").write_bytes(struct.pack("<Q", 0))

    def _write_colmap_camera_grid(self, root: Path):
        root.mkdir(parents=True)
        (root / "cameras.bin").write_bytes(struct.pack("<QiiQQ3d", 1, 1, 0, 1920, 1080, 1000, 960, 540))
        images = bytearray(struct.pack("<Q", 25))
        image_id = 0
        for x in range(5):
            for y in range(5):
                image_id += 1
                images.extend(struct.pack("<I4d3dI", image_id, 1, 0, 0, 0, -x, -y, -2, 1))
                images.extend(f"grid-{image_id:03d}.jpg".encode() + b"\0")
                images.extend(struct.pack("<Q", 0))
        (root / "images.bin").write_bytes(images)
        (root / "points3D.bin").write_bytes(struct.pack("<Q", 0))

    def test_generate_from_sfm_reads_existing_colmap_poses_as_provisional_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            source_dir = root / "photos"; source_dir.mkdir(parents=True)
            sfm_dir = root / "sfm"; self._write_colmap_camera_grid(sfm_dir)
            frame = {"source_to_viewer": {"uniform_scale": 1, "rotation": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                                           "translation": [0, 0, 0], "verified": True}}
            config = {"schema_version": "exhibition-archive.project.v1", "project_id": "colmap-navigation-fixture",
                      "source": {"photo_directory": str(source_dir), "read_only": True}, "output_directory": "output",
                      "integrations": {"sfm": {"mode": "adopt_existing", "directory": str(sfm_dir)},
                                       "navigation": {"mode": "generate_from_sfm", "coordinate_frame": frame,
                                                      "floor_plane": {"normal": [0, 0, 1], "offset": 0, "coordinate_frame": "source", "classification": "GEOMETRY_SUPPORTED"},
                                                      "ceiling": {"value": 4, "classification": "REVIEW_CANDIDATE"},
                                                      "camera_clearance_units": 0}},
                      "publication": {"public_site": True}}
            (root / "project.json").write_text(json.dumps(config), encoding="utf-8")
            state = ProjectState(root)
            try:
                result = run_workflow(root, config, state)
                self.assertEqual(result["stages"]["navigation"], "NEEDS_REVIEW")
                details = json.loads(state.stage("navigation")["details_json"])
                proposal = json.loads(Path(details["proposal_path"]).read_text())
                self.assertEqual(proposal["pose_evidence"]["input_pose_count"], 25)
                self.assertEqual(proposal["pose_evidence"]["provisional_registered_pose_count"], 25)
                self.assertEqual(proposal["pose_evidence"]["trusted_camera_centers"], [])
                self.assertEqual(len(proposal["pose_evidence"]["provisional_camera_centers"]), 25)
                summary = json.loads((root / "output" / "work" / "navigation-summary.json").read_text())
                self.assertEqual(summary["registered_camera_position_candidate_count"], 25)
                self.assertEqual(summary["explicitly_trusted_input_camera_count"], 0)
                self.assertEqual(summary["provisional_registered_camera_position_count"], 25)
                self.assertEqual(summary["geometry_unbounded_camera_position_count"], 0)
                self.assertNotIn("trusted_camera_count", summary)
                self.assertNotIn("training", state.stages())
            finally:
                state.close()

    def test_navigation_stage_stops_for_review_before_training(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            source_dir = root / "photos"; source_dir.mkdir(parents=True)
            sfm_dir = root / "sfm"; self._write_minimal_colmap(sfm_dir)
            centers_path = root / "camera-centers.json"
            points = [[x, y, 2] for x in range(5) for y in range(5)]
            centers_path.write_text(json.dumps({"stations": [{"stationId": str(i), "centerCommonViewer": [x, y, h]} for i, (x, y, h) in enumerate(points)]}), encoding="utf-8")
            config = {"schema_version": "exhibition-archive.project.v1", "project_id": "synthetic-navigation",
                      "source": {"photo_directory": str(source_dir), "read_only": True}, "output_directory": "output",
                      "integrations": {"sfm": {"mode": "adopt_existing", "directory": str(sfm_dir)},
                                       "navigation": {"mode": "generate_from_sfm", "camera_centers": str(centers_path),
                                                      "coordinate_frame": {"viewer_transform_verified": True},
                                                      "floor_plane": {"normal": [0, 0, 1], "offset": 0, "coordinate_frame": "viewer", "classification": "GEOMETRY_SUPPORTED"},
                                                      "ceiling": {"value": 4, "classification": "INFERRED"}, "camera_clearance_units": 0}},
                      "publication": {"public_site": True}}
            (root / "project.json").write_text(json.dumps(config), encoding="utf-8")
            state = ProjectState(root)
            try:
                result = run_workflow(root, config, state)
                self.assertEqual(result["stages"]["navigation"], "NEEDS_REVIEW")
                self.assertNotIn("training", state.stages())
                details = json.loads(state.stage("navigation")["details_json"])
                self.assertTrue(Path(details["proposal_path"]).is_file())
                state.close()
                cli = Path(__file__).resolve().parents[1] / "bin" / "exhibition-archive"
                approval = subprocess.run([str(cli), "navigation", "review", str(root), "--decision", "approve",
                                           "--reviewer", "synthetic-reviewer", "--rationale", "Verified synthetic fixture geometry."],
                                          text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                self.assertEqual(approval.returncode, 0, approval.stdout)
                approval_result = json.loads(approval.stdout)
                self.assertTrue(approval_result["publishing_ready"])
                summary = json.loads((root / "output" / "work" / "navigation-summary.json").read_text())
                self.assertEqual(summary["approval_status"], "APPROVED")
                self.assertTrue(summary["publishing_ready"])
                state = ProjectState(root)
                self.assertEqual(state.stage("navigation")["status"], "ADOPTED")
                nav_fp = stage_fingerprint(root, config, state, "navigation")
                config["integrations"]["model"] = {"mode": "spirula_fresh", "iterations": 500}
                self.assertEqual(nav_fp, stage_fingerprint(root, config, state, "navigation"))
                config["integrations"]["navigation"]["margin_units"] = 0.05
                self.assertNotEqual(nav_fp, stage_fingerprint(root, config, state, "navigation"))
            finally:
                state.close()

    def test_navigation_only_generation_does_not_run_upstream_or_downstream_stages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            source_dir = root / "photos"; source_dir.mkdir(parents=True)
            sfm_dir = root / "sfm"; self._write_minimal_colmap(sfm_dir)
            config = {"schema_version": "exhibition-archive.project.v1", "project_id": "navigation-only-fixture",
                      "source": {"photo_directory": str(source_dir), "read_only": True}, "output_directory": "output",
                      "integrations": {"sfm": {"mode": "adopt_existing", "directory": str(sfm_dir)},
                                       "navigation": {"mode": "generate_from_sfm"}},
                      "publication": {"public_site": False}}
            (root / "project.json").write_text(json.dumps(config), encoding="utf-8")
            state = ProjectState(root)
            try:
                sfm_fingerprint = stage_fingerprint(root, config, state, "sfm", full_hash=True)
                fixture_run = state.start_run("fixture")
                artifact = root / "sfm-fixture.json"
                artifact.write_text("{}", encoding="utf-8")
                sfm_execution = state.start_stage(fixture_run, "sfm", sfm_fingerprint, True, {})
                state.finish_stage(sfm_execution, "COMPLETED", "fixture-digest",
                                   {"artifacts": [str(artifact)]})
                state.set_stage("sfm", "COMPLETED", sfm_fingerprint, "fixture-digest", sfm_execution,
                                {"reconstruction_directory": str(sfm_dir)})
                state.end_run(fixture_run, "COMPLETED", {"status": "COMPLETED"})
                prior_lidar_executions = state.db.execute(
                    "SELECT COUNT(*) FROM stage_executions WHERE stage_id='lidar_registration'").fetchone()[0]

                result = run_navigation_generation_only(root, config, state)

                self.assertEqual(result["status"], "NEEDS_REVIEW")
                self.assertEqual(result["mode"], "navigation_generate_only")
                self.assertEqual(state.stage("sfm")["last_execution_id"], sfm_execution)
                self.assertIsNone(state.stage("lidar_registration"))
                self.assertIsNone(state.stage("training"))
                self.assertEqual(state.db.execute(
                    "SELECT COUNT(*) FROM stage_executions WHERE stage_id='lidar_registration'").fetchone()[0],
                    prior_lidar_executions)
                self.assertTrue(Path(json.loads(state.stage("navigation")["details_json"])["proposal_path"]).is_file())
            finally:
                state.close()


if __name__ == "__main__":
    unittest.main()
