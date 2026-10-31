#!/usr/bin/env python3
"""Level B integration coverage for the fresh Spirula orchestration boundary.

The fake executable emits a tiny, internally consistent COLMAP binary model
and a valid ASCII 3DGS PLY. It never invokes a reconstruction or GPU backend.
"""
from __future__ import annotations

import json
import hashlib
import os
import signal
import sqlite3
import struct
import subprocess
import sys
import time
import argparse
from datetime import datetime, timezone
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
PRODUCT = REPO / "exhibition-archive"
BIN = PRODUCT / "bin" / "exhibition-archive"
PHASE2EZ = REPO / "analysis" / "phase2ez-training-smoke-test"
DEST_ROOT = PHASE2EZ / "reliability-tests" / "runs"
sys.path.insert(0, str(PRODUCT / "src"))
from exhibition_archive.smoke import _gpu_initialization_evidence


FAKE_SPIRULA = r'''#!/usr/bin/env python3
import os, pathlib, struct, sys, time
args = sys.argv[1:]
if args == ["--version"]:
    print("Fake Spirula 0.0-test")
elif args == ["--help"]:
    print("Commands:\n  sfm     run SfM\n  train   train a model")
elif args[:2] == ["sfm", "--help"]:
    print("sfm help")
elif args[:3] == ["sfm", "auto", "--help"]:
    print("  --max-image-size <int>\n  --quality <name>\n  --data-type <name>\n  --reuse-features <mode>\n  --reuse-matches <mode>\n  --no-resume")
elif args == ["train", "--help"]:
    print("  --data <path>\n  --data-format <name>\n  --image-dir <path>\n  --colmap-recon-dir <path>\n  --output-dir-prefix <path>\n  --output-dir-name <name>\n  --num-iterations <int>\n  --train-resolution-divisor <int>\n  --device <device>\n  --disable-viewer <0|1>\n  --keep-viewer-alive <0|1>")
elif args == ["train", "--help-all"]:
    print("  --steps-per-save <int>\n  --save-only-latest-checkpoint <0|1>\n  --save-full-checkpoint <0|1>\n  --resume\n  --init-ply <path>")
elif args[:2] == ["sfm", "auto"]:
    if os.environ.get("FAKE_SFM_FAIL"):
        print("controlled fake SfM failure", file=sys.stderr)
        sys.exit(17)
    if os.environ.get("FAKE_SFM_SLEEP"):
        time.sleep(float(os.environ["FAKE_SFM_SLEEP"]))
    image_dir = pathlib.Path(args[2])
    output = pathlib.Path(args[args.index("-o") + 1])
    all_images = sorted(p.name for p in image_dir.iterdir() if p.suffix.casefold() == ".jpg")
    partial_sfm = os.environ.get("FAKE_SFM_PARTIAL")
    images = all_images[:6] if partial_sfm else all_images
    sparse = output / "sparse" / "0"
    sparse.mkdir(parents=True)
    (output / "features").mkdir()
    (output / "matches.bin").write_bytes(b"fake matches")
    with (sparse / "cameras.bin").open("wb") as f:
        f.write(struct.pack("<QiiQQ3d", 1, 1, 0, 640, 480, 500.0, 320.0, 240.0))
    tracks = [[(i + 1, j) for i in range(len(images))] for j in range(12)]
    with (sparse / "images.bin").open("wb") as f:
        f.write(struct.pack("<Q", len(images)))
        for i, name in enumerate(images):
            f.write(struct.pack("<I7dI", i + 1, 1.0, 0.0, 0.0, 0.0, -i * 0.1, 0.0, 0.0, 1))
            f.write(name.encode() + b"\0")
            f.write(struct.pack("<Q", len(tracks)))
            for j in range(len(tracks)):
                f.write(struct.pack("<2dQ", 100.0 + j, 120.0 + j, j + 1))
    with (sparse / "points3D.bin").open("wb") as f:
        f.write(struct.pack("<Q", len(tracks)))
        for j, track in enumerate(tracks):
            reprojection_error = 2.5 if partial_sfm else 0.25
            f.write(struct.pack("<Q3d3BdQ", j + 1, j * 0.1, j * 0.2, 1.0, 180, 190, 200, reprojection_error, len(track)))
            for image_id, point2d in track:
                f.write(struct.pack("<ii", image_id, point2d))
    if partial_sfm:
        print("controlled fake partial SfM output", file=sys.stderr)
        sys.exit(3)
elif args and args[0] == "train":
    if os.environ.get("FAKE_TRAIN_FAIL"):
        print("controlled fake training failure", file=sys.stderr)
        sys.exit(19)
    if os.environ.get("FAKE_TRAIN_SLEEP"):
        time.sleep(float(os.environ["FAKE_TRAIN_SLEEP"]))
    if os.environ.get("FAKE_TRAIN_NO_MODEL"):
        sys.exit(0)
    prefix = pathlib.Path(args[args.index("--output-dir-prefix") + 1])
    name = args[args.index("--output-dir-name") + 1]
    ply = prefix / name / "point_cloud" / "iteration_12" / "point_cloud.ply"
    ply.parent.mkdir(parents=True)
    properties = [
      ("float", "x"), ("float", "y"), ("float", "z"), ("float", "nx"), ("float", "ny"), ("float", "nz"),
      ("float", "f_dc_0"), ("float", "f_dc_1"), ("float", "f_dc_2"), ("float", "opacity"),
      ("float", "scale_0"), ("float", "scale_1"), ("float", "scale_2"),
      ("float", "rot_0"), ("float", "rot_1"), ("float", "rot_2"), ("float", "rot_3")]
    header = ["ply", "format ascii 1.0", "element vertex 2"] + [f"property {t} {n}" for t,n in properties] + ["end_header"]
    row = "0 0 0 0 0 0 0 0 0 0 0 0 0 1 0 0 0"
    if os.environ.get("FAKE_TRAIN_NONFINITE_SCALE"):
        values = row.split()
        values[10] = "nan"
        row = " ".join(values)
    ply.write_text("\n".join(header + [row, row]) + "\n", encoding="ascii")
    if os.environ.get("FAKE_TRAIN_CORRUPT_PLY"):
        ply.write_text("\n".join(header) + "\n0 0 0", encoding="ascii")
else:
    print("unsupported fake invocation", args, file=sys.stderr)
    sys.exit(2)
'''


