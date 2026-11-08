from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from exhibition_archive.preview import (
    _navigation_coverage_diagnostic,
    _navigation_model_overlay,
    _next_first_visual_revision,
    _write_visitor_assets,
    render_navigation_coverage_html,
)
from exhibition_archive.state import ProjectState
from exhibition_archive.workflow import run_first_visual_preview_only, stage_fingerprint


class PreviewRevisionTests(unittest.TestCase):
    def test_visitor_preview_banner_explicitly_warns_navigation_is_unbounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            visitor = Path(temporary) / "visitor"
            for name in ("config", "css", "js", "vendor"):
                (visitor / name).mkdir(parents=True)
            _write_visitor_assets(visitor, {"project_id": "fixture", "viewer": {"title": "Fixture"}}, {})
            page = (visitor / "index.html").read_text(encoding="utf-8")

        self.assertIn("Free flight is unbounded until navigation approval", page)
        self.assertIn("Not approved for public release", page)

    def test_revision_allocator_skips_existing_manual_revisions(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            existing = output / "first-visual" / "revision-000014"
            existing.mkdir(parents=True)
            revision, path = _next_first_visual_revision(output, 14)
            self.assertEqual(revision, 15)
            self.assertEqual(path, output / "first-visual" / "revision-000015")
            self.assertTrue(existing.is_dir())

    def test_model_overlay_maps_proposal_frame_to_display_frame_without_double_rotation(self):
        half = 2 ** -0.5
        rotation = [[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]]
        frame_matrix = [row + [0.0] for row in rotation] + [[0.0, 0.0, 0.0, 1.0]]
        camera = {"station_id": "camera-1", "image_name": "IMG_001.jpg",
                  "viewer_center": [-2.0, 1.0, 3.0], "viewer_forward": [0.0, 0.0, -1.0],
                  "navigation_local_uvh": [1.0, 1.0, 2.0], "spatial_component_id": "component-1",
                  "component_id": "component-1", "triangulated_observation_count": 40,
                  "capture_sequence_index": 0, "coverage_status": "CAMERA_POSITION_CANDIDATE"}
        region = {"id": "region-1", "polygon_uv": [[0, 0], [2, 0], [2, 2], [0, 2]],
                  "exploration_limits": {"minimum_height": {"value": 0.0}, "maximum_height": {"value": 3.0}},
                  "geometry_candidates": {"ground_plane": {"status": "PROPOSED", "normal": [0, 1, 0],
                                                               "offset": 0.0, "evidence": {"inlier_count": 12}}},
                  "boundary_segments": [], "evidence": {"candidate_station_count": 1,
                                                            "sparse_geometry_observation": {
                                                                "points_with_track_length_minimum_inside_candidate": 3}}}
        artifact = {"status": "NAVIGATION_NEEDS_REVIEW",
                    "coordinate_frame": {"source_to_viewer_matrix_row_major": frame_matrix,
                                         "uniform_scale": 1.0,
                                         "viewer_to_navigation": {"origin_viewer": [0, 0, 0],
                                                                   "u_viewer": [1, 0, 0],
                                                                   "v_viewer": [0, 0, 1],
                                                                   "up_viewer": [0, 1, 0]}},
                    "pose_evidence": {"input_pose_count": 1, "used_camera_station_count": 1,
                                      "excluded_pose_count": 0, "geometry_unbounded_station_count": 0,
                                      "candidate_camera_centers": []},
                    "viewer_compatibility": {}, "regions": [region],
                    "coverage_diagnostics": {"camera_candidate_count": 1, "camera_used_count": 1,
                                              "camera_excluded_count": 0, "camera_component_count": 1,
                                              "camera_trajectory": [{**camera, "viewer_forward": [0, 0, -1]}],
                                              "component_footprints": [{"component_id": "component-1",
                                                                        "median_nearest_neighbor_spacing_units": 1.0}],
                                              "trajectory_reconciliation": {"sequence": {"consecutive_pairs": []},
                                                                             "strategy_comparison": {}}}}
        model_transform = {"translation": [0.0, 0.0, 0.0],
                           "rotationQuaternionXYZW": [0.0, 0.0, half, half], "uniformScale": 1.0}
        gaussian_geometry = {"vertex_count": 2, "sample_count": 2,
                             "raw_exact_bounds": {"minimum": [0.0, 0.0, 0.0], "maximum": [1.0, 1.0, 1.0]},
                             "sampled_points": [[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]]}

        with patch("exhibition_archive.preview._gaussian_xyz_sample", return_value=gaussian_geometry):
            diagnostic = _navigation_coverage_diagnostic(
                artifact, [camera], [], [], model_transform, Path("fixture.ply"))
        overlay = _navigation_model_overlay(diagnostic)

        for actual, expected in zip(diagnostic["cameras"][0]["center"], [-2.0, 1.0, 3.0]):
            self.assertAlmostEqual(actual, expected)
        for actual, expected in zip(overlay["cameras"][0]["center"], [-2.0, 1.0, 3.0]):
            self.assertAlmostEqual(actual, expected)
        self.assertEqual(diagnostic["navigation_to_model_transform"]["relative_uniform_scale"], 1.0)
        self.assertEqual(len(overlay["candidate_v2"]), 1)
        self.assertIn("model_scene_transform composed with inverse proposal source_to_viewer transform",
                      diagnostic["navigation_to_model_transform"]["formula"])
        self.assertNotIn("exhibition.ply", json.dumps(overlay))

        nonmatching = json.loads(json.dumps(artifact))
        sfm_rotation = [[0.0, 0.0, 1.0], [0.0, 1.0, 0.0], [-1.0, 0.0, 0.0]]
        sfm_scale, sfm_translation = 2.0, [10.0, 20.0, 30.0]
        nonmatching["coordinate_frame"]["uniform_scale"] = sfm_scale
        nonmatching["coordinate_frame"]["source_to_viewer_matrix_row_major"] = [
            [sfm_scale * value for value in row] + [sfm_translation[index]]
            for index, row in enumerate(sfm_rotation)
        ] + [[0.0, 0.0, 0.0, 1.0]]
        nonmatching["coordinate_frame"]["viewer_to_navigation"] = {
            "origin_viewer": sfm_translation, "u_viewer": [1, 0, 0],
            "v_viewer": [0, 0, -1], "up_viewer": [0, 1, 0]}
        nonmatching_camera = {**camera, "viewer_center": [16.0, 24.0, 28.0],
                              "viewer_forward": [-1.0, 0.0, 0.0]}
        nonmatching["coverage_diagnostics"]["camera_trajectory"] = [nonmatching_camera]
        different_model_transform = {"translation": [-1.0, 1.0, 2.0],
                                     "rotationQuaternionXYZW": [0.0, 0.0, 0.0, 1.0],
                                     "uniformScale": 4.0}
        with patch("exhibition_archive.preview._gaussian_xyz_sample", return_value=gaussian_geometry):
            mapped = _navigation_coverage_diagnostic(
                nonmatching, [nonmatching_camera], [], [], different_model_transform, Path("fixture.ply"))
        for actual, expected in zip(mapped["cameras"][0]["center"], [3.0, 9.0, 14.0]):
            self.assertAlmostEqual(actual, expected)
        self.assertEqual(mapped["navigation_to_model_transform"]["relative_uniform_scale"], 2.0)

    def test_navigation_geometry_diagnostic_exposes_model_overlay_and_review_layers(self):
        document = {"summary": {"status": "NAVIGATION_NEEDS_REVIEW", "camera_used_count": 1,
                                "registered_camera_count": 1, "strategy_comparison": {}},
                    "navigation_basis": {"origin": [0, 0, 0], "u": [1, 0, 0],
                                         "v": [0, 0, 1], "up": [0, 1, 0]},
                    "cameras": [], "regions": [], "baseline_regions": [],
                    "trajectory_report": {}, "trajectory": {}, "limitations": []}
        page = render_navigation_coverage_html(document)
        self.assertIn("navigationDiagnostics=1", page)
        self.assertIn("Candidate v2 envelope", page)
        self.assertIn("Existing v1 boundary", page)
        self.assertIn("Inferred bridge and open endpoints", page)


class PreviewOnlyWorkflowTests(unittest.TestCase):
    def test_refresh_uses_current_artifacts_without_running_other_stages(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            photos = root / "photos"
            photos.mkdir(parents=True)
            model = root / "model.ply"
            model.write_bytes(b"fixture model")
            proposal = root / "navigation-proposal.json"
            proposal.write_text("{}", encoding="utf-8")
            config = {
                "schema_version": "exhibition-archive.project.v1",
                "project_id": "preview-only-fixture",
                "source": {"photo_directory": str(photos), "read_only": True},
                "output_directory": "output",
                "integrations": {
                    "model": {"mode": "adopt_existing", "path": str(model)},
                    "navigation": {"mode": "generate_from_sfm"},
                },
                "publication": {"public_site": False},
            }
            (root / "project.json").write_text(json.dumps(config), encoding="utf-8")
            state = ProjectState(root)
            try:
                training_fp = stage_fingerprint(root, config, state, "training")
                training_run = state.start_run("fixture")
                training_execution = state.start_stage(training_run, "training", training_fp, True, {})
                state.finish_stage(training_execution, "COMPLETED", "training-digest",
                                   {"artifacts": [str(model)]})
                state.set_stage("training", "COMPLETED", training_fp, "training-digest", training_execution,
                                {"model_path": str(model)})
                state.end_run(training_run, "COMPLETED", {"status": "COMPLETED"})

                navigation_fp = stage_fingerprint(root, config, state, "navigation", full_hash=True)
                state.set_stage("navigation", "NEEDS_REVIEW", navigation_fp, "navigation-digest", None,
                                {"proposal_path": str(proposal)})
                generated = root / "generated-reviewer.html"
                generated.write_text("review", encoding="utf-8")

                with patch("exhibition_archive.preview.build_first_visual_preview",
                           return_value={"status": "COMPLETED", "artifacts": [generated],
                                         "details": {"reviewer_html": str(generated)}}):
                    result = run_first_visual_preview_only(root, config, state)

                self.assertEqual(result["mode"], "first_visual_preview_only")
                self.assertEqual(result["status"], "AWAITING_VISUAL_REVIEW")
                self.assertEqual(state.stage("training")["last_execution_id"], training_execution)
                self.assertIsNone(state.stage("sfm"))
                self.assertIsNone(state.stage("lidar_registration"))
                self.assertIsNone(state.stage("navigation")["last_execution_id"])
                self.assertEqual(model.read_bytes(), b"fixture model")
            finally:
                state.close()
