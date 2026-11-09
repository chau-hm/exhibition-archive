from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from exhibition_archive.cli import _first_visual_config, _persist_project_configuration
from exhibition_archive.state import ProjectState
from exhibition_archive.training_profiles import (
    DEFAULT_TRAINING_PROFILE,
    apply_training_profile,
    apply_training_profile_defaults,
    load_training_profile,
)
from exhibition_archive.workflow import stage_fingerprint


class TrainingProfileTests(unittest.TestCase):
    def test_new_folder_run_resolves_versioned_defaults_before_engine_fallbacks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"
            photos.mkdir()
            output = root / "output"
            workspace, config = _first_visual_config(photos, output, "profile-test")

            self.assertEqual(workspace, (output / ".engine").resolve())
            model = config["integrations"]["model"]
            self.assertEqual(model["quality"], "high")
            self.assertEqual(model["iterations"], 30000)
            self.assertEqual(model["resolution_divisor"], 1)
            self.assertEqual(model["view_sampling"], "uniform")
            self.assertEqual(model["cache_images"], "disk")
            self.assertIs(model["load_masks"], False)
            metadata = config["training_profile"]
            self.assertEqual(f"{metadata['id']}@{metadata['revision']}", DEFAULT_TRAINING_PROFILE)
            self.assertEqual(metadata["selection_source"], "default")
            self.assertEqual(metadata["profile_defaults"]["iterations"], 30000)
            self.assertEqual(metadata["profile_provided_fields"]["integrations.model.iterations"], 30000)
            self.assertEqual(metadata["effective_training_configuration"], model)

    def test_profile_fills_only_missing_and_preserves_explicit_false_and_zero(self):
        config = {"integrations": {"model": {
            "quality": "medium", "load_masks": False, "seed": 0,
        }}}
        metadata = apply_training_profile(config, DEFAULT_TRAINING_PROFILE)

        model = config["integrations"]["model"]
        self.assertEqual(model["quality"], "medium")
        self.assertIs(model["load_masks"], False)
        self.assertEqual(model["seed"], 0)
        self.assertEqual(model["iterations"], 30000)
        self.assertNotIn("integrations.model.quality", metadata["profile_provided_fields"])
        self.assertIn("integrations.model.quality", metadata["explicitly_configured_fields"])
        self.assertIn("integrations.model.load_masks", metadata["explicitly_configured_fields"])

    def test_profile_application_is_deterministic_and_revision_is_pinned(self):
        first = {"integrations": {"model": {}}}
        second = json.loads(json.dumps(first))
        apply_training_profile(first, DEFAULT_TRAINING_PROFILE)
        apply_training_profile(second, DEFAULT_TRAINING_PROFILE)
        self.assertEqual(first, second)
        self.assertEqual(first["training_profile"]["revision"], "v1")

    def test_unknown_profile_and_unsupported_revision_fail_clearly(self):
        with self.assertRaisesRegex(ValueError, "Unknown training profile"):
            load_training_profile("unknown@v1")
        with self.assertRaisesRegex(ValueError, "Unsupported revision"):
            load_training_profile("indoor-exhibition-high-quality@v2")
        with self.assertRaisesRegex(ValueError, "explicit id@revision"):
            load_training_profile("indoor-exhibition-high-quality")

    def test_profile_helper_rejects_modified_registered_revision_content(self):
        profile = load_training_profile(DEFAULT_TRAINING_PROFILE)
        profile["training_defaults"]["iterations"] = 1
        with self.assertRaisesRegex(ValueError, "immutable registered revision"):
            apply_training_profile_defaults({"integrations": {"model": {}}}, profile)

    def test_same_revision_default_drift_is_rejected(self):
        config = {"integrations": {"model": {}}}
        apply_training_profile(config, DEFAULT_TRAINING_PROFILE)
        config["training_profile"]["profile_defaults"]["iterations"] = 20000
        with self.assertRaisesRegex(ValueError, "changed without a revision bump"):
            apply_training_profile(config, DEFAULT_TRAINING_PROFILE)

    def test_existing_folder_project_is_unchanged_without_explicit_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"
            photos.mkdir()
            output = root / "output"
            workspace, created = _first_visual_config(photos, output, "existing-test")
            legacy = json.loads(json.dumps(created))
            legacy.pop("training_profile")
            legacy["integrations"]["model"].pop("view_sampling")
            legacy["integrations"]["model"].pop("cache_images")
            legacy["integrations"]["model"].pop("load_masks")
            workspace.mkdir(parents=True)
            (workspace / "project.json").write_text(json.dumps(legacy), encoding="utf-8")

            existing_root, loaded = _first_visual_config(photos, output, "existing-test")

            self.assertEqual(existing_root, workspace)
            self.assertEqual(loaded, legacy)

    def test_existing_project_requires_explicit_profile_and_keeps_existing_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"
            photos.mkdir()
            output = root / "output"
            workspace, created = _first_visual_config(photos, output, "explicit-profile-test")
            existing = json.loads(json.dumps(created))
            existing.pop("training_profile")
            model = existing["integrations"]["model"]
            model["quality"] = "medium"
            model["load_masks"] = False
            model.pop("view_sampling")
            model.pop("cache_images")
            workspace.mkdir(parents=True)
            (workspace / "project.json").write_text(json.dumps(existing), encoding="utf-8")

            _, resolved = _first_visual_config(photos, output, "explicit-profile-test",
                                               training_profile=DEFAULT_TRAINING_PROFILE)

            self.assertEqual(resolved["integrations"]["model"]["quality"], "medium")
            self.assertIs(resolved["integrations"]["model"]["load_masks"], False)
            self.assertEqual(resolved["integrations"]["model"]["view_sampling"], "uniform")
            self.assertEqual(resolved["training_profile"]["selection_source"], "operator")

    def test_profile_metadata_does_not_change_training_fingerprint_but_effective_values_do(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"
            photos.mkdir()
            config = {
                "project_id": "fingerprint-test",
                "source": {"photo_directory": str(photos)},
                "integrations": {"model": {"mode": "adopt_existing", "path": "model.ply",
                                             "iterations": 100, "quality": "high"}},
            }
            state = ProjectState(root)
            try:
                baseline = stage_fingerprint(root, config, state, "training")
                metadata_only = json.loads(json.dumps(config))
                metadata_only["training_profile"] = {
                    "schema_version": "exhibition-archive.training-profile-provenance.v1",
                    "id": "indoor-exhibition-high-quality", "revision": "v1",
                    "selection_source": "default",
                    "profile_defaults": load_training_profile(DEFAULT_TRAINING_PROFILE)["training_defaults"],
                    "profile_provided_fields": {}, "explicitly_configured_fields": [],
                    "effective_training_configuration": metadata_only["integrations"]["model"],
                }
                self.assertEqual(baseline, stage_fingerprint(root, metadata_only, state, "training"))
                changed_value = json.loads(json.dumps(metadata_only))
                changed_value["integrations"]["model"]["iterations"] = 200
                self.assertNotEqual(baseline, stage_fingerprint(root, changed_value, state, "training"))
            finally:
                state.close()

    def test_effective_profile_change_uses_selective_training_invalidation(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            photos = base / "photos"
            photos.mkdir()
            root, old = _first_visual_config(photos, base / "output", "invalidate-test")
            root.mkdir(parents=True)
            (root / "project.json").write_text(json.dumps(old), encoding="utf-8")
            state = ProjectState(root)
            state.set_stage("training", "COMPLETED", "existing-fingerprint", "existing-output", None, {})
            state.close()

            changed = json.loads(json.dumps(old))
            changed["integrations"]["model"]["iterations"] = 20000
            invalidated = _persist_project_configuration(root, old, changed, "profile training value changed")

            state = ProjectState(root)
            try:
                self.assertIn("training", invalidated)
                self.assertEqual(state.stage("training")["status"], "INVALIDATED")
            finally:
                state.close()

    def test_metadata_only_profile_update_does_not_invalidate_completed_training(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            photos = base / "photos"
            photos.mkdir()
            root, old = _first_visual_config(photos, base / "output", "metadata-test")
            root.mkdir(parents=True)
            (root / "project.json").write_text(json.dumps(old), encoding="utf-8")
            state = ProjectState(root)
            fingerprint = stage_fingerprint(root, old, state, "training")
            state.set_stage("training", "COMPLETED", fingerprint, "existing-output", None, {})
            state.close()

            changed = json.loads(json.dumps(old))
            changed["training_profile"]["selection_source"] = "operator"
            invalidated = _persist_project_configuration(root, old, changed, "metadata changed")

            state = ProjectState(root)
            try:
                self.assertNotIn("training", invalidated)
                self.assertEqual(state.stage("training")["status"], "COMPLETED")
            finally:
                state.close()


if __name__ == "__main__":
    unittest.main()
