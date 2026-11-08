from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .adapters import read_json, sha256_file, write_json
from .navigation import approve_navigation_artifact, validate_navigation_artifact
from .state import utc_now


def _context(root: Path, config: dict, state, *, expected_sha256: str | None = None,
             candidate_path: str | Path | None = None) -> tuple[dict, dict, Path, str, str]:
    from .workflow import output_path, stage_fingerprint

    if config.get("integrations", {}).get("navigation", {}).get("mode") != "generate_from_sfm":
        raise ValueError("Browser review is available only for generated navigation proposals")
    stage = state.stage("navigation")
    if not stage:
        raise ValueError("No generated navigation proposal is recorded")
    details = json.loads(stage.get("details_json") or "{}")
    current_fingerprint = stage_fingerprint(root, config, state, "navigation")
    if stage.get("input_fingerprint") != current_fingerprint:
        raise ValueError("Navigation inputs are stale; regenerate the proposal before reviewing it")
    if candidate_path is None:
        if stage.get("status") == "ADOPTED" and details.get("approved_artifact"):
            selected_path = Path(details["approved_artifact"])
        else:
            selected_path = Path(details.get("review_candidate_path") or details.get("proposal_path") or "")
    else:
        selected_path = Path(candidate_path).expanduser().resolve()
    if not selected_path.is_file():
        raise ValueError("Current navigation review artifact is missing")
    candidate = read_json(selected_path)
    if candidate.get("provenance", {}).get("navigation_input_fingerprint") != current_fingerprint:
        raise ValueError("Review artifact has a stale navigation input fingerprint")
    current_sha256 = sha256_file(selected_path)
    if expected_sha256 and candidate_path is None and current_sha256 != expected_sha256:
        raise RuntimeError("Stale browser session: the navigation artifact changed after this page was opened")
    return stage, details, selected_path, current_fingerprint, current_sha256


