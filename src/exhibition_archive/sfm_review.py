from __future__ import annotations

import json
import uuid
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

from .adapters import sha256_file
from .backends import BackendError, image_inventory, validate_colmap_directory
from .dataset_prep import effective_sfm_dataset
from .state import utc_now


GATE_PREFIX = "sfm-component-acceptance:"
ACCEPT_DECISION = "ACCEPT_EXPERIMENTAL_TRAINING"
TRAINING_PURPOSE = "EXPERIMENTAL_3DGS_TRAINING_ONLY"
FORBIDDEN_PURPOSES = [
    "FINAL_MODEL_QUALITY_APPROVAL",
    "NAVIGATION_APPROVAL",
    "ARTWORK_COMPLETENESS_APPROVAL",
    "ARCHIVE_PACKAGING_APPROVAL",
    "PUBLICATION_APPROVAL",
]


def _json_digest(value: Any) -> str:
    import hashlib

    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _read_details(row: dict | None) -> dict:
    try:
        details = json.loads((row or {}).get("details_json") or "{}")
        return details if isinstance(details, dict) else {}
    except (TypeError, ValueError):
        return {}


def _sfm_component_root(root: Path, config: dict, stage_details: dict) -> Path:
    raw = stage_details.get("reconstruction_directory")
    if not raw:
        raw = config.get("integrations", {}).get("sfm", {}).get("directory")
    if not raw:
        raw = config.get("integrations", {}).get("sfm", {}).get("directory")
    if not raw:
        raise BackendError("SfM has no configured or recorded reconstruction directory", failure_kind="SFM_INVALID")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    if candidate.is_symlink():
        raise BackendError(f"SfM reconstruction directory is a symlink: {candidate}", failure_kind="SFM_INVALID")
    candidate = candidate.resolve()
    if not candidate.is_dir():
        raise BackendError(f"SfM reconstruction directory is missing: {candidate}", failure_kind="SFM_INVALID")

    if candidate.name.isdigit() and candidate.parent.is_dir():
        numeric_children = [path for path in candidate.parent.iterdir()
                            if path.is_dir() and path.name.isdigit()]
        if any(path.is_symlink() for path in numeric_children):
            raise BackendError("COLMAP component directories may not be symlinks", failure_kind="SFM_INVALID")
        if candidate in numeric_children:
            return candidate.parent
    numeric_children = [path for path in candidate.iterdir() if path.is_dir() and path.name.isdigit()]
    if any(path.is_symlink() for path in numeric_children):
        raise BackendError("COLMAP component directories may not be symlinks", failure_kind="SFM_INVALID")
    if numeric_children:
        return candidate
    return candidate


