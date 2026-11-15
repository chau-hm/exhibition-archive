from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from exhibition_archive.cli import build_parser
from exhibition_archive.adapters import sha256_file, write_json
from exhibition_archive.publisher import visitor_xr_preflight
from exhibition_archive.visitor import enclosure_collision_glb, write_glb
from exhibition_archive.visitor_xr import visitor_xr_configuration


ROOT = Path(__file__).resolve().parents[1]


class VisitorXrConfigurationTests(unittest.TestCase):
    def test_xr_is_disabled_by_default_and_retains_generic_boundary_defaults(self):
        profile = visitor_xr_configuration({})
        self.assertFalse(profile["enabled"])
        self.assertEqual(profile["renderer"], "auto")
        self.assertEqual(profile["recenter_spawn"], "opening")
        self.assertEqual(profile["boundary_feedback"]["approach_distance_viewer_units"], 0.85)
        self.assertFalse(profile["diagnostics"]["enabled"])

    def test_independent_scene_configuration_is_validated_without_scene_constants(self):
        config = {"publication": {"visitor_xr": {
            "enabled": True,
            "renderer": "webgl",
            "boundary_feedback": {"approach_distance_viewer_units": 1.5,
                                   "outside_enter_clearance_viewer_units": 0.05,
                                   "outside_exit_clearance_viewer_units": 0.25,
                                   "fade_duration_ms": 600,
                                   "maximum_vignette_opacity": 0.4},
            "diagnostics": {"enabled": True},
        }}}
        profile = visitor_xr_configuration(config)
        self.assertTrue(profile["enabled"])
        self.assertEqual(profile["renderer"], "webgl")
        self.assertEqual(profile["boundary_feedback"]["approach_distance_viewer_units"], 1.5)
        self.assertTrue(profile["diagnostics"]["enabled"])

    def test_invalid_boundary_order_and_renderer_fail_with_actionable_errors(self):
        with self.assertRaisesRegex(ValueError, "outside-enter <= outside-exit < approach-distance"):
            visitor_xr_configuration({"publication": {"visitor_xr": {
                "enabled": True,
                "boundary_feedback": {"outside_exit_clearance_viewer_units": 2},
            }}})
        with self.assertRaisesRegex(ValueError, "renderer must be"):
            visitor_xr_configuration({"publication": {"visitor_xr": {"renderer": "vulkan"}}})

    def test_package_dry_run_checks_xr_readiness_without_creating_state_or_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary) / "fixture"
            subprocess.run([sys.executable, str(ROOT / "tests" / "generate_independent_fixture.py"), str(fixture)],
                           check=True, stdout=subprocess.PIPE, text=True)
            synthetic_model = fixture / "inputs" / "gallery-model.ply"
            self.assertTrue(synthetic_model.read_bytes().startswith(b"ply\nformat binary_little_endian 1.0\n"))
            config = json.loads((fixture / "project-config.json").read_text(encoding="utf-8"))
            config["publication"]["visitor_xr"] = {
                "enabled": True,
                "renderer": "auto",
                "diagnostics": {"enabled": True},
            }
            workspace = fixture / "workspace"
            workspace.mkdir()
            write_json(workspace / "project.json", config)
            report = visitor_xr_preflight(workspace, config)
            self.assertEqual(report["status"], "READY", report)
            self.assertEqual(report["viewer_version"], "1.42.0")
            self.assertEqual(report["playcanvas_version"], "2.23.3")
            self.assertEqual(report["patch_sha256"], "94d9a4c4ad9612385708c21602d5a7f481d352468667dee6d3335beb6f4478ac")
            self.assertEqual(report["camera_readiness"]["status"], "PASS")
            self.assertEqual(report["camera_readiness"]["station_id"], "west-station")

            args = build_parser().parse_args(["package", str(workspace), "--dry-run"])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(args.func(args), 0)
            document = json.loads(output.getvalue())
            self.assertEqual(document["xr_readiness"]["status"], "READY")
            self.assertFalse((workspace / ".engine").exists())
            self.assertFalse((fixture / "deliverable").exists())

            missing_transform = dict(config)
            missing_transform["integrations"] = {
                **config["integrations"],
                "navigation": {
                    **config["integrations"]["navigation"],
                    "transform": str(fixture / "inputs" / "missing-transform.json"),
                },
            }
            missing_report = visitor_xr_preflight(workspace, missing_transform)
            self.assertEqual(missing_report["status"], "BLOCKED")
            self.assertTrue(any("XR enclosure validation failed" in item for item in missing_report["blockers"]))

            invalid_transform_path = fixture / "inputs" / "invalid-transform.json"
            write_json(invalid_transform_path, {"transform": {"viewerSceneNode": {
                "translation": [0, 0, 0], "rotationQuaternionXYZW": [0, 0, 0, 1], "uniformScale": 0,
            }}})
            invalid_transform = dict(config)
            invalid_transform["integrations"] = {
                **config["integrations"],
                "navigation": {
                    **config["integrations"]["navigation"],
                    "transform": str(invalid_transform_path),
                },
            }
            invalid_report = visitor_xr_preflight(workspace, invalid_transform)
            self.assertEqual(invalid_report["status"], "BLOCKED")
            self.assertTrue(any("XR camera/scene-transform preflight failed" in item
                                for item in invalid_report["blockers"]))

            navigation = config["integrations"]["navigation"]
            envelope = json.loads((workspace / navigation["envelope"]).resolve().read_text(encoding="utf-8"))
            transform = json.loads((workspace / navigation["transform"]).resolve().read_text(encoding="utf-8"))
            collision_data, _ = enclosure_collision_glb(envelope, transform)
            approved_path = fixture / "inputs" / "approved-enclosure.glb"
            write_glb(approved_path, collision_data)
            adopted_collision = json.loads(json.dumps(config))
            adopted_collision["integrations"]["navigation"]["approved_collision_glb"] = {
                "path": str(approved_path), "sha256": sha256_file(approved_path),
            }
            approved_report = visitor_xr_preflight(workspace, adopted_collision)
            self.assertEqual(approved_report["status"], "READY", approved_report)
            self.assertEqual(approved_report["camera_readiness"]["collision_geometry"]["source"],
                             "APPROVED_INPUT_ASSET")
            self.assertEqual(approved_report["camera_readiness"]["collision_geometry"]["triangle_count"], 12)
            adopted_collision["integrations"]["navigation"]["approved_collision_glb"]["sha256"] = "0" * 64
            stale_asset = visitor_xr_preflight(workspace, adopted_collision)
            self.assertEqual(stale_asset["status"], "BLOCKED")
            self.assertTrue(any("does not match its expected SHA-256" in item
                                for item in stale_asset["blockers"]))

    def test_preflight_reports_standard_publication_without_xr_dependencies(self):
        self.assertEqual(visitor_xr_preflight(Path.cwd(), {"project_id": "standard"})["status"], "NOT_REQUESTED")

    def test_preflight_uses_reviewed_generated_navigation_artifact_for_camera_readiness(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary) / "fixture"
            subprocess.run([sys.executable, str(ROOT / "tests" / "generate_independent_fixture.py"), str(fixture)],
                           check=True, stdout=subprocess.PIPE, text=True)
            root = fixture / "workspace"
            config = json.loads((fixture / "project-config.json").read_text(encoding="utf-8"))
            config["publication"]["visitor_xr"] = {"enabled": True, "renderer": "auto"}
            navigation = config["integrations"]["navigation"]
            navigation["mode"] = "generate_from_sfm"
            inputs = fixture / "inputs"
            approved = {
                "status": "NAVIGATION_APPROVED_BY_REVIEW",
                "validation": {"status": "PASS", "public_ready": True},
                "provenance": {"navigation_input_fingerprint": "a" * 64},
                "coordinate_frame": json.loads((inputs / "transform-alpha.json").read_text())["transform"],
                "viewer_compatibility": {
                    "envelope": json.loads((inputs / "envelope-alpha.json").read_text()),
                    "stations": json.loads((inputs / "stations-alpha.json").read_text()),
                    "runtime_policy": json.loads((inputs / "policy-alpha.json").read_text()),
                },
            }
            approved_path = root / "deliverable" / "work" / "navigation-approved.json"
            write_json(approved_path, approved)

            report = visitor_xr_preflight(root, config)
            self.assertEqual(report["status"], "READY", report)
            self.assertEqual(report["navigation_mode"], "generate_from_sfm")
            self.assertEqual(report["camera_readiness"]["status"], "PASS")
            self.assertEqual(report["camera_readiness"]["station_id"], "west-station")

            approved["status"] = "NAVIGATION_NEEDS_REVIEW"
            write_json(approved_path, approved)
            blocked = visitor_xr_preflight(root, config)
            self.assertEqual(blocked["status"], "BLOCKED")
            self.assertTrue(any("navigation approval artifact is not approved" in item
                                for item in blocked["blockers"]))


if __name__ == "__main__":
    unittest.main()