def preview_navigation_context(root: Path, config: dict, state) -> dict[str, Any]:
    """Resolve navigation for a private preview without weakening review authority.

    Generated proposals remain unbounded preview inputs until the current
    review gate, draft, approved copy, and navigation fingerprint all agree.
    The generation-time ``details.status`` is intentionally ignored after
    adoption: it records how the proposal was generated, not its current
    review authority.
    """
    from .workflow import output_path, stage_fingerprint

    navigation = config.get("integrations", {}).get("navigation", {})
    if navigation.get("mode") != "generate_from_sfm":
        return {"approved": False, "stage_status": "NOT_CONFIGURED", "gate_status": "NOT_REQUIRED",
                "reason": "No generated navigation approval is configured."}

    stage = state.stage("navigation")
    if not stage:
        raise ValueError("Viewer-only regeneration requires a recorded current navigation proposal.")
    try:
        details = json.loads(stage.get("details_json") or "{}")
    except (TypeError, ValueError) as exc:
        raise ValueError("Navigation stage details are corrupt; preview refresh was stopped.") from exc
    current_fingerprint = stage_fingerprint(root, config, state, "navigation", full_hash=True)
    if stage.get("input_fingerprint") != current_fingerprint:
        raise ValueError("Navigation inputs changed after generation or approval; refresh cannot enforce this boundary.")

    gate_row = state.db.execute(
        "SELECT status,required,decision_json FROM review_gates WHERE gate_id='navigation-approval'"
    ).fetchone()
    gate_status = gate_row["status"] if gate_row else "MISSING"
    try:
        gate_decision = json.loads(gate_row["decision_json"] or "{}") if gate_row else {}
    except (TypeError, ValueError) as exc:
        raise ValueError("Navigation approval record is corrupt; preview refresh was stopped.") from exc

    output = output_path(root, config).resolve()

    def owned_file(value: Any, label: str) -> Path:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Navigation {label} path is missing; preview refresh was stopped.")
        raw = Path(value).expanduser()
        if raw.is_symlink():
            raise ValueError(f"Navigation {label} must be a regular project artifact, not a symlink.")
        path = (raw if raw.is_absolute() else output / raw).resolve()
        try:
            path.relative_to(output)
        except ValueError as exc:
            raise ValueError(f"Navigation {label} is outside the configured project output; preview refresh was stopped.") from exc
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Navigation {label} is missing or is not a regular file; preview refresh was stopped.")
        return path

    def read_bound(path: Path, label: str, *, require_registry: bool) -> tuple[dict, str]:
        digest = sha256_file(path)
        if require_registry:
            row = state.db.execute(
                "SELECT sha256,size_bytes,status,provenance_json FROM artifacts WHERE stage_id='navigation' AND path=?",
                (str(path),),
            ).fetchone()
            if not row or row["status"] != "VALIDATED" or row["sha256"] != digest or row["size_bytes"] != path.stat().st_size:
                raise ValueError(f"Navigation {label} does not match its validated Engine artifact record.")
            try:
                registration = json.loads(row["provenance_json"] or "{}")
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Navigation {label} Engine provenance is corrupt.") from exc
            if registration.get("project_id") != config.get("project_id"):
                raise ValueError(f"Navigation {label} belongs to a different project.")
        try:
            document = read_json(path)
        except (OSError, ValueError, TypeError) as exc:
            raise ValueError(f"Navigation {label} is unreadable or corrupt.") from exc
        if not isinstance(document, dict):
            raise ValueError(f"Navigation {label} must contain a JSON object.")
        return document, digest

    if stage.get("status") == "ADOPTED":
        if gate_status != "APPROVED" or gate_decision.get("decision") != "APPROVE":
            raise ValueError("Navigation stage is ADOPTED but its current approval gate is not APPROVED.")
        if not gate_row or int(gate_row["required"]) != 1:
            raise ValueError("Navigation approval gate is not configured as a required review.")

        proposal_path = owned_file(details.get("proposal_path"), "proposal")
        candidate_path = owned_file(details.get("review_candidate_path"), "approved draft")
        approved_path = owned_file(details.get("approved_artifact"), "approved artifact")
        reviewed_path = owned_file(details.get("reviewed_version"), "reviewed artifact")
        if proposal_path != (output / "navigation" / "proposals" / proposal_path.name).resolve():
            raise ValueError("Current navigation proposal is not in the configured proposal directory.")
        if candidate_path != (output / "navigation" / "drafts" / candidate_path.name).resolve():
            raise ValueError("Approved navigation draft is not in the configured draft directory.")
        if approved_path != (output / "work" / "navigation-approved.json").resolve():
            raise ValueError("Navigation approval points to an unexpected approved-artifact path.")
        if reviewed_path.parent != (output / "navigation" / "reviewed").resolve():
            raise ValueError("Navigation approval points to an unexpected reviewed-artifact path.")

        proposal, proposal_sha = read_bound(proposal_path, "proposal", require_registry=True)
        candidate, candidate_sha = read_bound(candidate_path, "approved draft", require_registry=True)
        approved, approved_sha = read_bound(approved_path, "approved artifact", require_registry=True)
        reviewed, reviewed_sha = read_bound(reviewed_path, "reviewed artifact", require_registry=True)

        recorded_candidate_sha = gate_decision.get("candidate_sha256")
        approval = details.get("approval") or {}
        if (gate_decision.get("navigation_input_fingerprint") != current_fingerprint or
                gate_decision.get("candidate_path") != str(candidate_path) or
                recorded_candidate_sha != candidate_sha or
                details.get("draft_sha256") != candidate_sha or
                approval.get("candidate_sha256") != candidate_sha or
                approval.get("candidate_path") != str(candidate_path)):
            raise ValueError("Navigation approval does not bind the current draft path, SHA-256, and input fingerprint.")
        if gate_decision.get("proposal_path") != str(proposal_path) or approval.get("proposal_path") != str(proposal_path):
            raise ValueError("Navigation approval is not bound to the current generated proposal.")
        if details.get("approved_artifact") != str(approved_path) or details.get("reviewed_version") != str(reviewed_path):
            raise ValueError("Navigation stage paths do not match the current approved and reviewed artifacts.")

        for label, document in (("proposal", proposal), ("approved draft", candidate),
                                ("approved artifact", approved), ("reviewed artifact", reviewed)):
            if document.get("provenance", {}).get("navigation_input_fingerprint") != current_fingerprint:
                raise ValueError(f"Navigation {label} carries a stale input fingerprint.")
            if document.get("id") != approved.get("id"):
                raise ValueError(f"Navigation {label} does not refer to the approved proposal identity.")
        if proposal.get("status") != "NAVIGATION_NEEDS_REVIEW":
            raise ValueError("The bound navigation proposal has an unexpected generation status.")
        if candidate.get("provenance", {}).get("generation_execution_id") != stage.get("last_execution_id"):
            raise ValueError("Approved draft does not originate from the current navigation generation.")
        if (approved_sha != reviewed_sha or approved.get("status") != "NAVIGATION_APPROVED_BY_REVIEW" or
                approved.get("provenance", {}).get("review_candidate_sha256") != candidate_sha or
                reviewed.get("provenance", {}).get("review_candidate_sha256") != candidate_sha):
            raise ValueError("Approved and reviewed navigation artifact identities do not match the reviewed draft.")
        review = approved.get("review") or {}
        if not review.get("approved") or review.get("status") != "APPROVED":
            raise ValueError("The approved navigation artifact does not contain a valid approval record.")
        decisions = review.get("decisions") or []
        if not any(item.get("decision") == "APPROVE" and
                   item.get("source_artifact_sha256") == candidate_sha and
                   item.get("reviewer") == gate_decision.get("reviewer") and
                   item.get("timestamp_utc") == gate_decision.get("reviewed_at_utc")
                   for item in decisions if isinstance(item, dict)):
            raise ValueError("Navigation artifact approval record does not match the persisted review gate.")
        history_row = state.db.execute(
            "SELECT payload_json FROM review_history WHERE gate_id='navigation-approval' AND decision='APPROVE' ORDER BY review_id DESC LIMIT 1"
        ).fetchone()
        if not history_row:
            raise ValueError("Navigation approval audit history is missing.")
        try:
            history = json.loads(history_row["payload_json"] or "{}")
        except (TypeError, ValueError) as exc:
            raise ValueError("Navigation approval audit history is corrupt.") from exc
        if (history.get("candidate_sha256") != candidate_sha or
                history.get("navigation_input_fingerprint") != current_fingerprint or
                history.get("proposal_path") != str(proposal_path)):
            raise ValueError("Navigation approval audit history does not bind the current artifact set.")

        stored_validation = approved.get("validation") or {}
        if (stored_validation.get("status") != "PASS" or stored_validation.get("public_ready") is not True or
                stored_validation.get("review_status") != "NAVIGATION_APPROVED_BY_REVIEW"):
            raise ValueError("Approved navigation geometry has not passed its stored validation contract.")
        validation = validate_navigation_artifact(approved)
        if validation.get("status") != "PASS" or not validation.get("finite_geometry"):
            raise ValueError(f"Approved navigation geometry failed validation: {validation.get('errors', [])}")
        compatibility = approved.get("viewer_compatibility") or {}
        envelope = compatibility.get("envelope")
        stations = compatibility.get("stations")
        if not isinstance(envelope, dict) or not isinstance(stations, dict):
            raise ValueError("Approved navigation is missing its Viewer envelope or camera stations.")
        if not (envelope.get("horizontalBoundary") or {}).get("regions"):
            raise ValueError("Approved navigation has no horizontal regions to enforce.")
        if not compatibility.get("runtime_policy"):
            raise ValueError("Approved navigation has no runtime policy.")
        return {"approved": True, "stage_status": "ADOPTED", "gate_status": "APPROVED",
                "reason": "Current approved navigation passed artifact, review, fingerprint, and geometry checks.",
                "artifact": approved, "artifact_path": approved_path, "artifact_sha256": approved_sha,
                "candidate_path": candidate_path, "candidate_sha256": candidate_sha,
                "proposal_path": proposal_path, "proposal_sha256": proposal_sha,
                "reviewed_path": reviewed_path, "input_fingerprint": current_fingerprint,
                "envelope": envelope, "stations": stations,
                "runtime_policy": compatibility["runtime_policy"], "validation": validation}

    if stage.get("status") != "NEEDS_REVIEW":
        raise ValueError(f"Navigation stage status {stage.get('status')} cannot be used for preview refresh.")
    if gate_status == "APPROVED":
        raise ValueError("Navigation gate is APPROVED but the navigation stage is not ADOPTED; refusing ambiguous authority.")
    proposal_path = owned_file(details.get("proposal_path"), "proposal")
    proposal, proposal_sha = read_bound(proposal_path, "proposal", require_registry=False)
    if proposal.get("provenance", {}).get("navigation_input_fingerprint") != current_fingerprint:
        raise ValueError("Current navigation proposal carries a stale input fingerprint.")
    return {"approved": False, "stage_status": stage.get("status"), "gate_status": gate_status,
            "reason": "Navigation is not approved; the private Visitor remains unbounded.",
            "artifact": None, "proposal": proposal, "proposal_path": proposal_path,
            "proposal_sha256": proposal_sha, "input_fingerprint": current_fingerprint}