def authorize_current_plan(workspace: Path, stage: str, plan_id: str | None = None) -> str | None:
    if plan_id is None:
        plan_text = subprocess.run([str(BIN), "plan", str(workspace), "--full-hash"], stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True).stdout
        prefix = "  fresh backend authorization plan: "
        for line in plan_text.splitlines():
            if line.startswith(prefix):
                try: candidate = json.loads(line[len(prefix):])
                except json.JSONDecodeError: continue
                if candidate.get("stage") == stage:
                    plan_id = candidate.get("plan_id"); break
    if not plan_id:
        return None
    created = subprocess.run([str(BIN), "authorize", str(workspace), "--stage", stage, "--plan-id", plan_id,
                              "--reviewer", "TEST_ONLY fake backend harness", "--rationale",
                              "Synthetic fake-backend regression only; no real exhibition or GPU job."],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if created.returncode:
        return None
    try: return json.loads(created.stdout)["authorization_id"]
    except (json.JSONDecodeError, KeyError): return None


def run(*args: object, expect: int = 0) -> str:
    argv = list(args)
    proc = subprocess.run([str(BIN), *map(str, argv)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    attempts = 0
    while proc.returncode == 3 and argv and argv[0] in {"run", "resume"} and attempts < 2:
        try:
            result = json.loads(proc.stdout)
            blocked = next(item for item in result.get("blocked", []) if item.get("authorization_required") and item.get("plan_id"))
        except (json.JSONDecodeError, StopIteration):
            break
        workspace = Path(str(argv[1])).expanduser().resolve()
        stage, plan_id = blocked["stage"], blocked["plan_id"]
        # Authorize the exact plan returned by the blocked Engine invocation.
        # Do not pre-authorize before inventory/preflight: those safe stages
        # can change the current full-hash identity before the backend gate.
        grant_id = authorize_current_plan(workspace, stage, plan_id)
        if not grant_id:
            raise AssertionError(f"test-only {stage} authorization could not be created for blocked plan {plan_id}")
        argv = ["resume", str(workspace), f"--authorization-id={grant_id}"]
        proc = subprocess.run([str(BIN), *map(str, argv)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        attempts += 1
    if proc.returncode != expect:
        raise AssertionError(f"expected exit {expect}, got {proc.returncode}: {' '.join(map(str,args))}\n{proc.stdout}")
    return proc.stdout


def _wait_for_backend_pid(workspace: Path, stage: str, process: subprocess.Popen, timeout: float = 12.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"workflow exited before launching {stage}; code={process.returncode}")
        with sqlite3.connect(workspace / "workflow.sqlite3") as db:
            row = db.execute("SELECT pid FROM stage_executions WHERE stage_id=? ORDER BY execution_id DESC LIMIT 1", (stage,)).fetchone()
        if row and row[0]:
            return int(row[0])
        time.sleep(0.05)
    raise TimeoutError(f"workflow did not launch {stage} within {timeout} seconds")


def _start_authorized_backend(workspace: Path, stage: str, stdout_path: Path) -> tuple[subprocess.Popen, int, object]:
    """Start a fake backend, refreshing a test grant if discovery changes its first plan."""
    next_grant = authorize_current_plan(workspace, stage)
    for _ in range(3):
        if not next_grant:
            raise AssertionError(f"test-only {stage} authorization could not be created")
        stdout_file = stdout_path.open("w", encoding="utf-8")
        process = subprocess.Popen([str(BIN), "run", str(workspace), "--authorization-id", next_grant],
                                   stdout=stdout_file, stderr=subprocess.STDOUT, text=True)
        try:
            pid = _wait_for_backend_pid(workspace, stage, process)
            return process, pid, stdout_file
        except AssertionError:
            stdout_file.close()
            if process.returncode != 3:
                raise
            try:
                result = json.loads(stdout_path.read_text(encoding="utf-8"))
                blocked = next(item for item in result.get("blocked", [])
                               if item.get("authorization_required") and item.get("plan_id"))
            except (OSError, json.JSONDecodeError, StopIteration):
                raise AssertionError(f"workflow exited before launching {stage}: {stdout_path.read_text(encoding='utf-8')}")
            next_grant = authorize_current_plan(workspace, stage, blocked["plan_id"])
    raise AssertionError(f"workflow repeatedly changed the approved {stage} plan before launch")


def _project_fixture(root: Path, *, fail_sfm: bool = False, sleep_sfm: float | None = None,
                     partial_sfm: bool = False,
                     fail_training: bool = False, sleep_training: float | None = None,
                     no_model: bool = False, corrupt_ply: bool = False,
                     nonfinite_scale: bool = False,
                     min_disk_bytes: int = 0, sfm_extra_args: list[str] | None = None,
                     photo_count: int = 8, derived_inputs: bool = False,
                     run_type: str = "smoke_test", timeout_sfm_seconds: int | None = None,
                     timeout_training_seconds: int | None = None) -> tuple[Path, Path]:
    photos = root / "source" / "camera-roll"
    photos.mkdir(parents=True)
    for i in range(photo_count):
        # The fake backend does not decode pixels; inventory only checks names,
        # sizes and hashes. These bytes are test controls, not photographs.
        suffix = ".heic" if derived_inputs else ".jpg"
        (photos / f"other-gallery-view-{i:02}{suffix}").write_bytes(f"test-image-{i}".encode())
    derived_dir = root / "workspace" / "derived" / "spatial-camera-exports"
    derivative_manifest = root / "workspace" / "derived" / "derivative-map.json"
    mappings = []
    if derived_inputs:
        derived_dir.mkdir(parents=True)
        for i in range(photo_count):
            source_name = f"other-gallery-view-{i:02}.heic"
            derived_name = f"west-wing-angle-{i:02}.jpg"
            source = photos / source_name
            derived = derived_dir / derived_name
            derived.write_bytes(f"derived-test-image-{i}".encode())
            checksum = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
            mappings.append({"image_id": derived_name, "source_relative_path": source_name, "source_sha256": checksum(source),
                             "derived_relative_path": derived_name, "derived_sha256": checksum(derived),
                             "metadata_sidecar_relative_path": None, "metadata_sidecar_sha256": None})
        derivative_manifest.write_text(json.dumps({"schema_version":"exhibition-archive.image-derivative-map.v1", "conversion":{"tool":"fake-test-converter", "version":"test"}, "source_images_modified":False, "image_mappings":mappings}, indent=2) + "\n", encoding="utf-8")
    fake = root / "fake-spirula"
    fake.write_text(FAKE_SPIRULA, encoding="utf-8")
    fake.chmod(0o755)
    cfg = {
      "schema_version": "exhibition-archive.project.v1", "project_id": "indigo-room-fixture",
      "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
      "source": {"photo_directory": str(photos.resolve()), "read_only": True,
                 "derived_image_directories": ["derived/spatial-camera-exports"] if derived_inputs else []},
      "output_directory": "deliverable", "viewer": {"title": "Indigo Room", "theme": "dark"},
      "publication": {"public_site": True, "include_source_photographs": False, "include_private_review_notes": False},
      "archive_policy": {"bundle_source_photographs": False}, "retry_policy": {"max_failed_attempts": 1},
      "resource_policy": {"max_training_iterations": 12, "minimum_free_disk_bytes": 0, "sfm_timeout_seconds": 30,
                          "training_timeout_seconds": 30, "poll_interval_seconds": 0.1},
      "integrations": {
        "sfm": {"mode": "spirula_fresh", "executable": str(fake), "image_directory": "derived/spatial-camera-exports" if derived_inputs else str(photos.resolve()),
                "derived_image_manifest": "derived/derivative-map.json" if derived_inputs else None,
                "quality": "low", "data_type": "individual", "max_image_size": 640,
                "quality_gate": {"minimum_registered_ratio": 1.0, "minimum_registered_images": 8,
                                 "maximum_mean_point_reprojection_error_px": 2.0},
                "additional_args": sfm_extra_args or [],
                "environment_overrides": ({"FAKE_SFM_FAIL": "1"} if fail_sfm else {}) | ({"FAKE_SFM_PARTIAL": "1"} if partial_sfm else {}) | ({"FAKE_SFM_SLEEP": str(sleep_sfm)} if sleep_sfm is not None else {})},
        "model": {"mode": "spirula_fresh", "executable": str(fake), "run_type": run_type,
                  "preset": "3dgs", "iterations": 12, "resolution_divisor": 8, "steps_per_save": 12,
                  "image_directory": "derived/spatial-camera-exports" if derived_inputs else str(photos.resolve()),
                  "device": "auto",
                  "environment_overrides": ({"FAKE_TRAIN_FAIL": "1"} if fail_training else {}) | ({"FAKE_TRAIN_SLEEP": str(sleep_training)} if sleep_training is not None else {}) | ({"FAKE_TRAIN_NO_MODEL": "1"} if no_model else {}) | ({"FAKE_TRAIN_CORRUPT_PLY": "1"} if corrupt_ply else {}) | ({"FAKE_TRAIN_NONFINITE_SCALE": "1"} if nonfinite_scale else {})}
      }
    }
    if timeout_sfm_seconds is not None:
        cfg["integrations"]["sfm"]["timeout_seconds"] = timeout_sfm_seconds
    if timeout_training_seconds is not None:
        cfg["integrations"]["model"]["timeout_seconds"] = timeout_training_seconds
    cfg["resource_policy"]["minimum_free_disk_bytes"] = min_disk_bytes
    config = root / "project-config.json"
    config.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    workspace = root / "workspace"
    run("init", "--config", config, "--workspace", workspace)
    return workspace, fake


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest-root", type=Path, default=DEST_ROOT,
                        help="evidence destination; defaults to the historical reliability-test directory")
    args = parser.parse_args()
    dest_root = args.dest_root.resolve()
    dest_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%SZ")
    dest = dest_root / stamp
    dest.mkdir()
    selected_gpu_log = dest / "selected-gpu-device.log"
    selected_gpu_log.write_text("Devices:\n  * [0] Apple M4 Pro (integrated, 65536 MB)\n", encoding="utf-8")
    selected_gpu = _gpu_initialization_evidence(selected_gpu_log, "Apple M4 Pro")
    assert selected_gpu["initialized"] and selected_gpu["evidence_lines"] == ["* [0] Apple M4 Pro (integrated, 65536 MB)"], selected_gpu
    listed_only_gpu_log = dest / "listed-only-gpu-device.log"
    listed_only_gpu_log.write_text("Devices:\n    [0] Apple M4 Pro (integrated, 65536 MB)\n", encoding="utf-8")
    listed_only_gpu = _gpu_initialization_evidence(listed_only_gpu_log, "Apple M4 Pro")
    assert not listed_only_gpu["initialized"], listed_only_gpu
    workspace, fake = _project_fixture(dest / "happy-path", derived_inputs=True)
    plan_text = run("plan", workspace)
    assert '"sfm", "auto"' in plan_text and '"--max-image-size", "640"' in plan_text
    plan_path = dest / "level-b-plan.txt"
    plan_path.write_text(plan_text, encoding="utf-8")
    result = json.loads(run("run", workspace))
    assert result["stages"]["sfm"] in {"COMPLETED", "SKIPPED_VALID_CACHE"}, result
    assert result["stages"]["training"] == "COMPLETED", result
    assert result["stages"]["evaluation"] == "NEEDS_REVIEW", result
    assert result["needs_review"] == ["evaluation"]
    status = json.loads(run("status", workspace))
    assert status["stages"]["training"]["status"] == "COMPLETED"
    training_detail = json.loads(status["stages"]["training"]["details_json"])
    assert training_detail["model_status"] == "SMOKE_TEST_MODEL"
    assert training_detail["public_selection_allowed"] is False
    training_report = json.loads((workspace / "deliverable/work/training-run.json").read_text())
    assert training_report["checkpoint_resume_execution_status"] == "NOT_TESTED"
    assert training_report["model"]["gaussian_count"] == 2
    assert training_report["image_derivative_provenance"]["mapped_image_count"] == 8
    training_attempt_record = json.loads((Path(training_detail["attempt_directory"]) / "command.json").read_text())
    assert training_attempt_record["terminal_state"] == "SUCCEEDED"
    assert training_attempt_record["process_terminal_state"] == "SUCCEEDED"
    assert training_attempt_record["exit_code"] == 0
    assert training_attempt_record["artifact_discovery_status"] == "FOUND_VALIDATED"
    sfm_detail = json.loads(status["stages"]["sfm"]["details_json"])
    sfm_attempt_record = json.loads((Path(sfm_detail["attempt_directory"]) / "command.json").read_text())
    assert sfm_attempt_record["terminal_state"] == "SUCCEEDED" and sfm_attempt_record["exit_code"] == 0
    smoke_review = run("review-model", workspace, "--decision", "approve", "--candidate-sha256", training_detail["model_sha256"], "--reviewer", "test-reviewer", "--rationale", "test gate must reject smoke outputs", expect=1)
    assert "SMOKE_TEST_MODEL artifacts cannot be approved" in smoke_review
    # Identical rerun is cache-only; a changed training cap invalidates only the
    # affected stage and downstream review, leaving the validated SfM intact.
    repeated = json.loads(run("run", workspace))
    assert "sfm" in repeated["skipped"] and "training" in repeated["skipped"]
    before_source = sorted((p.name, p.stat().st_size, p.read_bytes()) for p in (dest / "happy-path/source/camera-roll").iterdir())

    # Separate failure fixture proves failed subprocess persistence and bounded
    # retries without touching the successful upstream baseline above.
    failure_workspace, _failure_fake = _project_fixture(dest / "failure-path", fail_sfm=True)
    run("run", failure_workspace, expect=1)
    failure_state = json.loads(run("status", failure_workspace))
    assert failure_state["stages"]["sfm"]["status"] == "FAILED"
    with sqlite3.connect(failure_workspace / "workflow.sqlite3") as db:
        failed_execution = db.execute("SELECT status,error,log_path FROM stage_executions WHERE stage_id='sfm' ORDER BY execution_id DESC LIMIT 1").fetchone()
    assert failed_execution[0] == "FAILED" and "code 17" in failed_execution[1]
    assert "controlled fake SfM failure" in Path(failed_execution[2]).read_text()
    failed_sfm_record = json.loads((Path(failed_execution[2]).parent / "command.json").read_text())
    assert failed_sfm_record["terminal_state"] == "NONZERO_EXIT"
    assert failed_sfm_record["process_terminal_state"] == "NONZERO_EXIT"
    assert failed_sfm_record["exit_code"] == 17 and failed_sfm_record["log_path"] == str(Path(failed_execution[2]).resolve())
    retry_output = run("resume", failure_workspace, expect=1)
    assert "reached the configured bound" in retry_output

    # Spirula exit code 3 means it wrote a partial reconstruction. Preserve and
    # validate it for human review instead of classifying it as a lost attempt.
    partial_workspace, _ = _project_fixture(dest / "partial-sfm-path", partial_sfm=True)
    partial_result = json.loads(run("run", partial_workspace))
    assert "sfm" in partial_result["needs_review"], partial_result
    assert partial_result["stages"]["sfm"] == "NEEDS_REVIEW", partial_result
    partial_status = json.loads(run("status", partial_workspace))
    assert partial_status["stages"]["sfm"]["status"] == "NEEDS_REVIEW"
    partial_details = json.loads(partial_status["stages"]["sfm"]["details_json"])
    assert partial_details["quality_gate"] == "SFM_NEEDS_REVIEW"
    assert partial_details["registered_image_count"] == 6
    assert partial_details["source_image_count"] == 8
    assert partial_details["backend_partial_output"] is True
    partial_attempt = Path(partial_details["attempt_directory"])
    partial_record = json.loads((partial_attempt / "command.json").read_text())
    assert partial_record["terminal_state"] == "PARTIAL_OUTPUT"
    assert partial_record["exit_code"] == 3
    partial_validation = json.loads((partial_attempt / "sfm-validation.json").read_text())
    assert partial_validation["backend_partial_output"] is True
    assert "SPIRULA_REPORTED_PARTIAL_OUTPUT" in partial_validation["warnings"]
    assert (partial_attempt / "workspace/sparse/0/points3D.bin").is_file()

    # Deterministic pre-launch guards: unsupported flags, insufficient source
    # images, and insufficient free space fail before any backend PID is set.
    unsupported_workspace, _ = _project_fixture(dest / "unsupported-flag-path", sfm_extra_args=["--not-a-real-flag"])
    unsupported_output = run("run", unsupported_workspace, expect=1)
    assert "Unsupported SfM extra flags" in unsupported_output
    too_small_workspace, _ = _project_fixture(dest / "too-small-path", photo_count=1)
    too_small_output = run("run", too_small_workspace, expect=1)
    assert "At least two supported images" in too_small_output
    disk_workspace, _ = _project_fixture(dest / "disk-shortage-path", min_disk_bytes=10**30)
    disk_output = run("run", disk_workspace, expect=1)
    assert "Insufficient free disk space for sfm" in disk_output

    # Training errors leave accepted upstream SfM intact and never register an
    # incomplete model as a successful stage.
    train_failure_workspace, _ = _project_fixture(dest / "training-failure-path", fail_training=True)
    run("run", train_failure_workspace, expect=1)
    train_failure_state = json.loads(run("status", train_failure_workspace))
    assert train_failure_state["stages"]["sfm"]["status"] == "COMPLETED"
    assert train_failure_state["stages"]["training"]["status"] == "FAILED"
    with sqlite3.connect(train_failure_workspace / "workflow.sqlite3") as db:
        failed_training = db.execute("SELECT error,log_path FROM stage_executions WHERE stage_id='training' ORDER BY execution_id DESC LIMIT 1").fetchone()
    failed_training_record = json.loads((Path(failed_training[1]).parent / "command.json").read_text())
    assert "code 19" in failed_training[0]
    assert failed_training_record["terminal_state"] == "NONZERO_EXIT" and failed_training_record["exit_code"] == 19
    missing_model_workspace, _ = _project_fixture(dest / "missing-model-path", no_model=True)
    missing_model_output = run("run", missing_model_workspace, expect=1)
    assert "No PLY model artifacts" in missing_model_output
    with sqlite3.connect(missing_model_workspace / "workflow.sqlite3") as db:
        missing_model_log = db.execute("SELECT log_path FROM stage_executions WHERE stage_id='training' ORDER BY execution_id DESC LIMIT 1").fetchone()[0]
    missing_record = json.loads((Path(missing_model_log).parent / "command.json").read_text())
    assert missing_record["terminal_state"] == "VALIDATION_FAILED" and missing_record["artifact_discovery_status"] == "MISSING"
    corrupt_model_workspace, _ = _project_fixture(dest / "corrupt-model-path", corrupt_ply=True)
    corrupt_model_output = run("run", corrupt_model_workspace, expect=1)
    assert "PLY body is truncated" in corrupt_model_output or "Malformed ASCII PLY row" in corrupt_model_output
    with sqlite3.connect(corrupt_model_workspace / "workflow.sqlite3") as db:
        corrupt_model_log = db.execute("SELECT log_path FROM stage_executions WHERE stage_id='training' ORDER BY execution_id DESC LIMIT 1").fetchone()[0]
    corrupt_record = json.loads((Path(corrupt_model_log).parent / "command.json").read_text())
    assert corrupt_record["terminal_state"] == "VALIDATION_FAILED"
    assert corrupt_record["artifact_discovery_status"] == "FOUND_INVALID_OR_AMBIGUOUS"
    nonfinite_model_workspace, _ = _project_fixture(dest / "nonfinite-model-path", nonfinite_scale=True)
    nonfinite_output = run("run", nonfinite_model_workspace, expect=1)
    assert "Non-finite scale_0 value" in nonfinite_output
    with sqlite3.connect(nonfinite_model_workspace / "workflow.sqlite3") as db:
        nonfinite_model_log = db.execute("SELECT log_path FROM stage_executions WHERE stage_id='training' ORDER BY execution_id DESC LIMIT 1").fetchone()[0]
    nonfinite_record = json.loads((Path(nonfinite_model_log).parent / "command.json").read_text())
    assert nonfinite_record["terminal_state"] == "VALIDATION_FAILED"

    # A configured timeout records the actual signal exit code and keeps the
    # timeout classification separate from an ordinary nonzero backend exit.
    timeout_workspace, _ = _project_fixture(dest / "timeout-path", sleep_sfm=8, timeout_sfm_seconds=1)
    timeout_output = run("run", timeout_workspace, expect=1)
    assert "timed out" in timeout_output
    with sqlite3.connect(timeout_workspace / "workflow.sqlite3") as db:
        timeout_execution = db.execute("SELECT error,log_path FROM stage_executions WHERE stage_id='sfm' ORDER BY execution_id DESC LIMIT 1").fetchone()
    timeout_record = json.loads((Path(timeout_execution[1]).parent / "command.json").read_text())
    assert timeout_record["terminal_state"] == "TIMED_OUT" and timeout_record["error_classification"] == "TIMEOUT"
    assert timeout_record["exit_code"] in {-signal.SIGTERM, -signal.SIGKILL}

    # An explicitly configured candidate can be approved by a named reviewer.
    # The registered PLY then feeds the existing package model selector without
    # a manual copy; absent navigation still blocks viewer/package creation.
    candidate_workspace, _ = _project_fixture(dest / "candidate-path", run_type="candidate")
    run("run", candidate_workspace)
    candidate_status = json.loads(run("status", candidate_workspace))
    candidate_details = json.loads(candidate_status["stages"]["training"]["details_json"])
    review_result = json.loads(run("review-model", candidate_workspace, "--decision", "approve", "--candidate-sha256", candidate_details["model_sha256"], "--reviewer", "fixture-curator", "--rationale", "Verified candidate smoke fixture for workflow handoff only."))
    assert review_result["status"] == "APPROVED" and review_result["public_selection_allowed"] is True
    resume_output = run("resume", candidate_workspace, expect=1)
    assert "viewer_package is blocked by incomplete dependencies" in resume_output.casefold()
    assert "navigation" in resume_output.casefold()
    candidate_state = json.loads(run("status", candidate_workspace))
    assert candidate_state["stages"]["evaluation"]["status"] == "COMPLETED"
    selection_path = candidate_workspace / "deliverable/work/model-selection.json"
    selection = json.loads(selection_path.read_text())
    assert selection["sha256"] == candidate_details["model_sha256"]
    sys.path.insert(0, str(PRODUCT / "src"))
    from exhibition_archive.model_registry import selected_model_record
    from exhibition_archive.state import ProjectState
    from exhibition_archive.workflow import load_project
    project_root, project_config = load_project(candidate_workspace)
    candidate_store = ProjectState(project_root)
    try:
        selected_record = selected_model_record(project_root, project_config, candidate_store)
    finally:
        candidate_store.close()
    assert selected_record["selection"] == "human-approved fresh candidate"

    # Cancellation sends SIGTERM to the backend process group, captures the
    # final nonzero status, and persists CANCELLED without losing its log.
    cancel_workspace, _ = _project_fixture(dest / "cancel-path", sleep_sfm=20)
    cancel_process, cancel_pid, cancel_stdout = _start_authorized_backend(
        cancel_workspace, "sfm", dest / "cancel-path" / "workflow.stdout.txt")
    cancel_report = json.loads(run("cancel", cancel_workspace, "sfm", "--grace-seconds", "0.5"))
    assert cancel_report["status"].startswith("TERMINATED_") and cancel_report["pid"] == cancel_pid
    assert cancel_process.wait(timeout=8) == 1
    cancel_stdout.close()
    with sqlite3.connect(cancel_workspace / "workflow.sqlite3") as db:
        cancelled = db.execute("SELECT status,log_path FROM stage_executions WHERE stage_id='sfm' ORDER BY execution_id DESC LIMIT 1").fetchone()
    assert cancelled[0] == "CANCELLED" and Path(cancelled[1]).is_file()
    cancel_record = json.loads((Path(cancelled[1]).parent / "command.json").read_text())
    assert cancel_record["terminal_state"] == "CANCELLED" and cancel_record["exit_code"] == -signal.SIGTERM

    # Simulate a CLI crash while an independently grouped backend process is
    # running. The backend exits on its own; status must reconcile the orphaned
    # RUNNING record to INTERRUPTED rather than claiming it can reattach.
    interrupted_workspace, _ = _project_fixture(dest / "interrupted-path", sleep_sfm=1.2)
    interrupted_process, interrupted_pid, interrupted_stdout = _start_authorized_backend(
        interrupted_workspace, "sfm", dest / "interrupted-path" / "workflow.stdout.txt")
    interrupted_process.kill()
    interrupted_process.wait(timeout=3)
    time.sleep(3.3)
    interrupted_state = json.loads(run("status", interrupted_workspace))
    interrupted_stdout.close()
    assert interrupted_state["stages"]["sfm"]["status"] == "FAILED"
    with sqlite3.connect(interrupted_workspace / "workflow.sqlite3") as db:
        interrupted_execution = db.execute("SELECT status,pid,log_path FROM stage_executions WHERE stage_id='sfm' ORDER BY execution_id DESC LIMIT 1").fetchone()
    assert interrupted_execution[0:2] == ("INTERRUPTED", interrupted_pid)
    interrupted_record = json.loads((Path(interrupted_execution[2]).parent / "command.json").read_text())
    assert interrupted_record["terminal_state"] == "INTERRUPTED"
    assert interrupted_record["exit_code_unavailable_reason"] == "PROCESS_EXIT_STATUS_LOST_WITH_ORCHESTRATOR"

    # Unexpected orchestration exceptions after Popen terminate the process and
    # are written to the attempt record without depending on SQLite success.
    from exhibition_archive.backends import BackendError, execute_process
    unexpected_dir = dest / "unexpected-exception-attempt"
    unexpected_dir.mkdir()
    (unexpected_dir / "command.json").write_text(json.dumps({"stage": "sfm", "command": [sys.executable, "-c", "import time; time.sleep(10)"]}) + "\n")
    class BrokenState:
        def update_stage_execution(self, *args, **kwargs):
            raise RuntimeError("controlled state persistence error")
        def stage(self, _stage):
            return {}
        def set_stage(self, *args, **kwargs):
            pass
    try:
        execute_process(BrokenState(), 1, "sfm", [sys.executable, "-c", "import time; time.sleep(10)"],
            unexpected_dir, unexpected_dir / "spirula.log", timeout_seconds=20, poll_interval=0.1)
        raise AssertionError("expected controlled orchestration exception")
    except BackendError as exc:
        assert exc.failure_kind == "UNEXPECTED_EXCEPTION"
    unexpected_record = json.loads((unexpected_dir / "command.json").read_text())
    assert unexpected_record["terminal_state"] == "UNEXPECTED_EXCEPTION"
    assert unexpected_record["exit_code"] in {-signal.SIGTERM, -signal.SIGKILL}
    assert "controlled state persistence error" in unexpected_record["error"]
    assert (unexpected_dir / "spirula.log").is_file()

    # Failure of the atomic command-record write is swallowed only after
    # preserving the original log and leaving its prior JSON intact.
    import exhibition_archive.backends as backend_module
    finalizer_dir = dest / "finalizer-write-failure"
    finalizer_dir.mkdir()
    original_command = {"terminal_state": "NONZERO_EXIT", "exit_code": 17}
    (finalizer_dir / "command.json").write_text(json.dumps(original_command) + "\n")
    (finalizer_dir / "spirula.log").write_text("original backend failure evidence\n")
    original_write_json = backend_module.write_json
    def controlled_write_failure(*args, **kwargs):
        raise OSError("controlled finalization write failure")
    backend_module.write_json = controlled_write_failure
    try:
        assert backend_module.finalize_attempt_record(finalizer_dir, "VALIDATION_FAILED") is False
    finally:
        backend_module.write_json = original_write_json
    assert json.loads((finalizer_dir / "command.json").read_text()) == original_command
    assert "original backend failure evidence" in (finalizer_dir / "spirula.log").read_text()
    assert "controlled finalization write failure" in (finalizer_dir / "command.json.finalization-error.txt").read_text()

    report = {
      "schema_version": "exhibition-archive.fresh-backend-test.v1", "status": "PASS",
      "scope": "Level B fake-backend integration; no real SfM, GPU training, Chuen Lung data, or image decoding",
      "fixture": {"project_id": "indigo-room-fixture", "image_names_are_site_specific_free": True, "image_count": 8,
                  "coordinate_layout": "synthetic COLMAP camera translations; independent fixture only"},
      "checks": {"fresh_sfm_workflow": "PASS", "fresh_training_workflow": "PASS", "colmap_binary_validation": "PASS",
                 "gaussian_ply_validation": "PASS", "smoke_model_never_public_selected": "PASS",
                 "fresh_evaluation_human_gate": "PASS", "valid_stage_cache_reuse": "PASS",
                 "bounded_failure_persistence": "PASS", "unsupported_flag_guard": "PASS", "invalid_input_guard": "PASS",
                 "disk_shortage_guard": "PASS", "training_failure_preserves_sfm": "PASS", "missing_model_rejected": "PASS",
                 "spirula_exit_3_partial_sfm_retained_and_review_gated": "PASS",
                 "corrupt_model_rejected": "PASS", "nonfinite_gaussian_attributes_rejected": "PASS", "smoke_model_rejected_for_public_selection": "PASS",
                 "explicit_candidate_handoff": "PASS; model-selection.json consumes registered artifact; navigation still blocks packaging",
                 "fresh_model_training_does_not_depend_on_navigation_review": "PASS; training/evaluation complete while viewer packaging remains blocked without navigation",
                 "failed_sfm_command_finalization": "PASS", "failed_training_command_finalization": "PASS",
                 "successful_command_finalization": "PASS", "timeout_finalization": "PASS",
                 "missing_artifact_finalization": "PASS", "corrupt_ply_finalization": "PASS",
                 "cancellation": "PASS", "orphaned_process_reconciliation": "PASS; attempt marked INTERRUPTED, not reattached",
                 "unexpected_exception_finalization": "PASS", "finalization_write_failure_preserves_error_evidence": "PASS",
                 "spirula_selected_device_log_format": "PASS; selected Apple device accepted, unselected device listing rejected",
                 "training_resume_claim": "NOT_TESTED; correctly recorded", "source_bytes_unchanged": before_source == sorted((p.name, p.stat().st_size, p.read_bytes()) for p in (dest / "happy-path/source/camera-roll").iterdir())},
      "plan": str(plan_path), "success_project": str(workspace), "failure_project": str(failure_workspace),
      "cancel_project": str(cancel_workspace), "interrupted_project": str(interrupted_workspace),
      "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    }
    report_path = dest / "fresh-backend-test-results.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "report": str(report_path), "workspace": str(workspace)}, indent=2))


if __name__ == "__main__":
    main()