def _stage_artifact_identity(root: Path, config: dict, state, stage: dict) -> dict:
    execution_id = stage.get("last_execution_id")
    execution = state.execution(int(execution_id)) if execution_id is not None else None
    if not execution:
        raise BackendError("SfM stage has no durable execution record", failure_kind="SFM_INVALID")
    try:
        execution_details = json.loads(execution.get("details_json") or "{}")
    except (TypeError, ValueError):
        execution_details = {}
    if not isinstance(execution_details, dict):
        execution_details = {}
    raw_paths = execution_details.get("artifacts", [])
    if not isinstance(raw_paths, list) or not raw_paths:
        raise BackendError("SfM execution has no recorded artifact list", failure_kind="SFM_INVALID")
    report_artifacts = []
    report_findings = {}
    for raw in raw_paths:
        recorded_path = Path(raw)
        if recorded_path.is_symlink():
            raise BackendError(f"Recorded SfM stage artifact is a symlink: {recorded_path}", failure_kind="SFM_INVALID")
        path = recorded_path.resolve()
        if not path.is_file():
            raise BackendError(f"Recorded SfM stage artifact is missing: {path}", failure_kind="SFM_INVALID")
        if path.name in {"sfm-validation.json", "sfm-summary.json"}:
            shown_path = path.relative_to(root).as_posix() if root in path.parents else str(path)
            digest = sha256_file(path)
            stored = state.db.execute("SELECT sha256 FROM artifacts WHERE stage_id='sfm' AND path=?", (str(path),)).fetchone()
            if stored and stored["sha256"] != digest:
                raise BackendError(f"Recorded SfM report artifact changed after validation: {path}", failure_kind="SFM_ARTIFACT_MISMATCH")
            report_artifacts.append({"path": shown_path, "sha256": digest})
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise BackendError(f"Recorded SfM report is unreadable: {path}", failure_kind="SFM_ARTIFACT_MISMATCH") from exc
            if not isinstance(report, dict):
                raise BackendError(f"Recorded SfM report does not contain an object: {path}", failure_kind="SFM_ARTIFACT_MISMATCH")
            report_findings[path.name] = {
                "sha256": digest,
                "quality_gate": report.get("quality_gate"),
                "warnings": report.get("warnings"),
                "thresholds": report.get("thresholds"),
            }
    if not stage.get("output_digest") or stage.get("output_digest") != execution.get("output_digest"):
        raise BackendError("SfM stage and execution output identities disagree", failure_kind="SFM_ARTIFACT_MISMATCH")
    plan = state.db.execute("SELECT plan_id,plan_json FROM execution_authorizations WHERE stage_id='sfm' AND execution_id=? ORDER BY created_at DESC LIMIT 1",
                            (execution_id,)).fetchone()
    try:
        plan_doc = json.loads(plan["plan_json"]) if plan else {}
    except (TypeError, ValueError) as exc:
        raise BackendError("Consumed SfM authorization plan is unreadable", failure_kind="SFM_ARTIFACT_MISMATCH") from exc
    if not isinstance(plan_doc, dict):
        raise BackendError("Consumed SfM authorization plan is not an object", failure_kind="SFM_ARTIFACT_MISMATCH")
    if plan_doc:
        recomputable = dict(plan_doc)
        saved_plan_id = recomputable.pop("plan_id", None)
        if saved_plan_id != plan["plan_id"] or _json_digest(recomputable) != saved_plan_id:
            raise BackendError("Consumed SfM authorization plan identity failed its stored fingerprint check", failure_kind="SFM_ARTIFACT_MISMATCH")
        current_sfm_cfg = config.get("integrations", {}).get("sfm", {})
        current_sfm_cfg = {key: value for key, value in current_sfm_cfg.items() if key != "environment_overrides"}
        if plan_doc.get("configuration") != current_sfm_cfg:
            raise BackendError("Current SfM configuration differs from the configuration in the consumed SfM plan",
                               failure_kind="SFM_INPUTS_CHANGED")
    return {
        "execution_id": int(execution_id),
        "execution_status": execution.get("status"),
        "execution_plan_id": plan["plan_id"] if plan else None,
        "execution_plan_sha256": _json_digest(plan_doc) if plan_doc else None,
        "stage_input_fingerprint": stage.get("input_fingerprint"),
        "stage_output_digest": stage["output_digest"],
        "verified_stage_report_artifacts": report_artifacts,
        "verified_stage_report_findings": report_findings,
    }


def _directory_tree(path: Path, state) -> list[dict[str, Any]]:
    if path.is_symlink():
        raise BackendError(f"COLMAP component contains a symlink: {path}", failure_kind="SFM_INVALID")
    rows = []
    for item in sorted(path.rglob("*")):
        if item.is_symlink():
            raise BackendError(f"COLMAP component contains a symlink: {item}", failure_kind="SFM_INVALID")
        if item.is_file():
            digest = sha256_file(item)
            stored = state.db.execute("SELECT sha256 FROM artifacts WHERE stage_id='sfm' AND path=?", (str(item.resolve()),)).fetchone()
            if stored and stored["sha256"] != digest:
                raise BackendError(f"COLMAP component artifact changed after SfM validation: {item}", failure_kind="SFM_ARTIFACT_MISMATCH")
            rows.append({"relative_path": item.relative_to(path).as_posix(),
                         "size_bytes": item.stat().st_size, "sha256": digest})
    return rows


