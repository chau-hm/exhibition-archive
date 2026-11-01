from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .adapters import read_json, sha256_file, write_json
from .backends import (
    BackendError,
    _parse_colmap,
    build_training_command,
    check_resources,
    create_attempt_dir,
    discover_checkpoints,
    discover_environment,
    discover_model_files,
    execute_process,
    finalize_attempt_record,
    image_inventory,
    prepare_training_dataset,
    validate_gaussian_ply,
)
from .state import ProjectState, utc_now
from .workflow import output_path, project_path


MAX_SMOKE_ITERATIONS = 300
SMOKE_STAGE = "smoke_training"


def _digest_tree(root: Path) -> tuple[str, list[dict[str, Any]]]:
    rows = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        if path.is_symlink():
            raise BackendError(f"Smoke component contains a symlink: {path}", failure_kind="INVALID_INPUT")
        rows.append({"relative_path": path.relative_to(root).as_posix(), "size_bytes": path.stat().st_size, "sha256": sha256_file(path)})
    encoded = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest(), rows


def _camera_dimensions(path: Path) -> list[dict[str, int]]:
    rows = []
    with path.open("rb") as stream:
        raw_count = stream.read(8)
        if len(raw_count) != 8:
            raise BackendError("COLMAP cameras.bin header is truncated", failure_kind="SFM_INVALID")
        count = struct.unpack("<Q", raw_count)[0]
        param_counts = {0:3,1:4,2:4,3:5,4:8,5:8,6:12,7:5,8:4,9:5,10:12,11:16}
        for _ in range(count):
            raw = stream.read(24)
            if len(raw) != 24:
                raise BackendError("COLMAP camera record is truncated", failure_kind="SFM_INVALID")
            camera_id, model_id, width, height = struct.unpack("<iiQQ", raw)
            n = param_counts.get(model_id)
            if n is None or width < 1 or height < 1:
                raise BackendError("COLMAP camera model or dimensions are invalid", failure_kind="SFM_INVALID")
            params = stream.read(n * 8)
            if len(params) != n * 8 or not all(math.isfinite(x) for x in struct.unpack(f"<{n}d", params)):
                raise BackendError("COLMAP camera intrinsics are truncated or non-finite", failure_kind="SFM_INVALID")
            rows.append({"camera_id": camera_id, "model_id": model_id, "width": width, "height": height})
        if stream.read(1):
            raise BackendError("COLMAP cameras.bin has trailing data", failure_kind="SFM_INVALID")
    return rows


def _validate_smoke_policy(root: Path, config: dict, operator_authorized: bool) -> tuple[dict, dict, Path, Path, Path]:
    policy = config.get("diagnostic_policy", {})
    if policy.get("mode") != "SMOKE_TEST_ONLY":
        raise BackendError("diagnostic_policy.mode must be SMOKE_TEST_ONLY", failure_kind="INVALID_CONFIGURATION")
    if not operator_authorized or policy.get("gpu_training_authorized_by_operator") is not True:
        raise BackendError("GPU training requires both the explicit CLI authorization flag and the isolated project authorization record", failure_kind="AUTHORIZATION_REQUIRED")
    if policy.get("public_selection_allowed") is not False or config.get("publication", {}).get("public_site") is not False:
        raise BackendError("Diagnostic smoke projects must explicitly disable public selection and publication", failure_kind="UNSAFE_OUTPUT")
    model_cfg = config.get("integrations", {}).get("model", {})
    if model_cfg.get("mode") != "spirula_fresh" or model_cfg.get("run_type") != "smoke_test":
        raise BackendError("Diagnostic training requires spirula_fresh with model.run_type=smoke_test", failure_kind="INVALID_CONFIGURATION")
    iterations = model_cfg.get("iterations")
    if not isinstance(iterations, int) or isinstance(iterations, bool) or iterations < 1 or iterations > MAX_SMOKE_ITERATIONS:
        raise BackendError(f"Smoke training iterations must be between 1 and the hard {MAX_SMOKE_ITERATIONS}-iteration cap", failure_kind="RESOURCE_LIMIT")
    if int(config.get("resource_policy", {}).get("max_training_iterations", 0)) > MAX_SMOKE_ITERATIONS:
        raise BackendError("The diagnostic resource policy may not raise the hard 300-iteration cap", failure_kind="RESOURCE_LIMIT")
    if policy.get("component_id") != "sparse/0" and not isinstance(policy.get("component_id"), str):
        raise BackendError("A named COLMAP component is required", failure_kind="INVALID_CONFIGURATION")
    manifest_path = project_path(root, config, policy.get("component_manifest", ""))
    component_dir = project_path(root, config, policy.get("component_directory", ""))
    image_dir = project_path(root, config, config.get("source", {}).get("photo_directory", ""))
    for label, path in (("component manifest", manifest_path), ("component", component_dir), ("images", image_dir)):
        if root.resolve() not in path.resolve().parents or not path.exists():
            raise BackendError(f"Smoke {label} must exist inside the isolated diagnostic project: {path}", failure_kind="UNSAFE_OUTPUT")
    return policy, model_cfg, manifest_path, component_dir, image_dir