def current_navigation_review(root: Path, config: dict, state) -> dict[str, Any]:
    stage, details, path, fingerprint, digest = _context(root, config, state)
    return {"path": str(path), "sha256": digest, "navigation_input_fingerprint": fingerprint,
            "stage_status": stage["status"], "approval_status": ("APPROVED" if stage["status"] == "ADOPTED" else "PENDING"),
            "artifact": read_json(path), "details": details}


def invalidate_navigation_approval(state, reason: str, current_fingerprint: str | None = None) -> dict[str, Any] | None:
    """Retain the prior gate decision in history while revoking its current authority."""
    row = state.db.execute("SELECT * FROM review_gates WHERE gate_id='navigation-approval'").fetchone()
    if not row or row["status"] == "INVALIDATED":
        return None
    prior = json.loads(row["decision_json"] or "{}")
    timestamp = utc_now()
    decision = {"decision": "INVALIDATED", "reason": reason, "previous_status": row["status"],
                "previous_decision": prior, "current_navigation_input_fingerprint": current_fingerprint,
                "invalidated_at_utc": timestamp}
    state.db.execute("UPDATE review_gates SET status='INVALIDATED',required=1,updated_at=?,decision_json=? WHERE gate_id='navigation-approval'",
                     (timestamp, json.dumps(decision, sort_keys=True)))
    state.db.execute("INSERT INTO review_history(created_at,gate_id,reviewer,decision,payload_json) VALUES(?,?,?,?,?)",
                     (timestamp, "navigation-approval", "Exhibition Archive Engine", "INVALIDATED", json.dumps(decision, sort_keys=True)))
    state.db.commit()
    return decision


