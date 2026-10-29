from __future__ import annotations

import json
import math
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

from exhibition_archive.navigation import (
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
from exhibition_archive.workflow import run_workflow, stage_fingerprint


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
        with self.assertRaisesRegex(ValueError, "ceiling value"):
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


if __name__ == "__main__":
    unittest.main()
