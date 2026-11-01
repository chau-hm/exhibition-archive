from __future__ import annotations

import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from exhibition_archive.adapters import sha256_file, write_json
from exhibition_archive.backends import (
    BackendError,
    image_inventory,
    prepare_training_dataset,
    validate_colmap_directory,
)
from exhibition_archive.sfm_review import (
    component_acceptance_status,
    review_sfm_component,
    training_component_scope,
)
from exhibition_archive.state import ProjectState
from exhibition_archive.workflow import (
    expensive_execution_plan,
    json_digest,
    plan,
    run_workflow,
    stage_fingerprint,
)

BACKEND = {
    "tool": {"executable": "/opt/test/spirula", "version": "test-1.2"},
    "sfm": {"auto_arguments": ["--max-image-size"]},
    "training": {"presets": ["3dgs"], "arguments": ["--train-resolution-divisor", "--device", "--steps-per-save", "--save-only-latest-checkpoint", "--save-full-checkpoint", "--resume", "--quality", "--view-sampling", "--cache-images", "--load-masks"],
                 "checkpoint": {"latest_only_retention": "VERIFIED_BY_HELP", "latest_only_prunes_older": "VERIFIED_BY_HELP", "full_state": "VERIFIED_BY_HELP", "resume_cli_flag": "VERIFIED_BY_HELP"}},
}

SFM_STAGE = {"id": "sfm", "deps": ["photo_inventory"], "inputs": [], "outputs": [], "mode": "fixture", "resource": "test"}
TRAINING_STAGE = {"id": "training", "deps": ["photo_inventory", "sfm"], "inputs": [], "outputs": [], "mode": "fixture", "resource": "test"}
NAVIGATION_STAGE = {"id": "navigation", "deps": ["sfm"], "inputs": [], "outputs": [], "mode": "fixture", "resource": "test"}


def _write_component(path: Path, names: list[str], *, point_error: float = 0.0) -> None:
    path.mkdir(parents=True, exist_ok=True)
    with (path / "cameras.bin").open("wb") as stream:
        stream.write(struct.pack("<Q", 1))
        stream.write(struct.pack("<iiQQ", 1, 0, 64, 48))
        stream.write(struct.pack("<3d", 50.0, 32.0, 24.0))
    with (path / "images.bin").open("wb") as stream:
        stream.write(struct.pack("<Q", len(names)))
        for index, name in enumerate(names, start=1):
            stream.write(struct.pack("<I7dI", index, 1.0, 0.0, 0.0, 0.0, float(index), 0.0, 0.0, 1))
            stream.write(name.encode("utf-8") + b"\0")
            if index <= 2:
                stream.write(struct.pack("<Q2dQ", 1, 10.0 + index, 12.0, 1))
            else:
                stream.write(struct.pack("<Q", 0))
    with (path / "points3D.bin").open("wb") as stream:
        stream.write(struct.pack("<Q", 1))
        stream.write(struct.pack("<Q3d3BdQ", 1, 0.0, 0.0, 0.0, 128, 128, 128, point_error, 2))
        stream.write(struct.pack("<4i", 1, 0, 2, 0))


class SfmReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ea-sfm-review-")
        self.root = (Path(self.temp.name) / "project").resolve()
        self.photos = self.root / "photos"
        self.photos.mkdir(parents=True)
        self.sparse = self.root / "sfm" / "sparse"
        self.sparse.mkdir(parents=True)
        self.config = {
            "schema_version": "exhibition-archive.project.v1",
            "project_id": "sfm-review-fixture",
            "source": {"photo_directory": str(self.photos), "read_only": True},
            "output_directory": "output",
            "integrations": {
                "sfm": {"mode": "spirula_fresh", "executable": "/opt/test/spirula",
                        "directory": str(self.sparse), "image_directory": str(self.photos),
                        "quality": "low", "quality_gate": {"minimum_registered_images": 8,
                            "minimum_registered_ratio": 0.65,
                            "maximum_mean_point_reprojection_error_px": 2.0,
                            "allow_training_with_warnings": False}},
                "model": {"mode": "spirula_fresh", "executable": "/opt/test/spirula",
                          "image_directory": str(self.photos), "run_type": "candidate",
                          "preset": "3dgs", "iterations": 40, "resolution_divisor": 2},
            },
            "resource_policy": {"max_training_iterations": 100, "training_timeout_seconds": 300,
                                "minimum_free_disk_bytes_training": 1,
                                "checkpoint_replacement_reserve_bytes": 1},
        }
        write_json(self.root / "project.json", self.config)
        self.state = ProjectState(self.root)

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def make_sfm(self, *, source_count: int = 12, component_sizes: tuple[int, ...] = (8, 2),
                 historical_warnings: tuple[str, ...] = ()) -> dict:
        names = [f"IMG_{index:04d}.jpg" for index in range(source_count)]
        for index, name in enumerate(names):
            (self.photos / name).write_bytes(f"synthetic image {index}".encode())
        cursor = 0
        component_dirs = []
        for component_index, size in enumerate(component_sizes):
            component_path = self.sparse / str(component_index)
            _write_component(component_path, names[cursor:cursor + size])
            component_dirs.append(component_path)
            cursor += size
        inventory = image_inventory(self.photos)
        validation = validate_colmap_directory(self.sparse, inventory["images"], self.config["integrations"]["sfm"]["quality_gate"])
        validation["warnings"].extend(historical_warnings)
        self.state.set_stage("photo_inventory", "COMPLETED", "inventory-input", "inventory-output", None, {})
        work = self.root / "output" / "work"
        work.mkdir(parents=True)
        validation_path = work / "sfm-validation.json"
        summary_path = work / "sfm-summary.json"
        write_json(validation_path, validation)
        write_json(summary_path, {"quality_gate": validation["quality_gate"], "warnings": validation["warnings"]})
        artifacts = [validation_path.resolve(), summary_path.resolve()]
        digest = json_digest([{"path": path.relative_to(self.root).as_posix(), "sha256": sha256_file(path)} for path in artifacts])
        run_id = self.state.start_run("fixture")
        fp = stage_fingerprint(self.root, self.config, self.state, "sfm", full_hash=True)
        execution_id = self.state.start_stage(run_id, "sfm", fp, False, {})
        self.state.finish_stage(execution_id, "NEEDS_REVIEW", digest,
                                {"artifacts": [str(path) for path in artifacts], "details": validation})
        details = {"quality_gate": validation["quality_gate"], "warnings": validation["warnings"],
                   "reconstruction_directory": str(component_dirs[0]),
                   "registered_image_count": validation["components"][0]["registered_image_count"]}
        self.state.set_stage("sfm", "NEEDS_REVIEW", fp, digest, execution_id, details)
        self.state.end_run(run_id, "NEEDS_REVIEW", {"fixture": True})
        return {"names": names, "components": component_dirs, "validation": validation}

    def accept(self, component_id: str = "0") -> dict:
        return review_sfm_component(self.root, self.config, self.state, component_id=component_id,
                                    decision="accept-experimental-training", reviewer="TEST_ONLY reviewer",
                                    rationale="Synthetic regression fixture only",
                                    limitations=["TEST_ONLY; experimental training scope only"])

    def test_single_component_acceptance_records_audited_scope_and_preserves_zero_error_warning(self):
        self.make_sfm(source_count=8, component_sizes=(8,))
        result = self.accept()
        self.assertEqual(result["status"], "ACCEPTED_FOR_EXPERIMENTAL_TRAINING")
        stage = self.state.stage("sfm")
        self.assertEqual(stage["status"], "NEEDS_REVIEW")
        decision = result["decision"]
        self.assertEqual(decision["permitted_downstream_purpose"], "EXPERIMENTAL_3DGS_TRAINING_ONLY")
        self.assertFalse(decision["final_archive_completeness_accepted"])
        self.assertIn("REPROJECTION_ERROR_ZERO_OR_UNINFORMATIVE", decision["original_quality_warnings"])
        self.assertEqual(decision["selected_component"]["registered_image_count"], 8)
        self.assertEqual(decision["selected_component"]["mean_point_reprojection_error_px"], 0.0)
        self.assertEqual(decision["historical_preparation_toolchain"], None)
        self.assertIn("python", decision["reviewer_environment"])
        self.assertEqual(self.state.db.execute("SELECT COUNT(*) FROM review_history").fetchone()[0], 1)
        scope = training_component_scope(self.root, self.config, self.state)
        self.assertEqual(scope["component_id"], "0")
        self.assertEqual(scope["registered_image_count"], 8)

    def test_multi_component_acceptance_applies_only_to_selected_component_and_keeps_original_warnings(self):
        self.make_sfm(historical_warnings=("SPIRULA_REPORTED_PARTIAL_OUTPUT",))
        result = self.accept("0")
        self.assertEqual(result["decision"]["original_quality_gate"], "SFM_NEEDS_REVIEW")
        self.assertIn("NOT_ALL_SOURCE_IMAGES_REGISTERED", result["decision"]["original_quality_warnings"])
        self.assertIn("MULTIPLE_RECONSTRUCTION_COMPONENTS", result["decision"]["original_quality_warnings"])
        self.assertIn("REPROJECTION_ERROR_ZERO_OR_UNINFORMATIVE", result["decision"]["original_quality_warnings"])
        self.assertIn("SPIRULA_REPORTED_PARTIAL_OUTPUT", result["decision"]["original_quality_warnings"])
        scope = training_component_scope(self.root, self.config, self.state)
        self.assertEqual(scope["component_id"], "0")
        self.assertEqual(scope["registered_image_count"], 8)
        self.assertIsNone(self.state.db.execute(
            "SELECT 1 FROM review_gates WHERE gate_id='sfm-component-acceptance:1'").fetchone())
        self.config["integrations"]["model"]["sfm_component_id"] = "1"
        with self.assertRaisesRegex(BackendError, "no current experimental-training acceptance"):
            training_component_scope(self.root, self.config, self.state)
        self.config["integrations"]["model"].pop("sfm_component_id")
        self.assertEqual(self.state.stage("sfm")["status"], "NEEDS_REVIEW")

    def test_artifact_change_makes_acceptance_stale(self):
        fixture = self.make_sfm()
        self.accept()
        _write_component(fixture["components"][0], fixture["names"][:8], point_error=0.1)
        status = component_acceptance_status(self.root, self.config, self.state)
        self.assertEqual(status["components"][0]["status"], "STALE")
        with self.assertRaisesRegex(BackendError, "current component-scoped"):
            training_component_scope(self.root, self.config, self.state)

    def test_malformed_or_unauthorized_acceptance_record_does_not_open_training_gate(self):
        self.make_sfm()
        self.accept("0")
        row = self.state.db.execute(
            "SELECT decision_json FROM review_gates WHERE gate_id='sfm-component-acceptance:0'").fetchone()
        decision = json.loads(row["decision_json"])
        decision["permitted_downstream_purpose"] = "FINAL_ARCHIVE_ACCEPTANCE"
        self.state.db.execute(
            "UPDATE review_gates SET decision_json=? WHERE gate_id='sfm-component-acceptance:0'",
            (json.dumps(decision),))
        self.state.db.commit()
        status = component_acceptance_status(self.root, self.config, self.state)
        self.assertEqual(status["components"][0]["status"], "INVALID_RECORD")
        with self.assertRaisesRegex(BackendError, "current component-scoped"):
            training_component_scope(self.root, self.config, self.state)

    def test_changed_registered_membership_requires_a_new_decision(self):
        fixture = self.make_sfm()
        self.accept()
        changed = fixture["names"][:7] + [fixture["names"][10]]
        _write_component(fixture["components"][0], changed)
        status = component_acceptance_status(self.root, self.config, self.state)
        self.assertEqual(status["components"][0]["status"], "STALE")
        self.assertNotEqual(status["components"][0]["registered_image_membership_sha256"],
                            status["components"][1]["registered_image_membership_sha256"])

    def test_unreviewed_rejected_and_deferred_results_cannot_plan_training(self):
        self.make_sfm()
        with self.assertRaisesRegex(BackendError, "component-scoped experimental-training acceptance"):
            training_component_scope(self.root, self.config, self.state)
        rejected = review_sfm_component(self.root, self.config, self.state, component_id="0", decision="reject",
                                        reviewer="TEST_ONLY reviewer", rationale="Reject fixture")
        self.assertEqual(rejected["status"], "REJECTED")
        with self.assertRaises(BackendError):
            training_component_scope(self.root, self.config, self.state)
        deferred = review_sfm_component(self.root, self.config, self.state, component_id="0", decision="defer",
                                        reviewer="TEST_ONLY reviewer", rationale="Defer fixture")
        self.assertEqual(deferred["status"], "NEEDS_REVIEW")
        self.assertEqual(self.state.stage("sfm")["status"], "NEEDS_REVIEW")

    def test_component_zero_plan_and_dataset_use_300_registered_images_from_332_sources(self):
        fixture = self.make_sfm(source_count=332, component_sizes=(300, 10))
        self.accept("0")
        scope = training_component_scope(self.root, self.config, self.state)
        self.assertEqual(scope["source_image_count"], 332)
        self.assertEqual(scope["registered_image_count"], 300)
        self.assertEqual(sorted(row["registered_image_count"] for row in fixture["validation"]["components"]), [10, 300])

        attempt = self.root / "output" / "test-attempt"
        attempt.mkdir(parents=True)
        dataset = prepare_training_dataset(attempt, self.photos, fixture["components"][0],
                                           registered_image_names=scope["registered_image_names"])
        materialized = sorted(path.relative_to(dataset / "images").as_posix()
                              for path in (dataset / "images").rglob("*") if path.is_file())
        self.assertEqual(materialized, sorted(scope["registered_image_names"]))
        self.assertEqual(len(materialized), 300)
        self.assertFalse((dataset / "images").is_symlink())
        self.assertTrue(all((dataset / "images" / name).is_symlink() for name in materialized))
        self.assertFalse(set(fixture["names"][300:310]) & set(materialized))
        self.assertFalse(set(fixture["names"][310:]) & set(materialized))
        self.assertTrue((dataset / "sparse" / "0").is_symlink())
        self.assertTrue((dataset / "sparse" / "0" / "images.bin").is_file())

    def test_training_plan_reports_scoped_readiness_and_exact_input_count(self):
        self.make_sfm(source_count=12, component_sizes=(8, 2))
        self.accept("0")
        training = next(row for row in plan(self.root, self.config, self.state) if row["id"] == "training")
        self.assertEqual(training["readiness"], "READY_EXPERIMENTAL_TRAINING_WITH_SCOPED_ACCEPTANCE")
        execution_plan = expensive_execution_plan(self.root, self.config, self.state, "training", backend_info=BACKEND)
        self.assertEqual(execution_plan["source_image_count"], 8)
        self.assertEqual(execution_plan["source_directory_image_count"], 12)
        self.assertEqual(execution_plan["sfm_component_scope"]["component_id"], "0")
        self.assertEqual(execution_plan["sfm_component_scope"]["registered_image_count"], 8)
        self.assertTrue(execution_plan["plan_id"])
        self.assertEqual(self.state.execution_authorizations(), [])

    def test_training_execution_still_waits_for_a_separate_exact_plan_grant(self):
        self.make_sfm()
        self.accept("0")
        with patch("exhibition_archive.workflow.STAGES", [SFM_STAGE, NAVIGATION_STAGE, TRAINING_STAGE]), \
             patch("exhibition_archive.backends.discover_environment", return_value=BACKEND):
            result = run_workflow(self.root, self.config, self.state)
        self.assertEqual(result["status"], "AUTHORIZATION_REQUIRED")
        self.assertEqual(result["stages"]["sfm"], "RETAINED_NEEDS_REVIEW_WITH_TRAINING_SCOPE")
        self.assertEqual(result["stages"]["navigation"], "SKIPPED_OUTSIDE_SFM_ACCEPTANCE_SCOPE")
        self.assertEqual(result["blocked"][0]["stage"], "training")
        self.assertEqual(self.state.stage("sfm")["status"], "NEEDS_REVIEW")
        self.assertEqual(self.state.db.execute("SELECT COUNT(*) FROM stage_executions WHERE stage_id='training'").fetchone()[0], 0)
        self.assertEqual(self.state.execution_authorizations(), [])

    def test_changed_stage_report_artifact_invalidates_a_prior_acceptance(self):
        self.make_sfm()
        self.accept()
        report = self.root / "output" / "work" / "sfm-summary.json"
        write_json(report, {"quality_gate": "SFM_VALID", "warnings": []})
        with self.assertRaises(BackendError):
            component_acceptance_status(self.root, self.config, self.state)

    def test_acceptance_requires_explicit_limitations(self):
        self.make_sfm(source_count=8, component_sizes=(8,))
        with self.assertRaisesRegex(ValueError, "explicit limitation"):
            review_sfm_component(self.root, self.config, self.state, component_id="0",
                                 decision="accept-experimental-training", reviewer="TEST_ONLY reviewer",
                                 rationale="Synthetic acceptance", limitations=[])


if __name__ == "__main__":
    unittest.main()
