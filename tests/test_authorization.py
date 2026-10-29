from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from exhibition_archive.state import ProjectState
from exhibition_archive.workflow import (AuthorizationRequired, expensive_execution_plan,
                                         execute_fresh_sfm, run_workflow, stage_fingerprint)


BACKEND = {
    "tool": {"executable": "/opt/test/spirula", "version": "test-1.2"},
    "sfm": {"auto_arguments": ["--max-image-size"]},
    "training": {"presets": ["3dgs"], "arguments": ["--train-resolution-divisor", "--device", "--steps-per-save"]},
}


def project_config(root: Path, *, smoke: bool = False) -> dict:
    return {
        "schema_version": "exhibition-archive.project.v1", "project_id": "authorization-fixture",
        "source": {"photo_directory": str(root / "photos"), "read_only": True}, "output_directory": "output",
        "integrations": {
            "sfm": {"mode": "spirula_fresh", "executable": "/opt/test/spirula", "image_directory": str(root / "photos"), "quality": "low"},
            "model": {"mode": "spirula_fresh", "executable": "/opt/test/spirula", "run_type": "candidate",
                      "preset": "3dgs", "iterations": 100, "resolution_divisor": 2},
        },
        "resource_policy": {"max_training_iterations": 1000, "sfm_timeout_seconds": 120,
                            "training_timeout_seconds": 300, "minimum_free_disk_bytes": 1},
        **({"diagnostic_policy": {"mode": "SMOKE_TEST_ONLY"}} if smoke else {}),
    }


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ea-auth-")
        self.root = Path(self.tmp.name) / "project"
        self.photos = self.root / "photos"
        self.photos.mkdir(parents=True)
        self.image = self.photos / "one.jpg"
        self.image.write_bytes(b"synthetic-photo-content")
        self.config = project_config(self.root)
        (self.root / "project.json").write_text(json.dumps(self.config), encoding="utf-8")
        self.state = ProjectState(self.root)

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def plan(self, stage="sfm"):
        return expensive_execution_plan(self.root, self.config, self.state, stage, backend_info=BACKEND)

    def test_plan_binds_inputs_resources_command_and_training_cap(self):
        first = self.plan("sfm")
        original_stat = self.image.stat()
        self.image.write_bytes(b"synthetic-photo-contenX")
        os.utime(self.image, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        changed_input = self.plan("sfm")
        self.assertNotEqual(first["plan_id"], changed_input["plan_id"])
        self.assertEqual(changed_input["source_image_count"], 1)
        self.assertIn("resource_limits", changed_input)
        self.assertIn("normalized_command", changed_input)

        self.state.set_stage("sfm", "ADOPTED", "sfm-fingerprint", "sfm-digest", None, {})
        training = self.plan("training")
        self.config["integrations"]["model"]["iterations"] = 200
        changed_training = self.plan("training")
        self.assertNotEqual(training["plan_id"], changed_training["plan_id"])

    def test_grant_is_one_time_and_stage_scoped(self):
        plan = self.plan()
        expires = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat().replace("+00:00", "Z")
        self.state.create_execution_authorization("grant-a", self.config["project_id"], "sfm", plan["plan_id"],
                                                  plan, "TEST_ONLY", "Isolated authorization test", expires)
        self.assertTrue(self.state.consume_execution_authorization("grant-a", plan["plan_id"], 42))
        self.assertFalse(self.state.consume_execution_authorization("grant-a", plan["plan_id"], 43))
        record = self.state.execution_authorization("grant-a")
        self.assertEqual(record["status"], "CONSUMED")
        self.assertEqual(record["execution_id"], 42)

    def test_run_without_approval_blocks_before_creating_backend_execution(self):
        with patch("exhibition_archive.workflow.expensive_execution_plan", return_value={"plan_id": "current", "backend": {}, "input_dataset": str(self.photos), "source_image_count": 1, "resource_limits": {}}):
            result = run_workflow(self.root, self.config, self.state)
        self.assertEqual(result["status"], "AUTHORIZATION_REQUIRED")
        self.assertEqual(result["blocked"][0]["stage"], "sfm")
        self.assertIsNone(self.state.stage("sfm")["last_execution_id"])
        self.assertEqual(self.state.db.execute("SELECT COUNT(*) FROM stage_executions WHERE stage_id='sfm'").fetchone()[0], 0)

    def test_changed_plan_invalidates_supplied_grant(self):
        expires = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat().replace("+00:00", "Z")
        self.state.create_execution_authorization("old-grant", self.config["project_id"], "sfm", "old-plan", {"plan_id": "old-plan"}, "TEST_ONLY", "old plan", expires)
        with patch("exhibition_archive.workflow.expensive_execution_plan", return_value={"plan_id": "new-plan", "backend": {}, "input_dataset": str(self.photos), "source_image_count": 1, "resource_limits": {}}):
            result = run_workflow(self.root, self.config, self.state, authorization_ids=["old-grant"])
        self.assertEqual(result["status"], "AUTHORIZATION_REQUIRED")
        self.assertEqual(self.state.execution_authorization("old-grant")["status"], "STALE")
        self.assertEqual(self.state.db.execute("SELECT COUNT(*) FROM stage_executions WHERE stage_id='sfm'").fetchone()[0], 0)

    def test_authorized_attempt_consumes_once_and_resume_needs_new_training_plan(self):
        sfm_plan = {"plan_id": "sfm-plan", "backend": {}, "input_dataset": str(self.photos), "source_image_count": 1, "resource_limits": {}}
        training_plan = {"plan_id": "training-plan", "backend": {}, "input_dataset": str(self.photos), "source_image_count": 1, "resource_limits": {}}
        expires = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat().replace("+00:00", "Z")
        self.state.create_execution_authorization("sfm-grant", self.config["project_id"], "sfm", "sfm-plan", sfm_plan, "TEST_ONLY", "fixture only", expires)
        real_execute_stage = __import__("exhibition_archive.workflow", fromlist=["_execute_stage"])._execute_stage

        def execute(root, config, state, stage_id, output_dir, execution_id, *args, **kwargs):
            if stage_id == "sfm":
                artifact = output_dir / "fixture-sfm.txt"; artifact.parent.mkdir(parents=True, exist_ok=True); artifact.write_text("synthetic validation only")
                return {"status": "COMPLETED", "artifacts": [artifact], "details": {"test_only": True}}
            return real_execute_stage(root, config, state, stage_id, output_dir, execution_id, *args, **kwargs)

        with patch("exhibition_archive.workflow.expensive_execution_plan", side_effect=[sfm_plan, training_plan]), \
             patch("exhibition_archive.workflow._execute_stage", side_effect=execute):
            result = run_workflow(self.root, self.config, self.state, resume=True, authorization_ids=["sfm-grant"])
        self.assertEqual(result["status"], "AUTHORIZATION_REQUIRED")
        self.assertEqual(result["stages"]["sfm"], "COMPLETED")
        self.assertIn("training", [item["stage"] for item in result["blocked"]])
        self.assertEqual(self.state.execution_authorization("sfm-grant")["status"], "CONSUMED")
        self.assertEqual(self.state.stage("training")["status"], "BLOCKED")

    def test_prelaunch_plan_change_does_not_call_backend_process(self):
        image_rows = [{"relative_path": "one.jpg", "sha256": "synthetic", "size_bytes": 23}]
        with patch("exhibition_archive.workflow.backend_image_inventory", return_value={"image_count": 1, "formats": {".jpg": 1}, "images": image_rows, "known_unsupported_images": []}), \
             patch("exhibition_archive.workflow.check_resources", return_value={"status": "PASS"}), \
             patch("exhibition_archive.workflow.discover_environment", return_value=BACKEND), \
             patch("exhibition_archive.workflow.build_sfm_command", return_value=["/opt/test/spirula", "sfm", "auto"]), \
             patch("exhibition_archive.workflow.assert_authorization_plan_current", side_effect=AuthorizationRequired("sfm", "plan changed")) as recheck, \
             patch("exhibition_archive.workflow.execute_process") as launch:
            with self.assertRaises(AuthorizationRequired):
                execute_fresh_sfm(self.root, self.config, self.state, self.root / "output" / "work", 11, "old-plan")
        self.assertEqual(recheck.call_count, 1)
        launch.assert_not_called()

    def test_smoke_test_candidate_cannot_be_authorized_for_production(self):
        self.config["diagnostic_policy"] = {"mode": "SMOKE_TEST_ONLY"}
        with self.assertRaisesRegex(Exception, "diagnostic smoke-training command"):
            run_workflow(self.root, self.config, self.state)
        self.assertEqual(self.state.db.execute("SELECT COUNT(*) FROM stage_executions").fetchone()[0], 0)

    def test_public_geometry_view_config_invalidates_viewer_package(self):
        self.config["viewer"] = {"title": "Test exhibition", "theme": "dark",
                                  "public_geometry_view_ids": ["view-a"],
                                  "default_geometry_view_id": "view-a"}
        first = stage_fingerprint(self.root, self.config, self.state, "viewer_package")
        self.config["viewer"]["public_geometry_view_ids"] = []
        self.config["viewer"].pop("default_geometry_view_id")
        second = stage_fingerprint(self.root, self.config, self.state, "viewer_package")
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
