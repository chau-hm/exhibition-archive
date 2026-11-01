from __future__ import annotations

import shutil
import hashlib
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from exhibition_archive.adapters import read_json, write_json
from exhibition_archive.backends import BackendError, build_training_command, check_resources, execute_process
from exhibition_archive.storage import storage_report
from exhibition_archive.workflow import WorkflowError, training_resume_identity


TRAINING_CAPABILITIES = {
    "training": {
        "presets": ["3dgs"],
        "arguments": ["--train-resolution-divisor", "--device", "--steps-per-save",
                      "--save-only-latest-checkpoint", "--save-full-checkpoint", "--resume",
                      "--quality", "--view-sampling", "--cache-images", "--load-masks"],
        "checkpoint": {
            "latest_only_retention": "VERIFIED_BY_HELP",
            "latest_only_prunes_older": "VERIFIED_BY_HELP",
            "full_state": "VERIFIED_BY_HELP",
            "resume_cli_flag": "VERIFIED_BY_HELP",
        },
    }
}


class _StateStub:
    def update_stage_execution(self, *_args, **_kwargs):
        return None

    def stage(self, _stage_id):
        return {"input_fingerprint": "fixture-fingerprint"}

    def set_stage(self, *_args, **_kwargs):
        return None


class TrainingCheckpointTests(unittest.TestCase):
    def test_training_command_records_storage_efficient_resumable_policy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            resume_dir = root / "previous-attempt" / "outputs" / "baseline-run"
            cfg = {"preset": "3dgs", "iterations": 30000, "resolution_divisor": 1,
                   "device": "auto", "quality": "high", "steps_per_save": 5000,
                   "save_only_latest_checkpoint": True, "save_full_checkpoint": True,
                   "view_sampling": "uniform", "cache_images": "disk", "load_masks": False}
            command = build_training_command(Path("/opt/spirula"), root / "dataset", root / "new-attempt",
                                             cfg, TRAINING_CAPABILITIES, "ignored-run-name", resume_dir)
            args = list(zip(command, command[1:] + [""]))
            self.assertIn(("--steps-per-save", "5000"), args)
            self.assertIn(("--save-only-latest-checkpoint", "1"), args)
            self.assertNotIn(("--save-only-latest-checkpoint", "0"), args)
            self.assertIn(("--save-full-checkpoint", "1"), args)
            self.assertIn(("--num-iterations", "30000"), args)
            self.assertIn(("--train-resolution-divisor", "1"), args)
            self.assertIn(("--quality", "high"), args)
            self.assertIn(("--view-sampling", "uniform"), args)
            self.assertIn(("--cache-images", "disk"), args)
            self.assertIn(("--load-masks", "0"), args)
            self.assertIn("--resume", command)
            self.assertIn(str(resume_dir.parent.resolve()), command)
            self.assertIn(resume_dir.name, command)

    def test_latest_only_requires_full_checkpoints_and_backend_flags(self):
        cfg = {"preset": "3dgs", "iterations": 100, "resolution_divisor": 1,
               "save_only_latest_checkpoint": True, "save_full_checkpoint": False}
        with self.assertRaisesRegex(BackendError, "requires full checkpoints"):
            build_training_command(Path("spirula"), Path("dataset"), Path("attempt"),
                                   cfg, TRAINING_CAPABILITIES, "run")
        unsupported = {"training": {"presets": ["3dgs"], "arguments": ["--train-resolution-divisor"]}}
        with self.assertRaisesRegex(BackendError, "latest-only checkpoint pruning semantics"):
            build_training_command(Path("spirula"), Path("dataset"), Path("attempt"),
                                   {"preset": "3dgs", "iterations": 100, "resolution_divisor": 1},
                                   unsupported, "run")

    def test_resume_identity_binds_checkpoint_hashes_inside_project(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "output"
            run_dir = output / "backend-runs" / "training" / "attempt-1" / "outputs" / "baseline"
            run_dir.mkdir(parents=True)
            checkpoint = run_dir / "checkpoint-5000.pth"
            checkpoint.write_bytes(b"full-checkpoint-v1")
            config = {"project_id": "resume-fixture", "output_directory": "output"}
            resolved, identity = training_resume_identity(root, config, {"resume_from": str(run_dir)})
            self.assertEqual(resolved, run_dir.resolve())
            self.assertEqual(identity["checkpoint_candidates"][0]["sha256"],
                             hashlib.sha256(b"full-checkpoint-v1").hexdigest())
            checkpoint.write_bytes(b"full-checkpoint-v2")
            _, changed_identity = training_resume_identity(root, config, {"resume_from": str(run_dir)})
            self.assertNotEqual(identity["newest_by_mtime"]["sha256"], changed_identity["newest_by_mtime"]["sha256"])
            (run_dir / "candidate.ply").write_bytes(b"reviewed-model-candidate")
            with self.assertRaisesRegex(WorkflowError, "containing a model PLY"):
                training_resume_identity(root, config, {"resume_from": str(run_dir)})
            (run_dir / "candidate.ply").unlink()
            (run_dir / "checkpoint-link.pth").symlink_to(checkpoint)
            with self.assertRaisesRegex(WorkflowError, "may not contain symlinks"):
                training_resume_identity(root, config, {"resume_from": str(run_dir)})
            empty_run = output / "backend-runs" / "training" / "attempt-2" / "outputs" / "empty"
            empty_run.mkdir(parents=True)
            with self.assertRaisesRegex(WorkflowError, "no Spirula checkpoint candidates"):
                training_resume_identity(root, config, {"resume_from": str(empty_run)})

    def test_training_launch_requires_disk_floor_plus_replacement_reserve(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            usage = shutil._ntuple_diskusage(total=1000, used=800, free=200)
            policy = {"minimum_free_disk_bytes_training": 100,
                      "checkpoint_replacement_reserve_bytes": 101,
                      "checkpoint_replacement_reserve_basis": "OPERATOR_PROVISIONED"}
            with patch("exhibition_archive.backends.shutil.disk_usage", return_value=usage):
                with self.assertRaises(BackendError) as caught:
                    check_resources(path, policy, "training")
            self.assertEqual(caught.exception.failure_kind, "RESOURCE_LIMIT")
            self.assertEqual(caught.exception.details["minimum_required_bytes"], 201)

            usage = shutil._ntuple_diskusage(total=1000, used=700, free=300)
            with patch("exhibition_archive.backends.shutil.disk_usage", return_value=usage):
                checked = check_resources(path, policy, "training")
            self.assertEqual(checked["minimum_required_at_launch_bytes"], 201)
            self.assertEqual(checked["minimum_required_bytes"], 100)
            self.assertEqual(checked["checkpoint_replacement_reserve_basis"], "OPERATOR_PROVISIONED")
            self.assertEqual(checked["checkpoint_replacement_overlap_assumption"],
                             "OLD_AND_NEW_FULL_CHECKPOINTS_MAY_COEXIST")

    def test_runtime_low_disk_safe_stops_without_deleting_recovery_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            attempt = Path(temp) / "attempt"
            output = attempt / "outputs"
            output.mkdir(parents=True)
            checkpoint = output / "checkpoint-5000.pth"
            checkpoint.write_bytes(b"still-valid-recovery-state")
            write_json(attempt / "command.json", {"stage": "training"})
            log_path = attempt / "spirula.log"
            usage = shutil._ntuple_diskusage(total=1000, used=950, free=50)
            with patch("exhibition_archive.backends.shutil.disk_usage", return_value=usage):
                with self.assertRaises(BackendError) as caught:
                    execute_process(_StateStub(), 1, "training",
                                    [sys.executable, "-c", "import time; time.sleep(10)"],
                                    attempt, log_path, timeout_seconds=20, poll_interval=0.1,
                                    checkpoint_root=output, minimum_free_disk_bytes=100)
            self.assertEqual(caught.exception.failure_kind, "RESOURCE_LIMIT")
            self.assertFalse(caught.exception.details["checkpoint_cleanup_performed"])
            self.assertFalse(caught.exception.details["checkpoint_saved_during_stop"])
            self.assertTrue(caught.exception.details["checkpoint_candidate_present_after_stop"])
            self.assertEqual(caught.exception.details["resumability_validation"], "NOT_PERFORMED")
            self.assertTrue(checkpoint.is_file())
            record = read_json(attempt / "command.json")
            self.assertEqual(record["error_classification"], "RESOURCE_LIMIT")
            self.assertFalse(record["terminal_details"]["checkpoint_saved_during_stop"])

    def test_runtime_disk_stop_reports_checkpoint_written_during_sigterm_shutdown(self):
        with tempfile.TemporaryDirectory() as temp:
            attempt = Path(temp) / "attempt"
            output = attempt / "outputs"
            output.mkdir(parents=True)
            ready = attempt / "backend-ready"
            checkpoint = output / "run" / "checkpoint-5000.pth"
            write_json(attempt / "command.json", {"stage": "training"})
            log_path = attempt / "spirula.log"
            usage = shutil._ntuple_diskusage(total=1000, used=950, free=50)
            script = (
                "import pathlib, signal, sys, time\n"
                "ready, checkpoint = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])\n"
                "def save_then_exit(_signal, _frame):\n"
                "    checkpoint.parent.mkdir(parents=True, exist_ok=True)\n"
                "    checkpoint.write_bytes(b'checkpoint-saved-on-stop')\n"
                "    raise SystemExit(0)\n"
                "signal.signal(signal.SIGTERM, save_then_exit)\n"
                "ready.write_text('ready')\n"
                "time.sleep(10)\n"
            )

            def low_disk_after_handler_ready(_path):
                deadline = time.monotonic() + 3
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                return usage

            with patch("exhibition_archive.backends.shutil.disk_usage", side_effect=low_disk_after_handler_ready):
                with self.assertRaises(BackendError) as caught:
                    execute_process(_StateStub(), 2, "training",
                                    [sys.executable, "-c", script, str(ready), str(checkpoint)],
                                    attempt, log_path, timeout_seconds=20, poll_interval=0.1,
                                    checkpoint_root=output, minimum_free_disk_bytes=100)

            details = caught.exception.details
            self.assertEqual(details["stop_signal"], "SIGTERM_SENT")
            self.assertTrue(details["checkpoint_saved_during_stop"])
            self.assertTrue(details["checkpoint_candidate_present_after_stop"])
            self.assertEqual(details["resumable_checkpoint_saved"], "UNKNOWN_NOT_VALIDATED")
            self.assertEqual(details["resumability_validation"], "NOT_PERFORMED")
            self.assertEqual(details["candidate_validity"], "NOT_BACKEND_VALIDATED")
            self.assertEqual(checkpoint.read_bytes(), b"checkpoint-saved-on-stop")
            record = read_json(attempt / "command.json")
            self.assertTrue(record["terminal_details"]["checkpoint_saved_during_stop"])
            self.assertFalse(record["terminal_details"]["checkpoint_cleanup_performed"])


class StorageReportTests(unittest.TestCase):
    def test_storage_report_separates_categories_and_never_deletes_candidates(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project?audit"
            source = root / "input" / "originals"
            source.mkdir(parents=True)
            (source / "original.heic").write_bytes(b"source-payload")
            derived = root / "derived" / "spirula-jpeg"
            derived.mkdir(parents=True)
            (derived / "converted.jpg").write_bytes(b"derived-payload")
            training_attempt = root / "output" / "backend-runs" / "training" / "attempt-1"
            dataset_images = training_attempt / "dataset" / "images"
            dataset_images.parent.mkdir(parents=True)
            dataset_images.symlink_to(source, target_is_directory=True)
            outputs = training_attempt / "outputs" / "run-1"
            outputs.mkdir(parents=True)
            (outputs / "checkpoint-5000.pth").write_bytes(b"checkpoint")
            external_attempt = Path(temp) / "outside-attempt"
            external_outputs = external_attempt / "outputs" / "checkpoint-run"
            external_outputs.mkdir(parents=True)
            external_checkpoint = external_outputs / "checkpoint-9000.pth"
            external_checkpoint.write_bytes(b"outside-checkpoint")
            training_root = root / "output" / "backend-runs" / "training"
            (training_root / "attempt-escape").symlink_to(external_attempt, target_is_directory=True)
            smoke_attempt = root / "output" / "backend-runs" / "smoke-training" / "attempt-smoke"
            smoke_images = smoke_attempt / "dataset" / "images"
            smoke_images.mkdir(parents=True)
            (smoke_images / "copied-image.jpg").write_bytes(b"diagnostic-copy")
            smoke_outputs = smoke_attempt / "outputs" / "run-smoke"
            smoke_outputs.mkdir(parents=True)
            smoke_checkpoint = smoke_outputs / "checkpoint-100.pth"
            smoke_checkpoint.write_bytes(b"diagnostic-checkpoint")
            cache = root / "output" / "caches"
            cache.mkdir(parents=True)
            (cache / "temporary.png").write_bytes(b"cache")
            archive = root / "output" / "archive"
            archive.mkdir(parents=True)
            (archive / "manifest.json").write_bytes(b"archive")
            accepted_model = archive / "accepted-model.ply"
            accepted_model.write_bytes(b"accepted-model")
            database = sqlite3.connect(root / "workflow.sqlite3")
            database.execute("CREATE TABLE artifacts(path TEXT, status TEXT)")
            database.execute("CREATE TABLE review_gates(gate_id TEXT, status TEXT, decision_json TEXT)")
            database.execute("INSERT INTO artifacts(path, status) VALUES(?, ?)",
                             (str(accepted_model), "VALIDATED"))
            database.execute("INSERT INTO review_gates(gate_id, status, decision_json) VALUES(?, ?, ?)",
                             ("model-review", "APPROVED", json.dumps({"candidate_path": str(accepted_model)})))
            database.commit()
            database.close()
            config = {"project_id": "storage-fixture", "source": {"photo_directory": str(source)},
                      "output_directory": "output"}

            report = storage_report(root, config)
            categories = {item["id"]: item for item in report["categories"]}
            self.assertEqual(categories["source_images"]["size_bytes"], len(b"source-payload"))
            self.assertEqual(categories["prepared_derivatives"]["size_bytes"], len(b"derived-payload"))
            self.assertEqual(categories["checkpoints"]["size_bytes"],
                             len(b"checkpoint") + len(b"diagnostic-checkpoint"))
            self.assertEqual(categories["training_datasets"]["file_count"], 1)
            smoke_candidate = next(item for item in report["checkpoint_candidates"]
                                   if item["path"] == str(smoke_checkpoint.resolve()))
            self.assertEqual(smoke_candidate["stage"], "smoke-training")
            self.assertEqual(smoke_candidate["attempt"], str(smoke_attempt.resolve()))
            self.assertEqual(categories["validated_artifacts"]["file_count"], 1)
            self.assertEqual(categories["review_referenced_artifacts"]["file_count"], 1)
            self.assertEqual(categories["training_datasets"]["size_bytes"], len(b"diagnostic-copy"))
            self.assertFalse(categories["checkpoints"]["additive"])
            self.assertNotIn(str(external_checkpoint.resolve()),
                             {item["path"] for item in report["checkpoint_candidates"]})
            self.assertTrue(report["cleanup_candidates"])
            self.assertTrue(all(item["safe_to_delete"] is False for item in report["cleanup_candidates"]))
            self.assertEqual((source / "original.heic").read_bytes(), b"source-payload")
            self.assertEqual(report["archive_optimization_target"]["ratio_to_original_photo_payload"], 10)


if __name__ == "__main__":
    unittest.main()