def _validate_component_inputs(root: Path, policy: dict, manifest_path: Path, component_dir: Path, image_dir: Path) -> dict:
    manifest = read_json(manifest_path)
    if manifest.get("policy") != "SMOKE_TEST_ONLY" or manifest.get("component_id") != policy.get("component_id"):
        raise BackendError("Selected component manifest does not match the explicit diagnostic policy", failure_kind="INVALID_INPUT")
    expected_component_digest, file_rows = _digest_tree(component_dir)
    if expected_component_digest != manifest.get("component_copy_sha256"):
        raise BackendError("Copied COLMAP component hash does not match the selected-component manifest", failure_kind="INVALID_INPUT")
    expected_files = manifest.get("component_files", [])
    if file_rows != expected_files:
        raise BackendError("Copied COLMAP component file membership or content changed", failure_kind="INVALID_INPUT")

    inventory = image_inventory(image_dir)
    actual = {row["relative_path"]: row for row in inventory["images"]}
    expected_images = {row["image_id"]: row for row in manifest.get("images", [])}
    if set(actual) != set(expected_images):
        raise BackendError("Smoke image directory membership differs from the selected component's registered image list", failure_kind="INVALID_INPUT",
                           details={"extra_images": sorted(set(actual)-set(expected_images)), "missing_images": sorted(set(expected_images)-set(actual))})
    original_root = Path(policy.get("source_heic_directory", "")).expanduser().resolve()
    if not original_root.is_dir():
        raise BackendError("Original HEIC provenance directory is unavailable", failure_kind="INVALID_INPUT")
    from .workflow import _image_dimensions
    for name, expected in expected_images.items():
        row = actual[name]
        image_path = Path(row["source_path"])
        if row["sha256"] != expected["derived_sha256"]:
            raise BackendError(f"Derived JPEG hash mismatch for {name}", failure_kind="INVALID_INPUT")
        width, height = _image_dimensions(image_path)
        if [width, height] != expected["dimensions"]:
            raise BackendError(f"Image dimensions changed for {name}", failure_kind="INVALID_INPUT")
        original = (original_root / expected["source_relative_path"]).resolve()
        if original_root not in original.parents or not original.is_file() or sha256_file(original) != expected["source_sha256"]:
            raise BackendError(f"Original HEIC source mapping/hash failed for {name}", failure_kind="INVALID_INPUT")

    rows = [{"image_id": name, "relative_path": name, "sha256": actual[name]["sha256"],
             "width": expected_images[name]["dimensions"][0], "height": expected_images[name]["dimensions"][1]}
            for name in sorted(expected_images)]
    parsed = _parse_colmap(component_dir, rows)
    if set(parsed["registered_image_names"]) != set(expected_images):
        raise BackendError("COLMAP image registrations do not exactly match the selected image manifest", failure_kind="SFM_INVALID")
    if parsed["registered_image_count"] < 2 or parsed["sparse_point_count"] < 1 or parsed["track_reciprocal_reference_mismatches"]:
        raise BackendError("COLMAP component failed structural smoke validation", failure_kind="SFM_INVALID", details=parsed)
    camera_rows = _camera_dimensions(component_dir / "cameras.bin")
    camera_sizes = {(c["width"], c["height"]) for c in camera_rows}
    image_sizes = {tuple(row["dimensions"]) for row in expected_images.values()}
    if len(camera_sizes) != 1 or camera_sizes != image_sizes:
        raise BackendError("COLMAP camera dimensions do not match the selected JPEG derivative dimensions", failure_kind="SFM_INVALID",
                           details={"camera_dimensions": sorted(camera_sizes), "image_dimensions": sorted(image_sizes)})
    return {"manifest": manifest, "manifest_path": str(manifest_path.resolve()), "manifest_sha256": sha256_file(manifest_path),
        "component_directory": str(component_dir.resolve()), "component_copy_sha256": expected_component_digest,
        "component_files": file_rows, "registered_image_count": parsed["registered_image_count"],
        "sparse_point_count": parsed["sparse_point_count"], "camera_count": parsed["camera_count"],
        "camera_model_ids": parsed["camera_model_ids"], "camera_dimensions": camera_rows,
        "mean_track_length": parsed["mean_track_length"], "median_track_length": parsed["median_track_length"],
        "track_reciprocal_reference_mismatches": parsed["track_reciprocal_reference_mismatches"],
        "all_camera_parameters_finite": parsed["all_camera_parameters_finite"],
        "all_camera_references_resolve": parsed["all_camera_references_resolve"],
        "all_image_poses_finite": parsed["all_image_poses_finite"], "all_3d_points_finite": parsed["all_3d_points_finite"],
        "registered_image_ids": parsed["registered_image_names"], "images": rows,
        "structural_status": "STRUCTURALLY_VALID", "quality_gate": "SFM_NEEDS_REVIEW",
        "production_quality_gate_bypassed": False,
        "interpretation": "Structural input validation for this isolated technical smoke run; production SfM status and thresholds remain unchanged."}


def _gpu_initialization_evidence(log_path: Path, requested_device: str) -> dict:
    text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
    needles = [f"Using {requested_device}", f"Created VkDevice to run on GPU {requested_device}"]
    matches = [line.strip() for line in text.splitlines() if any(needle in line for needle in needles)]
    # Spirula 2026.10.16 prints its selected backend as a starred device row
    # (for example, "  * [0] Apple M4 Pro (integrated, 65536 MB)") rather
    # than a Vulkan "Created VkDevice" line. Require the selection marker;
    # merely listing an available device is not sufficient evidence.
    selected_device = re.compile(rf"^\s*\*\s*\[\d+\]\s+{re.escape(requested_device)}(?:\s+\(|\s*$)")
    for line in text.splitlines():
        if selected_device.search(line):
            matches.append(line.strip())
    return {"requested_device": requested_device, "initialized": bool(matches), "evidence_lines": matches,
            "evidence_method": "EXPLICIT_DEVICE_INITIALIZATION_OR_SELECTED_DEVICE_ROW",
            "status": "VERIFIED_BY_EXECUTION" if matches else "GPU_DEVICE_NOT_CONFIRMED"}


def _discover_smoke_model(attempt: Path, model_cfg: dict) -> tuple[Path, list[dict[str, Any]], str]:
    files = discover_model_files(attempt / "outputs", model_cfg.get("artifact_selector"), allow_multiple=True)
    if not files:
        raise BackendError(f"No PLY model artifacts found beneath {attempt / 'outputs'}", failure_kind="MODEL_OUTPUT_MISSING")
    rows = []
    for path in files:
        rel = path.relative_to(attempt).as_posix()
        match = re.search(r"(?:iteration|iter|step|checkpoint)[_-]?(\d+)", rel, re.IGNORECASE)
        rows.append({"path": str(path.resolve()), "relative_path": rel, "iteration": int(match.group(1)) if match else None,
                     "size_bytes": path.stat().st_size, "sha256": sha256_file(path)})
    if len(files) == 1:
        return files[0], rows, "ONLY_DISCOVERED_PLY"
    if not all(row["iteration"] is not None for row in rows):
        raise BackendError("Multiple PLY outputs were found without an unambiguous iteration identifier; no model was selected",
                           failure_kind="MODEL_OUTPUT_AMBIGUOUS", details={"candidates": rows})
    latest = max(row["iteration"] for row in rows)
    latest_rows = [row for row in rows if row["iteration"] == latest]
    requested = int(model_cfg["iterations"])
    if latest != requested or len(latest_rows) != 1:
        raise BackendError("Multiple PLY outputs were found, but a unique final-iteration artifact could not be identified",
                           failure_kind="MODEL_OUTPUT_AMBIGUOUS", details={"requested_iterations": requested, "candidates": rows})
    selected = next(path for path in files if path.resolve().as_posix() == latest_rows[0]["path"])
    return selected, rows, "UNIQUE_HIGHEST_ITERATION_MATCHES_REQUESTED_TRAINING_CAP"