def save_navigation_draft(root: Path, config: dict, state, artifact: dict, reviewer: str,
                          expected_sha256: str) -> dict[str, Any]:
    from .workflow import output_path

    reviewer = str(reviewer or "").strip()
    if not reviewer:
        raise ValueError("Reviewer identity is required to save a navigation draft")
    stage, details, _current_path, fingerprint, _current_sha = _context(
        root, config, state, expected_sha256=expected_sha256)
    if artifact.get("provenance", {}).get("navigation_input_fingerprint") != fingerprint:
        raise ValueError("Draft does not carry the current navigation input fingerprint")
    original = read_json(details["proposal_path"])
    draft_dir = output_path(root, config) / "navigation" / "drafts"
    draft_dir.mkdir(parents=True, exist_ok=True)
    versions = [int(path.stem.rsplit("v", 1)[1]) for path in draft_dir.glob("navigation-draft-v*.json")
                if path.stem.rsplit("v", 1)[-1].isdigit()]
    draft_version = max(versions, default=0) + 1
    invalidated = stage.get("status") == "ADOPTED"
    saved = json.loads(json.dumps(artifact))
    saved["artifact_version"] = max(int(artifact.get("artifact_version", 1)) + 1,
                                     int(original.get("artifact_version", 1)) + draft_version)
    timestamp = utc_now()
    prior_review = dict(saved.get("review", {}))
    if invalidated:
        prior_approval = details.get("approval") or {key: prior_review.get(key) for key in
                          ("reviewer", "rationale", "reviewed_at_utc", "source_artifact_sha256")}
        prior_review["invalidated_prior_approval"] = prior_approval
        for key in ("reviewer", "rationale", "reviewed_at_utc", "source_artifact_sha256", "approved"):
            prior_review.pop(key, None)
        saved["status"] = "NAVIGATION_NEEDS_REVIEW"
        for region in saved.get("regions", []):
            region["status"] = "NEEDS_REVIEW"
            region["classification"] = "REVIEW_CANDIDATE"
            for axis in ("floor", "ceiling"):
                bound = region.get("vertical_bounds", {}).get(axis)
                if bound is not None:
                    bound["review_status"] = "NEEDS_REVIEW"
                    bound.pop("reviewed_by", None); bound.pop("reviewed_at_utc", None)
        for transition in saved.get("transitions", []):
            transition["approved"] = False
            transition["classification"] = "REVIEW_PROPOSED"
            transition.pop("reviewed_by", None); transition.pop("reviewed_at_utc", None)
        compatibility = saved.get("viewer_compatibility") or {}
        envelope = compatibility.get("envelope") or {}
        envelope["status"] = "NEEDS_REVIEW"
        policy = compatibility.get("runtime_policy") or {}
        policy["status"] = "NEEDS_REVIEW"
        policy.pop("review", None)
    saved["review"] = {**prior_review, "status": "DRAFT", "approved": False,
                        "draft_version": draft_version, "draft_saved_by": reviewer,
                        "draft_saved_at_utc": timestamp}
    validation = validate_navigation_artifact(saved)
    saved["validation"] = {**validation, "public_ready": False, "review_status": "NEEDS_REVIEW"}
    path = draft_dir / f"navigation-draft-v{draft_version:04d}.json"
    write_json(path, saved)
    digest = sha256_file(path)

    stage_details = {**details, "review_candidate_path": str(path.resolve()), "draft_version": draft_version,
                     "draft_sha256": digest, "draft_validation": validation, "publishing_ready": False}
    if invalidated:
        stage_details["approval_invalidated"] = {"reason": "A new navigation draft replaced the approved content",
                                                 "invalidated_at_utc": timestamp,
                                                 "previous_approval": details.get("approval")}
    state.add_artifact("navigation", path, "navigation_json", digest, path.stat().st_size,
                       {"project_id": config["project_id"], "draft_version": draft_version, "status": "DRAFT"})
    state.set_stage("navigation", "NEEDS_REVIEW", fingerprint, stage.get("output_digest"),
                    stage.get("last_execution_id"), stage_details)
    gate_status = "INVALIDATED" if invalidated else "PENDING"
    decision = {"decision": "SAVE_DRAFT", "reviewer": reviewer, "saved_at_utc": timestamp,
                "candidate_path": str(path.resolve()), "candidate_sha256": digest,
                "navigation_input_fingerprint": fingerprint, "validation_status": validation["status"],
                "invalidated_prior_approval": invalidated,
                "prior_approval": details.get("approval") if invalidated else None}
    if invalidated:
        invalidation = {"decision": "INVALIDATED", "reason": "A new navigation draft replaced the approved content",
                        "previous_approval": details.get("approval"), "replacement_draft_path": str(path.resolve()),
                        "replacement_draft_sha256": digest, "navigation_input_fingerprint": fingerprint,
                        "invalidated_at_utc": timestamp}
        state.db.execute("INSERT INTO review_history(created_at,gate_id,reviewer,decision,payload_json) VALUES(?,?,?,?,?)",
                         (timestamp, "navigation-approval", "Exhibition Archive Engine", "INVALIDATED",
                          json.dumps(invalidation, sort_keys=True)))
    state.db.execute("INSERT INTO review_gates(gate_id,status,required,updated_at,decision_json) VALUES(?,?,?,?,?) "
                     "ON CONFLICT(gate_id) DO UPDATE SET status=excluded.status,required=excluded.required,updated_at=excluded.updated_at,decision_json=excluded.decision_json",
                     ("navigation-approval", gate_status, 1, timestamp, json.dumps(decision, sort_keys=True)))
    state.db.execute("INSERT INTO review_history(created_at,gate_id,reviewer,decision,payload_json) VALUES(?,?,?,?,?)",
                     (timestamp, "navigation-approval", reviewer, "SAVE_DRAFT", json.dumps(decision, sort_keys=True)))
    summary_path = output_path(root, config) / "work" / "navigation-summary.json"
    if summary_path.is_file():
        summary = read_json(summary_path)
        summary.update({"status": "NAVIGATION_NEEDS_REVIEW", "approval_status": gate_status,
                        "review_candidate": str(path.resolve()), "reviewed_version": saved["artifact_version"],
                        "validation": validation, "publishing_ready": False})
        write_json(summary_path, summary)
    state.db.commit()
    return {"status": "SAVED", "stage_status": "NEEDS_REVIEW", "approval_status": gate_status,
            "path": str(path.resolve()), "sha256": digest, "artifact_version": saved["artifact_version"],
            "validation": validation, "approval_invalidated": invalidated}