def build_sfm_snapshot(root: Path, config: dict, state) -> dict[str, Any]:
    """Force-verify the current SfM stage, all component files, and image membership."""
    from .workflow import stage_fingerprint

    root = root.resolve()
    stage = state.stage("sfm") or {}
    if stage.get("status") not in {"NEEDS_REVIEW", "COMPLETED", "ADOPTED"}:
        raise BackendError(f"SfM result is not reviewable in its current state: {stage.get('status', 'NOT_STARTED')}",
                           failure_kind="QUALITY_GATE_BLOCKED")
    stage_details = _read_details(stage)
    current_sfm_cfg = config.get("integrations", {}).get("sfm", {})
    recorded_sfm_cfg_sha = stage_details.get("sfm_configuration_sha256")
    if recorded_sfm_cfg_sha and recorded_sfm_cfg_sha != _json_digest(current_sfm_cfg):
        raise BackendError("Current SfM configuration differs from the configuration used to adopt this reconstruction",
                           failure_kind="SFM_INPUTS_CHANGED")
    artifact_identity = _stage_artifact_identity(root, config, state, stage)
    current_stage_input = stage_fingerprint(root, config, state, "sfm", full_hash=True)
    if current_stage_input != stage.get("input_fingerprint"):
        raise BackendError("Current SfM inputs or configuration differ from the recorded SfM execution; review requires a current reconstruction",
                           failure_kind="SFM_INPUTS_CHANGED",
                           details={"recorded": stage.get("input_fingerprint"), "current": current_stage_input})

    image_dir, prep_manifest = effective_sfm_dataset(root, config, validate_files=True)
    inventory = image_inventory(image_dir)
    sfm_cfg = config.get("integrations", {}).get("sfm", {})
    component_root = _sfm_component_root(root, config, stage_details)
    validation = validate_colmap_directory(component_root, inventory["images"], sfm_cfg.get("quality_gate", {}))
    if validation.get("quality_gate") == "SFM_INVALID":
        raise BackendError("Structurally invalid COLMAP output cannot be accepted for training", failure_kind="SFM_INVALID",
                           details=validation)

    report_findings = artifact_identity["verified_stage_report_findings"]
    original_report = report_findings.get("sfm-validation.json") or report_findings.get("sfm-summary.json") or {}
    original_quality = original_report.get("quality_gate") or stage_details.get("quality_gate") or validation["quality_gate"]
    original_warnings = original_report.get("warnings")
    if original_warnings is None:
        original_warnings = stage_details.get("warnings", validation["warnings"])
    original_warnings = list(original_warnings or [])
    original_thresholds = original_report.get("thresholds") or validation["thresholds"]
    for report_name, findings in report_findings.items():
        if report_name == "sfm-validation.json":
            continue
        if findings.get("quality_gate") and findings["quality_gate"] != original_quality:
            raise BackendError("Recorded SfM summary and validation quality gates disagree", failure_kind="SFM_ARTIFACT_MISMATCH")
        if findings.get("warnings") is not None and list(findings["warnings"]) != original_warnings:
            raise BackendError("Recorded SfM summary and validation warnings disagree", failure_kind="SFM_ARTIFACT_MISMATCH")
    if stage_details.get("quality_gate") and stage_details["quality_gate"] != original_quality:
        raise BackendError("SfM stage quality gate differs from its recorded validation report", failure_kind="SFM_ARTIFACT_MISMATCH")
    if stage_details.get("warnings") is not None and list(stage_details["warnings"]) != original_warnings:
        raise BackendError("SfM stage warnings differ from its recorded validation report", failure_kind="SFM_ARTIFACT_MISMATCH")
    if original_quality == "SFM_INVALID":
        raise BackendError("The original SfM validation was structurally invalid", failure_kind="SFM_INVALID")

    source_inventory = [{"relative_path": row["relative_path"], "size_bytes": row["size_bytes"],
                         "sha256": row["sha256"]} for row in inventory["images"]]
    components = []
    root_resolved = component_root.resolve()
    for row in validation["components"]:
        directory = Path(row["directory"]).resolve()
        try:
            component_id = directory.relative_to(root_resolved).as_posix()
        except ValueError as exc:
            raise BackendError("COLMAP component is outside the configured reconstruction directory", failure_kind="SFM_INVALID") from exc
        if component_id == ".":
            component_id = "root"
        member_names = sorted(row.get("registered_image_names", []))
        tree = _directory_tree(directory, state)
        required = {item["relative_path"] for item in tree}
        if not {"cameras.bin", "images.bin", "points3D.bin"}.issubset(required):
            raise BackendError(f"COLMAP component {component_id} is missing a required binary artifact", failure_kind="SFM_INVALID")
        components.append({
            "component_id": component_id,
            "directory": str(directory),
            "artifact_files": tree,
            "artifact_tree_sha256": _json_digest(tree),
            "registered_image_names": member_names,
            "registered_image_count": row["registered_image_count"],
            "registered_image_membership_sha256": _json_digest(member_names),
            "camera_count": row["camera_count"],
            "sparse_point_count": row["sparse_point_count"],
            "mean_track_length": row.get("mean_track_length"),
            "mean_point_reprojection_error_px": row.get("mean_point_reprojection_error_px"),
            "camera_quaternion_norm_warning_image_names": row.get("camera_quaternion_norm_warning_image_names", []),
            "track_reciprocal_reference_mismatches": row.get("track_reciprocal_reference_mismatches", 0),
        })
    components.sort(key=lambda item: item["component_id"])
    source_identity = {"directory": str(image_dir.resolve()), "image_count": inventory["image_count"],
                       "images": source_inventory, "manifest": prep_manifest}
    binding = {
        "project_id": config["project_id"],
        "workspace": str(root),
        "sfm_execution": artifact_identity,
        "sfm_stage_status": stage.get("status"),
        "sfm_configuration_sha256": _json_digest(sfm_cfg),
        "source_image_inventory_sha256": _json_digest(source_identity),
        "source_image_count": inventory["image_count"],
        "reconstruction_directory": str(root_resolved),
        "components": components,
        "original_quality_gate": original_quality,
        "original_quality_warnings": original_warnings,
        "original_quality_thresholds": original_thresholds,
        "current_revalidation_quality_gate": validation["quality_gate"],
        "current_revalidation_warnings": list(validation["warnings"]),
        "current_revalidation_thresholds": validation["thresholds"],
    }
    return {**binding, "quality_gate": original_quality, "quality_warnings": original_warnings,
            "revalidation_quality_gate": validation["quality_gate"],
            "revalidation_warnings": list(validation["warnings"]),
            "validation": validation, "binding_sha256": _json_digest(binding),
            "source_image_directory": str(image_dir.resolve()), "source_inventory": inventory}