def preflight_smoke_training(root: Path, config: dict, *, operator_authorized: bool) -> dict:
    """Validate a smoke project and persist readiness before any GPU process starts."""
    root = root.resolve()
    policy, model_cfg, manifest_path, component_dir, image_dir = _validate_smoke_policy(root, config, operator_authorized)
    component = _validate_component_inputs(root, policy, manifest_path, component_dir, image_dir)
    output_root = output_path(root, config)
    output_root.mkdir(parents=True, exist_ok=True)
    resource_policy = config.get("resource_policy", {})
    resources = check_resources(output_root, resource_policy, "training")
    attempts_root = output_root / "backend-runs" / "smoke-training"
    existing_attempts = sorted(p.name for p in attempts_root.iterdir() if p.is_dir()) if attempts_root.is_dir() else []
    retention_limit = resource_policy.get("retained_attempt_limit")
    if retention_limit is not None and len(existing_attempts) >= retention_limit:
        raise BackendError("Retained diagnostic attempt limit reached; no existing attempts were changed", failure_kind="RESOURCE_LIMIT",
                           details={"attempts": existing_attempts, "limit": retention_limit})
    capabilities = discover_environment(model_cfg.get("executable"), root / "preflight/backend-discovery")
    if policy.get("expected_spirula_version") and policy["expected_spirula_version"] not in capabilities["tool"]["version"]:
        raise BackendError("Spirula version does not match the audited smoke configuration", failure_kind="BACKEND_VERSION_CHANGED")
    device = model_cfg.get("device")
    if device != policy.get("required_device") or "--device" not in capabilities["training"]["arguments"]:
        raise BackendError("Installed Spirula cannot explicitly select the configured Apple GPU", failure_kind="BACKEND_UNAVAILABLE")
    if model_cfg.get("steps_per_save") != 100:
        raise BackendError("The smoke run requires a 100-step save interval", failure_kind="INVALID_CONFIGURATION")
    placeholder = attempts_root / "<next-unique-attempt>"
    dataset = placeholder / "dataset"
    run_name = f"{config['project_id']}-diagnostic-<execution-id>"
    command = build_training_command(Path(capabilities["tool"]["executable"]), dataset, placeholder,
        {**model_cfg, "data_format": "colmap", "image_dir": "images", "colmap_recon_dir": "sparse/0"},
        capabilities, run_name)
    checkpoint_policy = {"steps_per_save": model_cfg.get("steps_per_save", 100),
                         "save_only_latest_checkpoint": model_cfg.get("save_only_latest_checkpoint", True),
                         "save_full_checkpoint": model_cfg.get("save_full_checkpoint", True),
                         "retention_owner": "SPIRULA_BACKEND"}
    report = {
        "schema_version": "phase2ez.training-readiness.v1", "policy": "SMOKE_TEST_ONLY",
        "status": "READY_TO_LAUNCH", "generated_at_utc": utc_now(),
        "operator_gpu_authorization": "RECORDED_AND_EXPLICITLY_CONFIRMED",
        "component_validation": component, "selected_image_count": len(component["images"]),
        "dataset_membership_matches_component": True,
        "trainer": {"executable": capabilities["tool"]["executable"], "version": capabilities["tool"]["version"],
            "preset": model_cfg["preset"], "iterations": model_cfg["iterations"], "hard_iteration_cap": MAX_SMOKE_ITERATIONS,
            "resolution_divisor": model_cfg["resolution_divisor"], "steps_per_save": model_cfg["steps_per_save"],
            "checkpoint_policy": checkpoint_policy, "device": device, "command_template": command},
        "production_sfm_status": "SFM_NEEDS_REVIEW", "production_quality_thresholds_changed": False,
        "production_quality_gate_bypassed": False, "available_disk_bytes": resources["free_bytes_before"],
        "minimum_free_disk_bytes": resources["minimum_required_bytes"], "resource_check": resources,
        "expected_output_directory_template": str(placeholder / "outputs" / run_name),
        "existing_smoke_attempts": existing_attempts, "non_publication": True,
        "known_quality_limitations": ["The selected component registered 19 of 40 source images.",
            "The other component is a separate coordinate frame and is excluded.",
            "Per-component reprojection error is unavailable; binary point error fields are uninformative zero.",
            "This run is not a production reconstruction or fidelity evaluation."],
    }
    target = root.parent / "component-audit/training-readiness.json"
    write_json(target, report)
    report["report_path"] = str(target.resolve())
    return report