def record_navigation_decision(root: Path, config: dict, state, decision: str, reviewer: str, rationale: str,
                               *, candidate_path: str | Path | None = None,
                               expected_sha256: str | None = None) -> dict[str, Any]:
    from .workflow import output_path, stage_fingerprint

    reviewer, rationale = str(reviewer or "").strip(), str(rationale or "").strip()
    if not reviewer or not rationale:
        raise ValueError("Reviewer identity and rationale are required")
    action = decision.upper()
    if action not in {"APPROVE", "REJECT"}:
        raise ValueError("Navigation decision must be approve or reject")
    stage, details, selected_path, fingerprint, candidate_hash = _context(
        root, config, state, expected_sha256=expected_sha256, candidate_path=candidate_path)
    candidate = read_json(selected_path)
    if candidate.get("provenance", {}).get("navigation_input_fingerprint") != fingerprint:
        raise ValueError("Review artifact does not carry the current navigation input fingerprint")
    timestamp = utc_now()
    decision_record = {"decision": action, "reviewer": reviewer, "rationale": rationale,
                       "reviewed_at_utc": timestamp, "proposal_path": details.get("proposal_path"),
                       "candidate_path": str(selected_path.resolve()), "candidate_sha256": candidate_hash,
                       "navigation_input_fingerprint": fingerprint}
    if action == "APPROVE":
        reviewed = approve_navigation_artifact(candidate, reviewer, rationale, timestamp=timestamp,
                                                source_artifact_sha256=candidate_hash)
        reviewed["provenance"]["navigation_input_fingerprint"] = fingerprint
        reviewed["provenance"]["review_candidate_sha256"] = candidate_hash
        review_dir = output_path(root, config) / "navigation" / "reviewed"
        review_dir.mkdir(parents=True, exist_ok=True)
        version = 1 + len(list(review_dir.glob("navigation-reviewed-v*.json")))
        reviewed_path = review_dir / f"navigation-reviewed-v{version:04d}.json"
        approved_path = output_path(root, config) / "work" / "navigation-approved.json"
        write_json(reviewed_path, reviewed)
        write_json(approved_path, reviewed)
        summary_path = output_path(root, config) / "work" / "navigation-summary.json"
        if summary_path.is_file():
            summary = read_json(summary_path)
            summary.update({"status": "NAVIGATION_APPROVED_BY_REVIEW", "approval_status": "APPROVED",
                            "approved_artifact": str(approved_path.resolve()), "reviewed_artifact": str(reviewed_path.resolve()),
                            "reviewed_version": version, "reviewer": reviewer, "reviewed_at_utc": timestamp,
                            "review_rationale": rationale, "publishing_ready": True})
            write_json(summary_path, summary)
        artifact_paths = [Path(details["proposal_path"]), selected_path,
                          Path(details["review_path"]) if details.get("review_path") else None,
                          reviewed_path, approved_path]
        artifact_paths = [path for path in artifact_paths if path and path.is_file()]
        digest = hashlib.sha256(json.dumps([{"path": str(path.resolve()), "sha256": sha256_file(path)}
                                           for path in artifact_paths], sort_keys=True).encode()).hexdigest()
        for path in artifact_paths:
            state.add_artifact("navigation", path, "navigation_json" if path.suffix == ".json" else "navigation_review",
                               sha256_file(path), path.stat().st_size,
                               {"project_id": config["project_id"], "reviewed_version": version})
        if stage.get("last_execution_id"):
            execution = state.execution(int(stage["last_execution_id"]))
            execution_details = json.loads(execution.get("details_json") or "{}") if execution else {}
            execution_details["artifacts"] = [str(path.resolve()) for path in artifact_paths]
            execution_details["approved_artifact"] = str(approved_path.resolve())
            state.db.execute("UPDATE stage_executions SET details_json=?,output_digest=? WHERE execution_id=?",
                             (json.dumps(execution_details, sort_keys=True), digest, stage["last_execution_id"]))
        stage_details = {**details, "approval": decision_record, "review_candidate_path": str(selected_path.resolve()),
                         "approved_artifact": str(approved_path.resolve()), "reviewed_version": str(reviewed_path.resolve()),
                         "publishing_ready": True}
        state.set_stage("navigation", "ADOPTED", fingerprint, digest, stage.get("last_execution_id"), stage_details)
        result = {"status": "APPROVED", "gate_id": "navigation-approval", "reviewed_version": version,
                  "candidate_sha256": candidate_hash, "approved_artifact": str(approved_path.resolve()),
                  "approved_sha256": sha256_file(approved_path), "publishing_ready": True}
    else:
        stage_details = {**details, "last_review": decision_record, "publishing_ready": False}
        state.set_stage("navigation", "NEEDS_REVIEW", fingerprint, stage.get("output_digest"),
                        stage.get("last_execution_id"), stage_details)
        result = {"status": "REJECTED", "gate_id": "navigation-approval", "candidate_sha256": candidate_hash,
                  "publishing_ready": False}
        summary_path = output_path(root, config) / "work" / "navigation-summary.json"
        if summary_path.is_file():
            summary = read_json(summary_path)
            summary.update({"status": "NAVIGATION_NEEDS_REVIEW", "approval_status": "REJECTED",
                            "last_review": decision_record, "publishing_ready": False})
            write_json(summary_path, summary)
    gate_status = "APPROVED" if action == "APPROVE" else "REJECTED"
    state.db.execute("INSERT INTO review_gates(gate_id,status,required,updated_at,decision_json) VALUES(?,?,?,?,?) "
                     "ON CONFLICT(gate_id) DO UPDATE SET status=excluded.status,required=excluded.required,updated_at=excluded.updated_at,decision_json=excluded.decision_json",
                     ("navigation-approval", gate_status, 1, timestamp, json.dumps(decision_record, sort_keys=True)))
    state.db.execute("INSERT INTO review_history(created_at,gate_id,reviewer,decision,payload_json) VALUES(?,?,?,?,?)",
                     (timestamp, "navigation-approval", reviewer, action, json.dumps(decision_record, sort_keys=True)))
    state.db.commit()
    return result