def _gate_id(component_id: str) -> str:
    return GATE_PREFIX + quote(component_id, safe="")


def _component(snapshot: dict, component_id: str) -> dict:
    selected = next((row for row in snapshot["components"] if row["component_id"] == component_id), None)
    if selected is None:
        raise BackendError(f"Unknown SfM component {component_id!r}; available components: "
                           f"{[row['component_id'] for row in snapshot['components']]}", failure_kind="INVALID_INPUT")
    return selected


def review_sfm_component(root: Path, config: dict, state, *, component_id: str, decision: str,
                          reviewer: str, rationale: str, limitations: list[str] | None = None) -> dict:
    normalized = decision.strip().casefold().replace("_", "-")
    decision_map = {"accept-experimental-training": (ACCEPT_DECISION, "ACCEPTED_FOR_EXPERIMENTAL_TRAINING"),
                    "accept": (ACCEPT_DECISION, "ACCEPTED_FOR_EXPERIMENTAL_TRAINING"),
                    "reject": ("REJECT", "REJECTED"),
                    "defer": ("DEFER", "NEEDS_REVIEW"),
                    "needs-further-review": ("DEFER", "NEEDS_REVIEW")}
    if normalized not in decision_map:
        raise ValueError("Decision must be accept-experimental-training, reject, or defer")
    decision_value, gate_status = decision_map[normalized]
    reviewer = reviewer.strip()
    rationale = rationale.strip()
    limitations = [str(item).strip() for item in (limitations or []) if str(item).strip()]
    if not reviewer or not rationale:
        raise ValueError("Reviewer and rationale must be non-empty")
    if decision_value == ACCEPT_DECISION and not limitations:
        raise ValueError("Conditional acceptance requires at least one explicit limitation, including an explicit statement if none are currently known")

    snapshot = build_sfm_snapshot(root, config, state)
    component = _component(snapshot, component_id)
    record = {
        "schema_version": "exhibition-archive.sfm-component-review.v1",
        "project_id": config["project_id"],
        "workspace": str(root.resolve()),
        "sfm_execution_id": snapshot["sfm_execution"]["execution_id"],
        "sfm_plan_identity": {
            "execution_plan_id": snapshot["sfm_execution"]["execution_plan_id"],
            "stage_input_fingerprint": snapshot["sfm_execution"]["stage_input_fingerprint"],
            "stage_output_digest": snapshot["sfm_execution"]["stage_output_digest"],
        },
        "selected_component": component,
        "reconstruction_binding_sha256": snapshot["binding_sha256"],
        "reconstruction_artifact_digests": {item["component_id"]: item["artifact_tree_sha256"]
                                             for item in snapshot["components"]},
        "source_image_inventory_sha256": snapshot["source_image_inventory_sha256"],
        "source_image_count": snapshot["source_image_count"],
        "original_quality_gate": snapshot["original_quality_gate"],
        "original_quality_warnings": snapshot["original_quality_warnings"],
        "original_quality_thresholds": snapshot["original_quality_thresholds"],
        "current_revalidation_quality_gate": snapshot["current_revalidation_quality_gate"],
        "current_revalidation_warnings": snapshot["current_revalidation_warnings"],
        "current_revalidation_thresholds": snapshot["current_revalidation_thresholds"],
        "decision": decision_value,
        "reviewer": reviewer,
        "rationale": rationale,
        "unresolved_limitations": limitations,
        "permitted_downstream_purpose": TRAINING_PURPOSE if decision_value == ACCEPT_DECISION else None,
        "forbidden_purposes": FORBIDDEN_PURPOSES,
        "final_archive_completeness_accepted": False,
        "decision_timestamp_utc": utc_now(),
        "audit_identity": "sfmrev_" + uuid.uuid4().hex,
        "binding_sha256": snapshot["binding_sha256"],
    }
    gate_id = _gate_id(component_id)
    review_id = state.record_review_decision(gate_id, gate_status, required=True, reviewer=reviewer,
                                             decision=decision_value, payload=record)
    return {"gate_id": gate_id, "status": gate_status, "decision": record,
            "binding_sha256": snapshot["binding_sha256"],
            "review_history_id": review_id}