def run_smoke_training(root: Path, config: dict, state: ProjectState, *, operator_authorized: bool) -> dict:
    """Run the isolated diagnostic training lane; never enters the production DAG."""
    root = root.resolve()
    policy, model_cfg, manifest_path, component_dir, image_dir = _validate_smoke_policy(root, config, operator_authorized)
    existing = state.stage(SMOKE_STAGE)
    if existing and existing.get("status") == "RUNNING":
        raise BackendError("A smoke-training execution is already recorded as running", failure_kind="ACTIVE_EXECUTION")
    run_id = state.start_run("SMOKE_TEST_ONLY")
    execution_id = state.start_stage(run_id, SMOKE_STAGE, "SMOKE_TEST_ONLY", False,
        {"component_id": policy["component_id"], "publication_allowed": False})
    state.set_stage(SMOKE_STAGE, "RUNNING", "SMOKE_TEST_ONLY", None, execution_id, {"mode": "SMOKE_TEST_ONLY"})
    attempt = None
    try:
        component = _validate_component_inputs(root, policy, manifest_path, component_dir, image_dir)
        resource_policy = config.get("resource_policy", {})
        attempt = create_attempt_dir(root, output_path(root, config), "smoke-training", execution_id,
                                     resource_policy.get("retained_attempt_limit"))
        resources = check_resources(attempt, resource_policy, "training")
        capabilities = discover_environment(model_cfg.get("executable"), attempt / "backend-discovery")
        expected_version = policy.get("expected_spirula_version")
        if expected_version and expected_version not in capabilities["tool"]["version"]:
            raise BackendError(f"Spirula version changed from the audited version {expected_version!r} to {capabilities['tool']['version']!r}", failure_kind="BACKEND_VERSION_CHANGED")
        device = model_cfg.get("device")
        if not isinstance(device, str) or not device.strip() or device != policy.get("required_device"):
            raise BackendError("Smoke project must explicitly name its authorized Apple GPU device", failure_kind="INVALID_CONFIGURATION")
        if "--device" not in capabilities["training"]["arguments"]:
            raise BackendError("Installed Spirula does not expose --device; explicit GPU selection is unavailable", failure_kind="BACKEND_UNAVAILABLE")
        if model_cfg.get("steps_per_save") != 100:
            raise BackendError("The smoke run requires a 100-step save interval", failure_kind="INVALID_CONFIGURATION")
        dataset = prepare_training_dataset(attempt, image_dir, component_dir, copy_inputs=True)
        copied_inventory = image_inventory(dataset / "images")
        expected_names = {row["image_id"] for row in component["images"]}
        if {row["relative_path"] for row in copied_inventory["images"]} != expected_names:
            raise BackendError("The copied training dataset image set changed during preparation", failure_kind="INVALID_INPUT")
        run_name = f"{config['project_id']}-diagnostic-{execution_id:04d}"
        command = build_training_command(Path(capabilities["tool"]["executable"]), dataset, attempt,
            {**model_cfg, "data_format": "colmap", "image_dir": "images", "colmap_recon_dir": "sparse/0"},
            capabilities, run_name)
        checkpoint_policy = {"steps_per_save": model_cfg.get("steps_per_save", 100),
                             "save_only_latest_checkpoint": model_cfg.get("save_only_latest_checkpoint", True),
                             "save_full_checkpoint": model_cfg.get("save_full_checkpoint", True),
                             "retention_owner": "SPIRULA_BACKEND"}
        attempt_manifest = {
            "schema_version": "exhibition-archive.smoke-training-input-manifest.v1",
            "policy": "SMOKE_TEST_ONLY", "project_id": config["project_id"],
            "component_id": policy["component_id"], "component_manifest": component["manifest_path"],
            "component_manifest_sha256": component["manifest_sha256"], "component_copy_sha256": component["component_copy_sha256"],
            "component_quality_gate": "SFM_NEEDS_REVIEW", "registered_image_count": component["registered_image_count"],
            "camera_count": component["camera_count"], "sparse_point_count": component["sparse_point_count"],
            "source_image_directory": str(image_dir.resolve()), "training_dataset_directory": str(dataset.resolve()),
            "images": [{**row, "source_heic_path": expected["source_path"], "source_heic_sha256": expected["source_sha256"],
                        "derivative_sha256": row["sha256"], "dimensions": expected["dimensions"]}
                       for row, expected in ((x, component["manifest"]["images_by_id"][x["image_id"]]) for x in copied_inventory["images"])],
            "source_images_modified": False, "component_coordinate_frame": "One preserved COLMAP component only; arbitrary non-metric units.",
        }
        manifest_out = attempt / "training-input-manifest.json"
        write_json(manifest_out, attempt_manifest)
        readiness = {
            "schema_version": "phase2ez.training-readiness.v1", "policy": "SMOKE_TEST_ONLY",
            "status": "READY_TO_LAUNCH", "operator_gpu_authorization": "RECORDED_AND_EXPLICITLY_CONFIRMED",
            "production_sfm_status": "SFM_NEEDS_REVIEW", "production_quality_thresholds_changed": False,
            "component_validation": component, "selected_image_count": len(copied_inventory["images"]),
            "image_set_exactly_matches_component": True, "dataset_copy_integrity": "VALIDATED",
            "trainer": {"executable": capabilities["tool"]["executable"], "version": capabilities["tool"]["version"],
                        "preset": model_cfg.get("preset"), "iterations": model_cfg["iterations"],
                        "hard_iteration_cap": MAX_SMOKE_ITERATIONS, "resolution_divisor": model_cfg.get("resolution_divisor"),
                        "steps_per_save": model_cfg["steps_per_save"], "device": device, "command": command},
            "output_directory": str(attempt.resolve()), "resource_check": resources,
            "dataset_format": "colmap", "images_subdirectory": "images", "component_subdirectory": "sparse/0",
            "known_quality_limitations": ["19 of the original 40 images registered in sparse/0.",
                "Two disconnected components were present in Phase 2EY; only sparse/0 is used.",
                "Per-component reprojection error is unknown; COLMAP point-error values were all zero/uninformative.",
                "The original full-dataset run-level reprojection mean was 2.845 px.",
                "This smoke run cannot establish scene fidelity or production archive suitability."],
            "non_publication": True, "public_model_approval": "PROHIBITED_FOR_SMOKE_TEST_MODEL",
        }
        readiness_path = root.parent / "component-audit" / "training-readiness.json"
        write_json(readiness_path, readiness)
        private_environment_path = attempt / "environment-overrides.private.json"
        environment_overrides = model_cfg.get("environment_overrides", {})
        if not isinstance(environment_overrides, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in environment_overrides.items()):
            raise BackendError("model.environment_overrides must be a string map", failure_kind="INVALID_CONFIGURATION")
        write_json(private_environment_path, environment_overrides)
        try: private_environment_path.chmod(0o600)
        except OSError: pass
        command_record = {
            "schema_version": "exhibition-archive.backend-attempt.v2", "stage": SMOKE_STAGE,
            "attempt_id": attempt.name, "engine_execution_id": execution_id,
            "policy": "SMOKE_TEST_ONLY", "component_id": policy["component_id"],
            "production_quality_gate": "SFM_NEEDS_REVIEW", "backend_version": capabilities["tool"]["version"],
            "configuration": {k: v for k, v in model_cfg.items() if k != "environment_overrides"},
            "command": command, "cwd": str(attempt.resolve()), "resource_check": resources,
            "checkpoint_policy": checkpoint_policy,
            "input_manifest": str(manifest_out.resolve()), "readiness_report": str(readiness_path.resolve()),
            "expected_output_prefix": str((attempt / "outputs" / run_name).resolve()),
            "checkpoint_resume_capability": capabilities["training"]["checkpoint"],
            "started_at_utc": utc_now(), "artifact_discovery_status": "NOT_STARTED", "validation_result": {"status": "NOT_RUN"},
            "authorization": {"gpu_training_authorized_by_operator": True, "publication": False},
        }
        write_json(attempt / "command.json", command_record)
        execution = execute_process(state, execution_id, SMOKE_STAGE, command, attempt, attempt / "spirula.log",
            environment_overrides, model_cfg.get("timeout_seconds", resource_policy.get("training_timeout_seconds")),
            float(resource_policy.get("poll_interval_seconds", 1.0)), checkpoint_root=attempt / "outputs",
            minimum_free_disk_bytes=resources["minimum_required_bytes"])
        command_record = read_json(attempt / "command.json")
        gpu = _gpu_initialization_evidence(attempt / "spirula.log", device)
        model, candidate_rows, selection_reason = _discover_smoke_model(attempt, model_cfg)
        model_validation = validate_gaussian_ply(model)
        model_validation.update({
            "status": "PLY_SCHEMA_VALID", "model_status": "SMOKE_TEST_MODEL", "producer": "Spirula",
            "producer_version": capabilities["tool"]["version"], "attempt_id": attempt.name,
            "component_id": policy["component_id"], "component_sha256": component["component_copy_sha256"],
            "training_input_manifest_sha256": sha256_file(manifest_out), "gpu": gpu,
            "training_configuration": {k: v for k, v in model_cfg.items() if k != "environment_overrides"},
            "public_selection_allowed": False, "publication": "PROHIBITED",
        })
        validation_path = attempt / "model-validation.json"
        write_json(validation_path, model_validation)
        discovered = []
        for path in sorted(p for p in (attempt / "outputs").rglob("*") if p.is_file() and not p.is_symlink()):
            discovered.append({"relative_path": path.relative_to(attempt).as_posix(), "size_bytes": path.stat().st_size,
                               "sha256": model_validation["sha256"] if path == model else None,
                               "role": "MODEL_PLY" if path == model else ("CHECKPOINT_OR_STATE" if path.suffix.casefold() in {".ckpt", ".pt", ".pth", ".safetensors"} else "OTHER_OUTPUT")})
        output_discovery = {"status": "FOUND_VALIDATED", "selected_ply": str(model.resolve()),
            "candidate_count": len(candidate_rows), "candidates": candidate_rows, "selection_reason": selection_reason,
            "files": discovered, "checkpoint_candidates": discover_checkpoints(attempt / "outputs"),
            "sha256": model_validation["sha256"], "file_bytes": model_validation["file_bytes"], "gaussian_count": model_validation["gaussian_count"]}
        output_discovery_path = attempt / "output-discovery.json"
        write_json(output_discovery_path, output_discovery)
        if not gpu["initialized"]:
            finalize_attempt_record(attempt, "VALIDATION_FAILED", exit_code=execution["exit_code"],
                error_classification="GPU_DEVICE_NOT_VERIFIED", error="Training output exists, but the requested GPU device was not confirmed in the Spirula log.",
                pid=execution["pid"], log_path=execution["log_path"], elapsed_seconds=execution["elapsed_seconds"],
                artifact_discovery_status="FOUND_VALIDATED", validation_result={"status": "PLY_SCHEMA_VALID", "gpu_status": gpu["status"]})
            raise BackendError("Spirula produced a PLY, but GPU initialization for the requested device was not confirmed; stopping before viewer validation.",
                               failure_kind="GPU_DEVICE_NOT_VERIFIED", details={"model": model_validation, "gpu": gpu})
        command_record.update({"model_path": str(model.resolve()), "model_sha256": model_validation["sha256"],
            "gaussian_count": model_validation["gaussian_count"], "model_bytes": model_validation["file_bytes"],
            "gpu_initialization": gpu, "checkpoint_candidates": output_discovery["checkpoint_candidates"]})
        write_json(attempt / "command.json", command_record)
        finalize_attempt_record(attempt, "SUCCEEDED", exit_code=execution["exit_code"], pid=execution["pid"],
            log_path=execution["log_path"], elapsed_seconds=execution["elapsed_seconds"], artifact_discovery_status="FOUND_VALIDATED",
            validation_result={"status": "PLY_SCHEMA_VALID", "model_path": str(model.resolve()),
                "model_sha256": model_validation["sha256"], "gaussian_count": model_validation["gaussian_count"], "gpu": gpu},
            recovery_guidance="Technical model artifact validated. Keep it isolated; production SfM, model approval, navigation, and publication remain separate gates.")
        execution_record = {
            "schema_version": "phase2ez.training-execution.v1", "status": "VERIFIED_BY_EXECUTION",
            "policy": "SMOKE_TEST_ONLY", "attempt_id": attempt.name, "engine_run_id": run_id,
            "engine_execution_id": execution_id, "backend": "Spirula", "version": capabilities["tool"]["version"],
            "command": command, "process_id": execution["pid"], "exit_code": execution["exit_code"],
            "elapsed_seconds": execution["elapsed_seconds"], "log_path": str((attempt / "spirula.log").resolve()),
            "gpu": gpu, "training_configuration": readiness["trainer"], "resource_check": resources,
            "component": {"id": policy["component_id"], "sha256": component["component_copy_sha256"],
                          "image_count": component["registered_image_count"], "point_count": component["sparse_point_count"]},
            "model": model_validation, "output_discovery": str(output_discovery_path.resolve()),
            "training_input_manifest": str(manifest_out.resolve()), "checkpoint_resume_execution_status": "NOT_TESTED",
            "public_selection_allowed": False, "publication": "PROHIBITED",
        }
        phase_training = root.parent / "training"
        phase_training.mkdir(parents=True, exist_ok=True)
        execution_record_path = phase_training / "execution-record.json"
        write_json(execution_record_path, execution_record)
        write_json(phase_training / "output-discovery.json", output_discovery)
        write_json(phase_training / "model-validation.json", model_validation)
        artifacts = [manifest_out, output_discovery_path, validation_path, attempt / "command.json", attempt / "spirula.log", model]
        for artifact in artifacts:
            state.add_artifact(SMOKE_STAGE, artifact, "3dgs-ply" if artifact.suffix.casefold() == ".ply" else "diagnostic-evidence",
                               sha256_file(artifact), artifact.stat().st_size,
                               {"policy": "SMOKE_TEST_ONLY", "publication": False, "component_id": policy["component_id"]})
        digest = hashlib.sha256(json.dumps([{"path": str(p), "sha256": sha256_file(p)} for p in artifacts], sort_keys=True).encode()).hexdigest()
        details = {"status": "SMOKE_TRAINING", "model_status": "SMOKE_TEST_MODEL", "public_selection_allowed": False,
                   "model_path": str(model.resolve()), "model_sha256": model_validation["sha256"],
                   "gaussian_count": model_validation["gaussian_count"], "file_bytes": model_validation["file_bytes"],
                   "attempt_directory": str(attempt.resolve()), "gpu": gpu, "production_sfm_quality_gate": "SFM_NEEDS_REVIEW"}
        result = {"run_id": run_id, "execution_id": execution_id, "status": "SMOKE_TRAINING", "details": details,
                  "artifacts": [str(p.resolve()) for p in artifacts], "execution_record": str(execution_record_path.resolve())}
        state.finish_stage(execution_id, "SMOKE_TRAINING", digest, result)
        state.set_stage(SMOKE_STAGE, "SMOKE_TRAINING", "SMOKE_TEST_ONLY", digest, execution_id, details)
        state.end_run(run_id, "SMOKE_TRAINING_COMPLETED", result)
        return result
    except Exception as exc:
        failure_kind = getattr(exc, "failure_kind", "UNEXPECTED_EXCEPTION")
        details = {"failure_kind": failure_kind, "error": str(exc), "failure_details": getattr(exc, "details", {}),
                   "policy": "SMOKE_TEST_ONLY", "publication_allowed": False}
        if attempt is not None and (attempt / "command.json").is_file():
            record = read_json(attempt / "command.json")
            if record.get("terminal_state") not in {"NONZERO_EXIT", "CANCELLED", "TIMED_OUT", "PROCESS_LAUNCH_FAILED", "INTERRUPTED", "UNEXPECTED_EXCEPTION", "VALIDATION_FAILED"}:
                finalize_attempt_record(attempt, "VALIDATION_FAILED", exit_code=record.get("exit_code"),
                    error_classification=failure_kind, error=str(exc), pid=record.get("pid"), log_path=record.get("log_path") or attempt / "spirula.log",
                    elapsed_seconds=record.get("elapsed_seconds"),
                    artifact_discovery_status="MISSING" if failure_kind == "MODEL_OUTPUT_MISSING" else "NOT_VALIDATED",
                    validation_result={"status": "FAILED", "failure_kind": failure_kind, "details": details})
        state.finish_stage(execution_id, "FAILED", None, details, str(exc))
        state.set_stage(SMOKE_STAGE, "FAILED", "SMOKE_TEST_ONLY", None, execution_id, details)
        state.end_run(run_id, "FAILED", {"run_id": run_id, "execution_id": execution_id, **details})
        if attempt is not None:
            phase_training = root.parent / "training"
            phase_training.mkdir(parents=True, exist_ok=True)
            write_json(phase_training / "execution-record.json", {"schema_version": "phase2ez.training-execution.v1", "status": "FAILED", **details,
                "attempt_id": attempt.name, "engine_run_id": run_id, "engine_execution_id": execution_id,
                "attempt_directory": str(attempt.resolve()), "command_record": str((attempt / "command.json").resolve()),
                "log_path": str((attempt / "spirula.log").resolve())})
        raise


