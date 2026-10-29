from __future__ import annotations

import json
from pathlib import Path

from .adapters import ply_identity, sha256_file
from .backends import BackendError
from .workflow import project_path


def fresh_candidate_record(root: Path, config: dict, state) -> dict:
    stage = state.stage("training") or {}
    if stage.get("status") not in {"COMPLETED", "ADOPTED"}:
        raise BackendError("No completed candidate training artifact is available", failure_kind="MODEL_NOT_READY")
    details = json.loads(stage.get("details_json") or "{}")
    raw = details.get("model_path")
    expected = details.get("model_sha256")
    if not raw or not expected:
        raise BackendError("Training stage has no registered fresh model identity", failure_kind="MODEL_NOT_READY")
    path = Path(raw).resolve()
    if root.resolve() not in path.parents or not path.is_file():
        raise BackendError("Fresh model candidate is missing or outside its project-owned workspace", failure_kind="MODEL_INVALID")
    artifact = state.db.execute("SELECT * FROM artifacts WHERE stage_id='training' AND path=? AND artifact_type='3dgs-ply' AND status='VALIDATED'", (str(path),)).fetchone()
    if not artifact:
        raise BackendError("Fresh model candidate is not registered as a validated training artifact", failure_kind="MODEL_NOT_REGISTERED")
    actual = sha256_file(path)
    if actual != expected or actual != artifact["sha256"]:
        raise BackendError("Fresh model candidate SHA-256 changed after validation", failure_kind="MODEL_IDENTITY_MISMATCH")
    run_type = details.get("run_type")
    validation_path = details.get("validation_path")
    validation = json.loads(Path(validation_path).read_text(encoding="utf-8")) if validation_path and Path(validation_path).is_file() else {}
    identity = ply_identity(path)
    return {"path": str(path), "sha256": actual, "size_bytes": identity["size_bytes"],
            "gaussian_count": identity["gaussian_count"], "run_type": run_type,
            "model_status": details.get("model_status"), "validation_path": validation_path,
            "producer": validation.get("producer", "Spirula"), "producer_version": validation.get("producer_version"),
            "attempt_id": validation.get("attempt_id"), "source_dataset_identity": validation.get("source_dataset_identity"),
            "sfm_input_identity": validation.get("sfm_input_identity"),
            "training_configuration": validation.get("training_configuration", {}),
            "artifact_id": artifact["artifact_id"], "public_selection_allowed": run_type == "candidate"}


def selected_model_record(root: Path, config: dict, state) -> dict:
    model_cfg = config.get("integrations", {}).get("model", {})
    if model_cfg.get("mode", "adopt_existing") == "spirula_fresh":
        candidate = fresh_candidate_record(root, config, state)
        if candidate["run_type"] != "candidate":
            raise BackendError("SMOKE_TEST_MODEL artifacts cannot be consumed by the public viewer/archive publisher", failure_kind="SMOKE_MODEL_BLOCKED")
        gate = state.db.execute("SELECT * FROM review_gates WHERE gate_id='fresh-model-selection'").fetchone()
        if not gate or gate["status"] != "APPROVED":
            raise BackendError("Fresh model publication requires explicit reviewer approval", failure_kind="MODEL_REVIEW_REQUIRED")
        decision = json.loads(gate["decision_json"] or "{}")
        if decision.get("candidate_sha256") != candidate["sha256"] or decision.get("candidate_path") != candidate["path"]:
            raise BackendError("Fresh model approval does not match the current training candidate", failure_kind="MODEL_REVIEW_STALE")
        return {**candidate, "selection": "human-approved fresh candidate", "review": decision}
    path = project_path(root, config, model_cfg["path"])
    identity = ply_identity(path)
    expected = model_cfg.get("expected_sha256")
    if expected and identity["sha256"] != expected:
        raise BackendError("Adopted model SHA-256 no longer matches its configured identity", failure_kind="MODEL_IDENTITY_MISMATCH")
    return {**identity, "path": str(path.resolve()), "selection": "accepted baseline"}
