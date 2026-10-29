#!/usr/bin/env python3
"""Run the independent synthetic project through init, failure, resume and cache checks."""
from __future__ import annotations

import json
import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


PRODUCT = Path(__file__).resolve().parents[1]
BASE_DEST = PRODUCT / ".local" / "validation" / "portability"
BIN = PRODUCT / "bin" / "exhibition-archive"
GENERATOR = PRODUCT / "tests" / "generate_independent_fixture.py"


def command(*args, expect=0):
    result = subprocess.run([str(BIN), *map(str, args)], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode != expect:
        raise RuntimeError(f"Command returned {result.returncode}, expected {expect}: {args}\n{result.stdout}")
    try: return json.loads(result.stdout)
    except json.JSONDecodeError: return result.stdout


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-dest", type=Path, default=BASE_DEST,
                        help="where timestamped synthetic test workspaces and results are written")
    args = parser.parse_args(argv)
    stamp = datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%SZ")
    global DEST
    DEST = args.base_dest.resolve() / stamp
    DEST.mkdir(parents=True, exist_ok=True)
    generated = subprocess.run([sys.executable, str(GENERATOR), str(DEST / "fixture")], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if generated.returncode:
        raise RuntimeError(generated.stdout)
    gen = json.loads(generated.stdout)
    workspace = DEST / "fixture" / "workspace"
    command("init", "--config", DEST / "fixture" / "project-config.json", "--workspace", workspace)
    failed = command("run", workspace, "--fail-at", "viewer_package", expect=1)
    state = command("status", workspace)
    assert state["stages"]["photo_inventory"]["status"] == "COMPLETED"
    assert state["stages"]["capture_preflight"]["status"] == "COMPLETED"
    assert state["stages"]["artwork_association"]["status"] == "COMPLETED"
    assert state["stages"]["sfm"]["status"] == "ADOPTED"
    assert state["stages"]["training"]["status"] == "ADOPTED"
    assert state["stages"]["viewer_package"]["status"] == "FAILED"
    resumed = command("resume", workspace)
    assert resumed["stages"]["viewer_package"] == "COMPLETED"
    assert resumed["stages"]["archive_package"] == "COMPLETED"
    assert resumed["stages"]["verification"] == "COMPLETED"
    packaged = command("package", workspace)
    assert packaged["mode"] == "package_only" and len(packaged["skipped"]) == 10, packaged
    repeated = command("run", workspace)
    assert len(repeated["skipped"]) == 10, repeated
    command("config-set", workspace, "--key", "viewer.theme", "--value", '"light"')
    light_state = command("status", workspace)
    invalidated = {key for key, value in light_state["stages"].items() if value["status"] == "INVALIDATED"}
    assert invalidated == {"viewer_package", "archive_package", "verification"}, invalidated
    assert light_state["stages"]["training"]["status"] == "ADOPTED"
    assert light_state["stages"]["sfm"]["status"] == "ADOPTED"
    command("config-set", workspace, "--key", "viewer.theme", "--value", '"dark"')
    restored_state = command("status", workspace)
    assert all(restored_state["stages"][x]["status"] in {"COMPLETED", "ADOPTED"} for x in ("viewer_package", "archive_package", "verification"))
    verified = command("verify", workspace)
    assert verified["status"] == "PASS", verified
    output_root = workspace / "deliverable"
    site = output_root / "website"
    public_app = (site / "viewer" / "app.js").read_text(encoding="utf-8")
    assert "IMG_" not in public_app and "attention-" not in public_app and "chuen-lung" not in public_app.casefold()
    preflight_with_sfm = json.loads((output_root / "work/capture-quality-summary.json").read_text())
    assert preflight_with_sfm["sfm_status"] == "AVAILABLE_BASIC_COUNTS_ONLY"
    assert preflight_with_sfm["angular_coverage"] == "UNKNOWN_REGION_ORIENTATION_ANALYSIS_NOT_IMPLEMENTED"

    no_sfm_config = json.loads((DEST / "fixture" / "project-config.json").read_text())
    no_sfm_config["project_id"] = "citrine-image-only-fixture"
    no_sfm_config["integrations"] = {}
    no_sfm_config_path = DEST / "fixture" / "image-only-project.json"
    no_sfm_config_path.write_text(json.dumps(no_sfm_config, indent=2) + "\n", encoding="utf-8")
    no_sfm_workspace = DEST / "fixture" / "image-only-workspace"
    command("init", "--config", no_sfm_config_path, "--workspace", no_sfm_workspace)
    blocked_run = subprocess.run([str(BIN), "run", str(no_sfm_workspace)], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert blocked_run.returncode == 1 and "stage sfm blocked" in blocked_run.stdout.casefold(), blocked_run.stdout
    image_only_status = command("status", no_sfm_workspace)
    no_sfm_preflight = json.loads((no_sfm_workspace / "deliverable/work/capture-quality-summary.json").read_text())
    assert image_only_status["stages"]["sfm"]["status"] == "BLOCKED"
    assert no_sfm_preflight["sfm_status"] == "UNKNOWN_NOT_CONFIGURED"
    assert no_sfm_preflight["angular_coverage"] == "UNKNOWN_SFM_UNAVAILABLE"

    # A fresh backend configuration cannot turn `package` into an implicit GPU
    # run when its current validated prerequisites do not exist.
    fresh_config = json.loads((DEST / "fixture" / "project-config.json").read_text())
    fresh_config["project_id"] = "citrine-fresh-package-only-fixture"
    fresh_config["integrations"]["sfm"] = {"mode": "spirula_fresh", "image_directory": str(Path(gen["fixture_root"]) / "source-photos"), "executable": "spirula"}
    fresh_config["integrations"]["model"] = {"mode": "spirula_fresh", "run_type": "candidate", "preset": "3dgs", "iterations": 100}
    fresh_config["resource_policy"] = {"max_training_iterations": 1000}
    fresh_config_path = DEST / "fixture" / "fresh-package-only-project.json"
    fresh_config_path.write_text(json.dumps(fresh_config, indent=2) + "\n", encoding="utf-8")
    fresh_package_workspace = DEST / "fixture" / "fresh-package-only-workspace"
    command("init", "--config", fresh_config_path, "--workspace", fresh_package_workspace)
    package_blocked = subprocess.run([str(BIN), "package", str(fresh_package_workspace)], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert package_blocked.returncode == 3, package_blocked.stdout
    package_blocked_result = json.loads(package_blocked.stdout)
    assert package_blocked_result["status"] == "BLOCKED_PACKAGE_PREREQUISITE"
    import sqlite3
    with sqlite3.connect(fresh_package_workspace / "workflow.sqlite3") as state_db:
        assert state_db.execute("SELECT COUNT(*) FROM stage_executions").fetchone()[0] == 0
    report = {
        "schema_version":"exhibition-archive.portability-test.v1", "status":"PASS", "test_scope":"software portability on synthetic independent fixture; not a second real exhibition",
        "project_id":"citrine-gallery-fixture", "source_image_count":2, "synthetic_sfm_images":2, "synthetic_model_gaussians":1,
        "commands":["init --config project-config.json --workspace workspace", "run --fail-at viewer_package", "resume", "package (valid cache only)", "run (cache check)", "config-set viewer.theme light", "config-set viewer.theme dark", "verify"],
        "image_only_preflight": {"status":preflight_with_sfm["mode"], "configured_sfm_status":preflight_with_sfm["sfm_status"], "region_angular_coverage":preflight_with_sfm["angular_coverage"], "no_sfm_fallback_status":no_sfm_preflight["sfm_status"], "no_sfm_angular_coverage":no_sfm_preflight["angular_coverage"], "missing_sfm_remains_unknown":True, "downstream_sfm_stage":"BLOCKED"},
        "failure_recovery":{"injected_stage":"viewer_package","first_status":"FAILED","resumed":"COMPLETED","adopted_sfm_and_model_not_repeated":True},
        "package_only_safety":{"valid_cache_package":"PASS","fresh_backend_prerequisite":"BLOCKED_WITHOUT_STAGE_EXECUTION","backend_processes_started":0},
        "repeat_run_skipped_stages":repeated["skipped"], "selective_invalidation":sorted(invalidated),
        "final_stage_statuses":{key:value["status"] for key,value in restored_state["stages"].items()},
        "verification":verified, "fixture_root":str(DEST / "fixture"),
        "generated_at_utc":datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")
    }
    (DEST / "portability-test-results.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status":"PASS","report":str(DEST / "portability-test-results.json"),"skipped":repeated["skipped"],"invalidated":sorted(invalidated)},indent=2))


if __name__ == "__main__":
    main()