def reconcile_smoke_attempt(root: Path, config: dict, state: ProjectState, attempt_id: str) -> dict:
    """Revalidate a completed smoke attempt after a corrected log parser, without launching training."""
    root = root.resolve()
    policy, model_cfg, manifest_path, component_dir, image_dir = _validate_smoke_policy(root, config, operator_authorized=True)
    attempts_root = (output_path(root, config) / "backend-runs" / "smoke-training").resolve()
    attempt = (attempts_root / attempt_id).resolve()
    if attempts_root not in attempt.parents or not attempt.is_dir():
        raise BackendError("The requested smoke attempt must exist beneath this diagnostic project's attempt directory", failure_kind="UNSAFE_OUTPUT")

    command_path = attempt / "command.json"
    command_record = read_json(command_path)
    if command_record.get("terminal_state") != "VALIDATION_FAILED" or command_record.get("error_classification") != "GPU_DEVICE_NOT_VERIFIED":
        raise BackendError("Only the known GPU-log-format false negative can be reconciled by this command", failure_kind="INVALID_STATE")
    if command_record.get("process_terminal_state") != "SUCCEEDED" or command_record.get("exit_code") != 0:
        raise BackendError("The recorded trainer process did not exit successfully; validation-only reconciliation is not applicable", failure_kind="INVALID_STATE")

    component = _validate_component_inputs(root, policy, manifest_path, component_dir, image_dir)
    input_manifest_path = attempt / "training-input-manifest.json"
    input_manifest = read_json(input_manifest_path)
    if input_manifest.get("component_id") != policy["component_id"] or input_manifest.get("component_copy_sha256") != component["component_copy_sha256"]:
        raise BackendError("The completed attempt's component provenance does not match the isolated project", failure_kind="INVALID_INPUT")
    dataset = attempt / "dataset"
    dataset_component_digest, dataset_component_rows = _digest_tree(dataset / "sparse" / "0")
    if dataset_component_digest != component["component_copy_sha256"]:
        raise BackendError("The completed attempt's copied COLMAP input changed after training", failure_kind="INVALID_INPUT")
    actual_images = {row["relative_path"]: row for row in image_inventory(dataset / "images")["images"]}
    expected_images = {row["image_id"]: row for row in input_manifest.get("images", [])}
    if set(actual_images) != set(expected_images) or any(actual_images[name]["sha256"] != expected_images[name]["derivative_sha256"] for name in expected_images):
        raise BackendError("The completed attempt's copied training images changed after training", failure_kind="INVALID_INPUT")

    command = command_record.get("command")
    if not isinstance(command, list) or len(command) < 3 or command[1:3] != ["train", "3dgs"]:
        raise BackendError("The recorded process command is not the expected fresh Spirula 3DGS training invocation", failure_kind="INVALID_INPUT")
    values = {flag: command[command.index(flag) + 1] for flag in ("--device", "--num-iterations") if flag in command and command.index(flag) + 1 < len(command)}
    if values.get("--device") != model_cfg.get("device") or values.get("--device") != policy.get("required_device"):
        raise BackendError("The recorded training command did not request the configured diagnostic GPU", failure_kind="INVALID_INPUT")
    requested_iterations = int(model_cfg.get("iterations", 0))
    if requested_iterations < 1 or requested_iterations > MAX_SMOKE_ITERATIONS or values.get("--num-iterations") != str(requested_iterations):
        raise BackendError("The recorded training command does not match the bounded smoke iteration configuration", failure_kind="RESOURCE_LIMIT")

    gpu = _gpu_initialization_evidence(attempt / "spirula.log", values["--device"])
    if not gpu["initialized"]:
        raise BackendError("The selected GPU still cannot be confirmed in the completed attempt log", failure_kind="GPU_DEVICE_NOT_VERIFIED", details=gpu)
    model, candidate_rows, selection_reason = _discover_smoke_model(attempt, model_cfg)
    model_validation = validate_gaussian_ply(model)
    model_validation.update({
        "status": "PLY_SCHEMA_VALID", "model_status": "SMOKE_TEST_MODEL", "producer": "Spirula",
        "producer_version": command_record.get("backend_version"), "attempt_id": attempt.name,
        "component_id": policy["component_id"], "component_sha256": component["component_copy_sha256"],
        "training_input_manifest_sha256": sha256_file(input_manifest_path), "gpu": gpu,
        "training_configuration": {k: v for k, v in model_cfg.items() if k != "environment_overrides"},
        "public_selection_allowed": False, "publication": "PROHIBITED",
    })
    validation_path = attempt / "model-validation.json"
    output_discovery_path = attempt / "output-discovery.json"
    output_discovery = read_json(output_discovery_path)
    if output_discovery.get("selected_ply") != str(model.resolve()):
        raise BackendError("The selected PLY differs from the previously discovered output; refusing silent reselection", failure_kind="MODEL_OUTPUT_AMBIGUOUS")
    if output_discovery.get("sha256") != model_validation["sha256"] or output_discovery.get("file_bytes") != model_validation["file_bytes"]:
        raise BackendError("The discovered PLY identity changed after the original run", failure_kind="INVALID_OUTPUT")

    training_root = root.parent / "training"
    training_root.mkdir(parents=True, exist_ok=True)
    execution_record_path = training_root / "execution-record.json"
    training_validation_path = training_root / "model-validation.json"
    training_discovery_path = training_root / "output-discovery.json"
    reconciliation_path = attempt / "validation-reconciliation.json"
    if reconciliation_path.exists():
        raise BackendError("This completed attempt already has a validation reconciliation record", failure_kind="INVALID_STATE")
    initial_model_validation_path = attempt / "model-validation.initial.json"
    initial_command_path = attempt / "command.initial.json"
    initial_model_validation = read_json(validation_path)
    original_command_bytes = command_path.read_bytes()
    original_model_bytes = validation_path.read_bytes()
    initial_command_path.write_bytes(original_command_bytes)
    initial_model_validation_path.write_bytes(original_model_bytes)
    reconciliation = {
        "schema_version": "phase2ez.smoke-validation-reconciliation.v1",
        "status": "VERIFIED_BY_EXECUTION",
        "reason": "The initial parser did not recognize Spirula's selected-device row format; the completed run is revalidated without retraining.",
        "previous_terminal_state": command_record.get("terminal_state"),
        "previous_error_classification": command_record.get("error_classification"),
        "initial_process_terminal_state": command_record.get("process_terminal_state"),
        "initial_exit_code": command_record.get("exit_code"),
        "initial_validation": initial_model_validation.get("gpu"),
        "initial_command_record": {"path": str(initial_command_path.resolve()), "sha256": hashlib.sha256(original_command_bytes).hexdigest()},
        "initial_model_validation_record": {"path": str(initial_model_validation_path.resolve()), "sha256": hashlib.sha256(original_model_bytes).hexdigest()},
        "log": {"path": str((attempt / "spirula.log").resolve()), "sha256": sha256_file(attempt / "spirula.log"),
                "device_evidence": gpu["evidence_lines"], "evidence_method": gpu["evidence_method"]},
        "training_process": {"pid": command_record.get("pid"), "exit_code": command_record.get("exit_code"),
                             "process_terminal_state": command_record.get("process_terminal_state"),
                             "completed_steps": requested_iterations},
        "model": {"path": str(model.resolve()), "sha256": model_validation["sha256"],
                  "file_bytes": model_validation["file_bytes"], "gaussian_count": model_validation["gaussian_count"]},
        "reconciled_at_utc": utc_now(), "training_relaunched": False,
    }
    write_json(reconciliation_path, reconciliation)
    write_json(validation_path, model_validation)
    write_json(training_validation_path, model_validation)
    command_record.update({
        "terminal_state": "SUCCEEDED", "error_classification": None, "error": None,
        "artifact_discovery_status": "FOUND_VALIDATED", "validation_result": {
            "status": "PLY_SCHEMA_VALID", "model_path": str(model.resolve()), "model_sha256": model_validation["sha256"],
            "gaussian_count": model_validation["gaussian_count"], "gpu_status": gpu["status"]},
        "gpu_initialization": gpu, "model_path": str(model.resolve()), "model_sha256": model_validation["sha256"],
        "gaussian_count": model_validation["gaussian_count"], "model_bytes": model_validation["file_bytes"],
        "validation_reconciliation": str(reconciliation_path.resolve()),
        "validation_reconciled_at_utc": reconciliation["reconciled_at_utc"],
        "recovery_guidance": "Training completed and the selected GPU and PLY were verified after correcting log-format recognition. Keep this model isolated; production approval and publication remain prohibited.",
    })
    write_json(command_path, command_record)
    output_discovery.update({"status": "FOUND_VALIDATED", "selected_ply": str(model.resolve()),
        "candidate_count": len(candidate_rows), "candidates": candidate_rows, "selection_reason": selection_reason,
        "sha256": model_validation["sha256"], "file_bytes": model_validation["file_bytes"],
        "gaussian_count": model_validation["gaussian_count"]})
    write_json(output_discovery_path, output_discovery)
    write_json(training_discovery_path, output_discovery)

    execution_id = command_record.get("engine_execution_id")
    db_execution = state.execution(int(execution_id)) if isinstance(execution_id, int) else None
    if not db_execution or db_execution["stage_id"] != SMOKE_STAGE or db_execution["status"] != "FAILED":
        raise BackendError("Durable SQLite state is not the expected failed smoke execution; refusing reconciliation", failure_kind="INVALID_STATE")
    run_id = int(db_execution["run_id"])
    run_row = state.db.execute("SELECT * FROM workflow_runs WHERE run_id=?", (run_id,)).fetchone()
    if not run_row or run_row["status"] != "FAILED":
        raise BackendError("Durable SQLite run is not in the expected failed state; refusing reconciliation", failure_kind="INVALID_STATE")

    details = {"status": "SMOKE_TRAINING", "model_status": "SMOKE_TEST_MODEL", "public_selection_allowed": False,
        "model_path": str(model.resolve()), "model_sha256": model_validation["sha256"],
        "gaussian_count": model_validation["gaussian_count"], "file_bytes": model_validation["file_bytes"],
        "attempt_directory": str(attempt.resolve()), "gpu": gpu, "production_sfm_quality_gate": "SFM_NEEDS_REVIEW",
        "validation_reconciliation": str(reconciliation_path.resolve()),
        "initial_engine_failure": {"failure_kind": "GPU_DEVICE_NOT_VERIFIED", "error": "Initial selected-device log parser did not match Spirula 2026.10.16's output format."}}
    artifacts = [input_manifest_path, output_discovery_path, validation_path, training_discovery_path,
                 training_validation_path, command_path,
                 attempt / "spirula.log", model, reconciliation_path, initial_command_path, initial_model_validation_path]
    digest = hashlib.sha256(json.dumps([{"path": str(p), "sha256": sha256_file(p)} for p in artifacts], sort_keys=True).encode()).hexdigest()
    result = {"run_id": run_id, "execution_id": execution_id, "status": "SMOKE_TRAINING", "details": details,
              "artifacts": [str(p.resolve()) for p in artifacts], "execution_record": str(execution_record_path.resolve()),
              "validation_reconciliation": str(reconciliation_path.resolve())}
    execution_record = {
        "schema_version": "phase2ez.training-execution.v1", "status": "VERIFIED_BY_EXECUTION",
        "policy": "SMOKE_TEST_ONLY", "attempt_id": attempt.name, "engine_run_id": run_id,
        "engine_execution_id": execution_id, "backend": "Spirula", "version": command_record.get("backend_version"),
        "command": command, "process_id": command_record.get("pid"), "exit_code": command_record.get("exit_code"),
        "elapsed_seconds": command_record.get("elapsed_seconds"), "log_path": str((attempt / "spirula.log").resolve()),
        "gpu": gpu, "training_configuration": {k: v for k, v in model_cfg.items() if k != "environment_overrides"},
        "component": {"id": policy["component_id"], "sha256": component["component_copy_sha256"],
                      "image_count": component["registered_image_count"], "point_count": component["sparse_point_count"]},
        "model": model_validation, "output_discovery": str(output_discovery_path.resolve()),
        "training_input_manifest": str(input_manifest_path.resolve()), "checkpoint_resume_execution_status": "NOT_TESTED",
        "validation_reconciliation": str(reconciliation_path.resolve()), "public_selection_allowed": False,
        "publication": "PROHIBITED", "training_relaunched": False,
    }
    write_json(execution_record_path, execution_record)

    prior_run_summary = json.loads(run_row["summary_json"] or "{}")
    prior_execution_details = json.loads(db_execution["details_json"] or "{}")
    reconciliation["initial_database_state"] = {
        "workflow_run_status": run_row["status"], "workflow_run_summary": prior_run_summary,
        "stage_execution_status": db_execution["status"], "stage_execution_details": prior_execution_details,
        "stage_execution_error": db_execution["error"],
    }
    write_json(reconciliation_path, reconciliation)
    # Include the final reconciliation file bytes in both the artifact digest and database provenance.
    digest = hashlib.sha256(json.dumps([{"path": str(p), "sha256": sha256_file(p)} for p in artifacts], sort_keys=True).encode()).hexdigest()
    state.db.execute("BEGIN IMMEDIATE")
    try:
        state.db.execute("UPDATE stage_executions SET status=?,output_digest=?,details_json=?,error=NULL WHERE execution_id=? AND status='FAILED'",
            ("SMOKE_TRAINING", digest, json.dumps(details, sort_keys=True), execution_id))
        if state.db.execute("SELECT changes()").fetchone()[0] != 1:
            raise BackendError("SQLite smoke execution changed during reconciliation", failure_kind="INVALID_STATE")
        state.db.execute("UPDATE stage_states SET status=?,output_digest=?,updated_at=?,details_json=? WHERE stage_id=? AND last_execution_id=?",
            ("SMOKE_TRAINING", digest, utc_now(), json.dumps(details, sort_keys=True), SMOKE_STAGE, execution_id))
        summary = {"run_id": run_id, "execution_id": execution_id, "status": "SMOKE_TRAINING", "details": details,
                   "initial_failure": prior_run_summary, "validation_reconciliation": str(reconciliation_path.resolve())}
        state.db.execute("UPDATE workflow_runs SET status=?,summary_json=? WHERE run_id=? AND status='FAILED'",
            ("SMOKE_TRAINING_COMPLETED", json.dumps(summary, sort_keys=True), run_id))
        if state.db.execute("SELECT changes()").fetchone()[0] != 1:
            raise BackendError("SQLite smoke run changed during reconciliation", failure_kind="INVALID_STATE")
        for artifact in artifacts:
            is_model = artifact == model
            state.db.execute("""INSERT INTO artifacts(stage_id,path,artifact_type,sha256,size_bytes,status,provenance_json)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(stage_id,path) DO UPDATE SET artifact_type=excluded.artifact_type,
                sha256=excluded.sha256,size_bytes=excluded.size_bytes,status=excluded.status,provenance_json=excluded.provenance_json""",
                (SMOKE_STAGE, str(artifact.resolve()), "3dgs-ply" if is_model else "diagnostic-evidence",
                 sha256_file(artifact), artifact.stat().st_size, "VALIDATED",
                 json.dumps({"policy": "SMOKE_TEST_ONLY", "publication": False, "component_id": policy["component_id"],
                             "validation_reconciliation": str(reconciliation_path.resolve())}, sort_keys=True)))
        state.db.commit()
    except Exception:
        state.db.rollback()
        raise
    return result