def _current_gate_decision(state, snapshot: dict, component_id: str) -> tuple[str, dict | None]:
    gate = state.db.execute("SELECT status,decision_json FROM review_gates WHERE gate_id=?", (_gate_id(component_id),)).fetchone()
    if not gate:
        return "NOT_REVIEWED", None
    try:
        decision = json.loads(gate["decision_json"] or "{}")
    except (TypeError, ValueError):
        return "INVALID_RECORD", None
    if not isinstance(decision, dict):
        return "INVALID_RECORD", None
    status = str(gate["status"])
    expected_decision = {
        "ACCEPTED_FOR_EXPERIMENTAL_TRAINING": ACCEPT_DECISION,
        "REJECTED": "REJECT",
        "NEEDS_REVIEW": "DEFER",
    }.get(status)
    if expected_decision is None or decision.get("decision") != expected_decision:
        return "INVALID_RECORD", decision
    if decision.get("binding_sha256") != snapshot["binding_sha256"]:
        return "STALE", decision
    component = _component(snapshot, component_id)
    selected = decision.get("selected_component")
    reviewer = decision.get("reviewer")
    rationale = decision.get("rationale")
    timestamp = decision.get("decision_timestamp_utc")
    audit_identity = decision.get("audit_identity")
    common_identity_matches = (
        decision.get("schema_version") == "exhibition-archive.sfm-component-review.v1" and
        decision.get("project_id") == snapshot["project_id"] and
        decision.get("workspace") == snapshot["workspace"] and
        decision.get("sfm_execution_id") == snapshot["sfm_execution"]["execution_id"] and
        decision.get("reconstruction_binding_sha256") == snapshot["binding_sha256"] and
        selected == component and isinstance(reviewer, str) and reviewer.strip() and
        isinstance(rationale, str) and rationale.strip() and isinstance(timestamp, str) and timestamp and
        isinstance(audit_identity, str) and audit_identity.startswith("sfmrev_")
    )
    if not common_identity_matches:
        return "INVALID_RECORD", decision
    if status == "ACCEPTED_FOR_EXPERIMENTAL_TRAINING":
        expected_plan_identity = {
            "execution_plan_id": snapshot["sfm_execution"]["execution_plan_id"],
            "stage_input_fingerprint": snapshot["sfm_execution"]["stage_input_fingerprint"],
            "stage_output_digest": snapshot["sfm_execution"]["stage_output_digest"],
        }
        if (decision.get("permitted_downstream_purpose") != TRAINING_PURPOSE or
                decision.get("forbidden_purposes") != FORBIDDEN_PURPOSES or
                decision.get("final_archive_completeness_accepted") is not False or
                not isinstance(decision.get("unresolved_limitations"), list) or
                not decision.get("unresolved_limitations") or
                not all(isinstance(item, str) and item.strip() for item in decision["unresolved_limitations"]) or
                decision.get("sfm_plan_identity") != expected_plan_identity or
                decision.get("reconstruction_artifact_digests") != {
                    item["component_id"]: item["artifact_tree_sha256"] for item in snapshot["components"]} or
                decision.get("source_image_inventory_sha256") != snapshot["source_image_inventory_sha256"] or
                decision.get("original_quality_gate") != snapshot["original_quality_gate"] or
                decision.get("original_quality_warnings") != snapshot["original_quality_warnings"] or
                decision.get("original_quality_thresholds") != snapshot["original_quality_thresholds"] or
                decision.get("selected_component") != component):
            return "INVALID_RECORD", decision
    return status, decision


