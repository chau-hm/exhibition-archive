from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from exhibition_archive.preview import _next_first_visual_revision
from exhibition_archive.state import ProjectState
from exhibition_archive.workflow import run_first_visual_preview_only, stage_fingerprint


class PreviewRevisionTests(unittest.TestCase):
    def test_revision_allocator_skips_existing_manual_revisions(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            existing = output / "first-visual" / "revision-000014"
            existing.mkdir(parents=True)
            revision, path = _next_first_visual_revision(output, 14)
            self.assertEqual(revision, 15)
            self.assertEqual(path, output / "first-visual" / "revision-000015")
            self.assertTrue(existing.is_dir())


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
