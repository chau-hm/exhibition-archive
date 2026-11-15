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
from exhibition_archive.adapters import write_json
from exhibition_archive.publisher import visitor_xr_preflight
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

            args = build_parser().parse_args(["package", str(workspace), "--dry-run"])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(args.func(args), 0)
            document = json.loads(output.getvalue())
            self.assertEqual(document["xr_readiness"]["status"], "READY")
            self.assertFalse((workspace / ".engine").exists())
            self.assertFalse((fixture / "deliverable").exists())

    def test_preflight_reports_standard_publication_without_xr_dependencies(self):
        self.assertEqual(visitor_xr_preflight(Path.cwd(), {"project_id": "standard"})["status"], "NOT_REQUESTED")


if __name__ == "__main__":
    unittest.main()