def component_acceptance_status(root: Path, config: dict, state) -> dict[str, Any]:
    snapshot = build_sfm_snapshot(root, config, state)
    rows = []
    for component in snapshot["components"]:
        status, decision = _current_gate_decision(state, snapshot, component["component_id"])
        rows.append({"component_id": component["component_id"], "status": status,
                     "registered_image_count": component["registered_image_count"],
                     "registered_image_membership_sha256": component["registered_image_membership_sha256"],
                     "decision": decision})
    return {"binding_sha256": snapshot["binding_sha256"], "quality_gate": snapshot["quality_gate"],
            "quality_warnings": snapshot["quality_warnings"],
            "current_revalidation_quality_gate": snapshot["revalidation_quality_gate"],
            "current_revalidation_warnings": snapshot["revalidation_warnings"], "components": rows}


def training_component_scope(root: Path, config: dict, state) -> dict[str, Any]:
    """Resolve the one exact component and image set authorized for fresh training."""
    model_cfg = config.get("integrations", {}).get("model", {})
    if model_cfg.get("mode", "adopt_existing") != "spirula_fresh":
        raise BackendError("Scoped SfM acceptance only authorizes a fresh experimental training candidate",
                           failure_kind="QUALITY_GATE_BLOCKED")
    snapshot = build_sfm_snapshot(root, config, state)
    if snapshot["quality_gate"] == "SFM_INVALID":
        raise BackendError("Structurally invalid SfM output cannot be used for training", failure_kind="QUALITY_GATE_BLOCKED")

    stage = state.stage("sfm") or {}
    sfm_details = _read_details(stage)
    configured_quality = sfm_details.get("quality_gate")
    if configured_quality == "SFM_INVALID":
        raise BackendError("The recorded SfM validation is SFM_INVALID", failure_kind="QUALITY_GATE_BLOCKED")
    quality = snapshot["original_quality_gate"]
    revalidation_quality = snapshot["current_revalidation_quality_gate"]
    allow_warnings = bool(config.get("integrations", {}).get("sfm", {}).get("quality_gate", {}).get("allow_training_with_warnings", False))
    needs_acceptance = (stage.get("status") == "NEEDS_REVIEW" or quality == "SFM_NEEDS_REVIEW" or
                        revalidation_quality == "SFM_NEEDS_REVIEW" or
                        ((quality == "SFM_VALID_WITH_WARNINGS" or revalidation_quality == "SFM_VALID_WITH_WARNINGS")
                         and not allow_warnings))

    acceptance_rows = []
    for item in snapshot["components"]:
        gate_status, decision = _current_gate_decision(state, snapshot, item["component_id"])
        if gate_status == "ACCEPTED_FOR_EXPERIMENTAL_TRAINING":
            acceptance_rows.append((item, decision))

    requested = model_cfg.get("sfm_component_id")
    if requested is not None:
        selected = _component(snapshot, str(requested))
        accepted = next((decision for item, decision in acceptance_rows if item["component_id"] == selected["component_id"]), None)
        if needs_acceptance and accepted is None:
            raise BackendError(f"SfM component {selected['component_id']} has no current experimental-training acceptance",
                               failure_kind="QUALITY_GATE_BLOCKED")
    elif needs_acceptance:
        if not acceptance_rows:
            raise BackendError("SfM remains under review; record a current component-scoped experimental-training acceptance before planning training",
                               failure_kind="QUALITY_GATE_BLOCKED",
                               details={"quality_gate": quality, "warnings": snapshot["quality_warnings"]})
        if len(acceptance_rows) > 1:
            raise BackendError("More than one SfM component is accepted for training; set integrations.model.sfm_component_id explicitly",
                               failure_kind="INVALID_CONFIGURATION",
                               details={"accepted_components": [item["component_id"] for item, _ in acceptance_rows]})
        selected, accepted = acceptance_rows[0]
    else:
        primary_path = Path(snapshot["validation"]["primary_component"]).resolve()
        selected = next(item for item in snapshot["components"] if Path(item["directory"]).resolve() == primary_path)
        accepted = next((decision for item, decision in acceptance_rows if item["component_id"] == selected["component_id"]), None)

    current_decision_status, current_decision = _current_gate_decision(state, snapshot, selected["component_id"])
    if current_decision_status == "REJECTED":
        raise BackendError(f"SfM component {selected['component_id']} was explicitly rejected for downstream training",
                           failure_kind="QUALITY_GATE_BLOCKED", details=current_decision)
    if current_decision_status == "NEEDS_REVIEW":
        raise BackendError(f"SfM component {selected['component_id']} was deferred and still requires review",
                           failure_kind="QUALITY_GATE_BLOCKED", details=current_decision)

    names = selected["registered_image_names"]
    inventory_by_name = {row["relative_path"]: row for row in snapshot["source_inventory"]["images"]}
    missing = sorted(set(names) - set(inventory_by_name))
    if missing:
        raise BackendError("Selected COLMAP component references images missing from the configured source dataset",
                           failure_kind="SFM_INVALID", details={"missing_images": missing[:20]})
    selected_rows = [inventory_by_name[name] for name in names]
    return {
        "component_id": selected["component_id"],
        "component": selected,
        "registered_image_names": list(names),
        "registered_image_rows": selected_rows,
        "registered_image_count": len(names),
        "registered_image_membership_sha256": selected["registered_image_membership_sha256"],
        "source_image_count": snapshot["source_image_count"],
        "source_image_directory": snapshot["source_image_directory"],
        "reconstruction_directory": selected["directory"],
        "sfm_snapshot": snapshot,
        "acceptance": accepted,
        "acceptance_required": needs_acceptance,
        "conditional_scope": stage.get("status") == "NEEDS_REVIEW" or needs_acceptance,
    }


def training_acceptance_for_run(root: Path, config: dict, state) -> dict[str, Any] | None:
    try:
        scope = training_component_scope(root, config, state)
    except BackendError:
        return None
    if scope["conditional_scope"] and scope["acceptance"] is None:
        return None
    return scope
