from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import __version__
from .adapters import read_json, write_json
from .state import ProjectState, utc_now
from .workflow import (STAGES, WorkflowError, expensive_execution_plan, load_project, output_path,
                       plan, project_path, run_workflow, stage_fingerprint)
from .navigation import approve_navigation_artifact, validate_navigation_artifact
from .adapters import sha256_file, validate_navigation


PRODUCT_ROOT = Path(__file__).resolve().parents[2]


def _root_for_init(args) -> tuple[Path, dict]:
    if args.config:
        doc = read_json(args.config)
        if doc.get("schema_version") != "exhibition-archive.project.v1":
            raise ValueError("--config must be a complete exhibition-archive.project.v1 project manifest")
        doc.setdefault("created_at_utc", utc_now())
        if args.photos:
            doc.setdefault("source", {})["photo_directory"] = str(Path(args.photos).expanduser().resolve())
        if args.project:
            doc["project_id"] = args.project
    else:
        if not args.photos or not args.project:
            raise ValueError("init requires SOURCE_PHOTO_DIR and --project when --config is omitted")
        doc = {"schema_version": "exhibition-archive.project.v1", "project_id": args.project,
               "created_at_utc": utc_now(), "source": {"photo_directory": str(Path(args.photos).expanduser().resolve()), "read_only": True},
               "output_directory": "output", "viewer": {"title": args.project, "theme": "dark"},
               "publication": {"public_site": True, "include_source_photographs": False, "include_private_review_notes": False},
               "archive_policy": {"bundle_source_photographs": False}, "integrations": {}}
    if not doc.get("source", {}).get("photo_directory"):
        raise ValueError("project manifest must configure source.photo_directory")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,127}", str(doc.get("project_id", ""))):
        raise ValueError("project_id must be 2–128 letters, digits, dots, underscores, or hyphens and start with a letter or digit")
    workspace = Path(args.workspace).expanduser().resolve() if args.workspace else PRODUCT_ROOT / "projects" / doc["project_id"]
    return workspace, doc


def cmd_init(args) -> int:
    root, config = _root_for_init(args)
    source_dir = project_path(root, config, config["source"]["photo_directory"])
    if not source_dir.is_dir():
        raise FileNotFoundError(f"Source photo directory does not exist: {source_dir}")
    if config.get("source", {}).get("read_only", True) is not True:
        raise ValueError("The engine treats the registered source photo directory as read-only; set source.read_only=true")
    if config.get("archive_policy", {}).get("bundle_source_photographs"):
        raise ValueError("Bundling source photographs is not implemented in this MVP; set bundle_source_photographs=false and retain the managed source directory")
    if config.get("publication", {}).get("include_source_photographs") or config.get("publication", {}).get("include_private_review_notes"):
        raise ValueError("The static publisher does not expose source photographs or private review notes; disable those publication flags")
    root.mkdir(parents=True, exist_ok=True)
    config_path = root / "project.json"
    if config_path.exists():
        raise FileExistsError(f"Project already exists: {config_path}; select it with status or use a new workspace")
    write_json(config_path, config)
    state = ProjectState(root); state.close()
    print(json.dumps({"status": "INITIALIZED", "project_id": config["project_id"], "project": str(root), "config": str(config_path), "state_store": str(root / "workflow.sqlite3"), "source_photo_directory": config["source"]["photo_directory"], "source_copied": False}, indent=2))
    return 0


def cmd_plan(args) -> int:
    root, config = load_project(args.project)
    state = ProjectState(root)
    try:
        result = plan(root, config, state, discover_backends=True)
        print(f"Project {config['project_id']} · workflow DAG")
        for x in result:
            print(f"{x['id']:<22} {x['readiness']:<36} state={x['current_status']:<12} mode={x['execution_mode']}")
            print(f"  deps: {', '.join(x['deps']) or 'none'} · outputs: {', '.join(x['outputs'])} · resource: {x['resource']}")
            if x.get("planned_command"):
                print("  command: " + json.dumps(x["planned_command"], ensure_ascii=False))
            group = "sfm" if x["id"] == "sfm" else "model" if x["id"] == "training" else None
            if group and config.get("integrations", {}).get(group, {}).get("mode") == "spirula_fresh":
                if not args.full_hash:
                    print("  fresh backend authorization: run plan --full-hash to force-hash inputs and show the authorizable plan_id")
                else:
                    try:
                        execution_plan = expensive_execution_plan(root, config, state, x["id"])
                        print("  fresh backend authorization plan: " + json.dumps(execution_plan, ensure_ascii=False))
                    except Exception as exc:
                        print(f"  fresh backend authorization unavailable: {exc}")
        return 0
    finally:
        state.close()


def cmd_authorize(args) -> int:
    root, config = load_project(args.project)
    reviewer, rationale = args.reviewer.strip(), args.rationale.strip()
    if not reviewer or not rationale:
        raise ValueError("--reviewer and --rationale must be non-empty; this command records explicit operator consent")
    state = ProjectState(root)
    try:
        with state.lock():
            execution_plan = expensive_execution_plan(root, config, state, args.stage)
            if args.plan_id != execution_plan["plan_id"]:
                raise WorkflowError("The supplied plan_id does not match the current full-hash execution plan. Run plan --full-hash and review the updated plan before authorizing.")
            authorization_id = "ea_" + secrets.token_urlsafe(18)
            created = datetime.now(timezone.utc).replace(microsecond=0)
            expires = created + timedelta(minutes=15)
            state.create_execution_authorization(authorization_id, config["project_id"], args.stage,
                                                 execution_plan["plan_id"], execution_plan, reviewer,
                                                 rationale, expires.isoformat().replace("+00:00", "Z"))
            print(json.dumps({"status": "AUTHORIZED_ONCE", "authorization_id": authorization_id,
                              "project_id": config["project_id"], "stage": args.stage,
                              "plan_id": execution_plan["plan_id"], "created_at": created.isoformat().replace("+00:00", "Z"),
                              "expires_at": expires.isoformat().replace("+00:00", "Z"),
                              "reviewer": reviewer, "rationale": rationale,
                              "scope": "one fresh backend attempt for this exact full-hash plan; not a sandbox or security boundary"}, indent=2))
            return 0
    finally:
        state.close()


def cmd_run(args) -> int:
    root, config = load_project(args.project)
    state = ProjectState(root)
    try:
        with state.lock():
            result = run_workflow(root, config, state, resume=args.resume, force_hash=args.full_hash,
                                  fail_at=getattr(args, "fail_at", None),
                                  authorization_ids=getattr(args, "authorization_id", []),
                                  package_only=getattr(args, "package_only", False))
        print(json.dumps(result, indent=2))
        status = result.get("status", "COMPLETED")
        if status.startswith(("AUTHORIZATION_REQUIRED", "BLOCKED_PACKAGE")): return 3
        if status.startswith("BLOCKED_BACKEND"): return 1
        return 0
    finally:
        state.close()


def cmd_diagnostic_smoke_train(args) -> int:
    root, config = load_project(args.project)
    from .smoke import run_smoke_training
    state = ProjectState(root)
    try:
        with state.lock():
            result = run_smoke_training(root, config, state, operator_authorized=args.authorize_gpu_training)
        print(json.dumps(result, indent=2))
        return 0
    finally:
        state.close()


def cmd_diagnostic_smoke_preflight(args) -> int:
    root, config = load_project(args.project)
    from .smoke import preflight_smoke_training
    result = preflight_smoke_training(root, config, operator_authorized=args.authorize_gpu_training)
    print(json.dumps(result, indent=2))
    return 0


def cmd_diagnostic_smoke_validate(args) -> int:
    root, config = load_project(args.project)
    from .smoke import reconcile_smoke_attempt
    state = ProjectState(root)
    try:
        with state.lock():
            result = reconcile_smoke_attempt(root, config, state, args.attempt_id)
        print(json.dumps(result, indent=2))
        return 0
    finally:
        state.close()


def cmd_status(args) -> int:
    root, config = load_project(args.project)
    state = ProjectState(root)
    try:
        from .backends import reconcile_running_stage
        for sid in ("sfm", "training"):
            reconcile_running_stage(state, sid)
        stages = state.stages()
        runs = [dict(x) for x in state.db.execute("SELECT run_id,started_at,ended_at,mode,status,summary_json FROM workflow_runs ORDER BY run_id DESC LIMIT 5")]
        gates = [dict(x) for x in state.db.execute("SELECT * FROM review_gates ORDER BY gate_id")]
        failed = any(x["status"] == "FAILED" for x in stages.values())
        blocked = any(x["status"] == "BLOCKED" for x in stages.values())
        pending_reviews = any(x["status"] in {"PENDING", "NEEDS_REVIEW"} for x in gates)
        review_stage = any(x["status"] == "NEEDS_REVIEW" for x in stages.values())
        complete = bool(stages) and all(x["status"] in {"COMPLETED", "ADOPTED"} for x in stages.values())
        overall = "FAILED" if failed else ("BLOCKED" if blocked else ("READY_TO_REVIEW" if pending_reviews or review_stage else ("COMPLETED" if complete else "IN_PROGRESS")))
        out = output_path(root, config)
        authorizations = state.execution_authorizations()
        from .release_gate import publication_release_status
        publication_release = publication_release_status(root, config, state)
        sfm_component_acceptance = None
        sfm_review_history = []
        if stages.get("sfm", {}).get("status") in {"NEEDS_REVIEW", "COMPLETED", "ADOPTED"}:
            from .sfm_review import component_acceptance_status
            try:
                sfm_component_acceptance = component_acceptance_status(root, config, state)
            except Exception as exc:
                sfm_component_acceptance = {"status": "STALE_OR_UNAVAILABLE", "reason": str(exc)}
            sfm_review_history = [dict(row) for row in state.db.execute(
                "SELECT review_id,created_at,gate_id,reviewer,decision FROM review_history "
                "WHERE gate_id LIKE ? ORDER BY review_id DESC", ("sfm-component-acceptance:%",))]
        print(json.dumps({"project_id": config["project_id"], "root": str(root), "overall": overall,
                          "stages": stages, "review_gates": gates, "execution_authorizations": authorizations,
                          "sfm_component_acceptance": sfm_component_acceptance,
                          "sfm_component_review_history": sfm_review_history,
                          "publication_release": publication_release,
                          "recent_runs": runs,
                          "resume_eligible": any(x["status"] in {"FAILED", "RUNNING", "INVALIDATED", "BLOCKED"} for x in stages.values()),
                          "selected_model": stages.get("training", {}).get("details_json"),
                          "website": str(out / "website"), "archive": str(out / "archive"),
                          "next_action": next((json.loads(v.get("details_json") or "{}").get("reason") for k, v in stages.items()
                                               if v.get("status") == "BLOCKED"), None)}, indent=2))
        return 0
    finally:
        state.close()


def cmd_cancel(args) -> int:
    if args.grace_seconds < 0:
        raise ValueError("--grace-seconds must be zero or greater")
    root, _ = load_project(args.project)
    state = ProjectState(root)
    try:
        from .backends import cancel_execution
        stage = state.stage(args.stage)
        if not stage or stage.get("status") != "RUNNING":
            print(json.dumps({"status": "NO_ACTIVE_STAGE_PROCESS", "stage": args.stage}, indent=2))
            return 0
        execution = state.execution(int(stage["last_execution_id"]))
        if not execution or execution.get("status") != "RUNNING":
            print(json.dumps({"status": "NO_ACTIVE_PROCESS_RECORD", "stage": args.stage}, indent=2))
            return 0
        result = cancel_execution(execution, grace_seconds=args.grace_seconds)
        result["stage"] = args.stage
        print(json.dumps(result, indent=2))
        return 0
    finally:
        state.close()


def cmd_logs(args) -> int:
    if args.lines < 1:
        raise ValueError("--lines must be a positive integer")
    root, _ = load_project(args.project)
    state = ProjectState(root)
    try:
        stage = state.stage(args.stage)
        execution = state.execution(int(stage["last_execution_id"])) if stage and stage.get("last_execution_id") else None
        log_path = Path(execution.get("log_path") or "") if execution else None
        if not log_path or not log_path.is_file():
            raise WorkflowError(f"No backend log is recorded for stage {args.stage}")
        from collections import deque
        with log_path.open("r", encoding="utf-8", errors="replace") as stream:
            lines = deque(stream, maxlen=args.lines)
        print("".join(lines), end="")
        return 0
    finally:
        state.close()


def cmd_review_model(args) -> int:
    from .model_registry import fresh_candidate_record
    root, config = load_project(args.project)
    if config.get("integrations", {}).get("model", {}).get("mode", "adopt_existing") != "spirula_fresh":
        raise WorkflowError("review-model applies only to a spirula_fresh training candidate")
    reviewer = args.reviewer.strip()
    rationale = args.rationale.strip()
    if not reviewer or not rationale:
        raise ValueError("--reviewer and --rationale must be non-empty")
    state = ProjectState(root)
    try:
        candidate = fresh_candidate_record(root, config, state)
        if args.candidate_sha256 != candidate["sha256"]:
            raise WorkflowError("--candidate-sha256 does not match the current registered PLY candidate")
        if args.decision == "approve" and candidate["run_type"] != "candidate":
            raise WorkflowError("SMOKE_TEST_MODEL artifacts cannot be approved for public selection")
        decision = {"decision": args.decision.upper(), "reviewer": reviewer, "rationale": rationale,
                    "candidate_path": candidate["path"], "candidate_sha256": candidate["sha256"],
                    "candidate_attempt_id": candidate.get("attempt_id"), "candidate_run_type": candidate["run_type"],
                    "reviewed_at_utc": utc_now(), "public_selection_allowed": args.decision == "approve"}
        gate_status = "APPROVED" if args.decision == "approve" else "REJECTED"
        state.db.execute("INSERT INTO review_gates(gate_id,status,required,updated_at,decision_json) VALUES(?,?,?,?,?) ON CONFLICT(gate_id) DO UPDATE SET status=excluded.status,required=excluded.required,updated_at=excluded.updated_at,decision_json=excluded.decision_json",
                         ("fresh-model-selection", gate_status, 1, utc_now(), json.dumps(decision, sort_keys=True)))
        state.db.execute("INSERT INTO review_history(created_at,gate_id,reviewer,decision,payload_json) VALUES(?,?,?,?,?)",
                         (utc_now(), "fresh-model-selection", reviewer, args.decision.upper(), json.dumps(decision, sort_keys=True)))
        state.db.commit()
        print(json.dumps({"gate_id": "fresh-model-selection", "status": gate_status, "candidate_sha256": candidate["sha256"], "candidate_path": candidate["path"], "run_type": candidate["run_type"], "public_selection_allowed": decision["public_selection_allowed"], "next_step": "run resume to materialize model-selection.json and continue only when navigation and publication prerequisites are configured"}, indent=2))
        return 0
    finally:
        state.close()


def cmd_sfm_review(args) -> int:
    from .sfm_review import review_sfm_component
    root, config = load_project(args.project)
    state = ProjectState(root)
    try:
        with state.lock():
            result = review_sfm_component(root, config, state, component_id=args.component,
                                          decision=args.decision, reviewer=args.reviewer,
                                          rationale=args.rationale, limitations=args.limitation)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    finally:
        state.close()


def cmd_navigation(args) -> int:
    root, config = load_project(args.project)
    state = ProjectState(root)
    try:
        stage = state.stage("navigation")
        details = json.loads(stage.get("details_json") or "{}") if stage else {}
        gate = state.db.execute("SELECT * FROM review_gates WHERE gate_id='navigation-approval'").fetchone()
        proposal_path = Path(details["proposal_path"]) if details.get("proposal_path") else None
        if args.navigation_action == "status":
            approved_artifact = output_path(root, config) / "work" / "navigation-approved.json"
            print(json.dumps({"project_id": config["project_id"], "navigation_mode": config.get("integrations", {}).get("navigation", {}).get("mode", "adopt_existing"),
                              "stage": stage, "review_gate": dict(gate) if gate else None,
                              "proposal": str(proposal_path) if proposal_path else None,
                              "review_candidate": details.get("review_candidate_path") or (str(approved_artifact) if stage and stage.get("status") == "ADOPTED" and approved_artifact.is_file() else (str(proposal_path) if stage and stage.get("status") == "NEEDS_REVIEW" and proposal_path and proposal_path.is_file() else None)),
                              "review_preview": details.get("review_path"), "approved_artifact": str(approved_artifact) if stage and stage.get("status") == "ADOPTED" and approved_artifact.is_file() else None,
                              "training_dependency": False}, indent=2))
            return 0
        if args.navigation_action == "assess":
            rows = plan(root, config, state, discover_backends=False)
            nav_plan = next(item for item in rows if item["id"] == "navigation")
            print(json.dumps({"project_id": config["project_id"], "assessment": nav_plan,
                              "current_proposal": details or None,
                              "required_before_publication": ["verified SfM-to-viewer transform", "reviewed region boundaries", "floor and ceiling bounds per region", "explicit approval"],
                              "notice": "Assessment inspects configured evidence and workflow state; it does not approve a proposal."}, indent=2))
            return 0
        if args.navigation_action == "validate":
            candidate_path = Path(args.artifact).expanduser().resolve() if args.artifact else proposal_path
            if candidate_path is None or not candidate_path.is_file():
                raise WorkflowError("No navigation artifact is available; run navigation generate first")
            candidate = read_json(candidate_path)
            result = validate_navigation_artifact(candidate)
            print(json.dumps({"artifact": str(candidate_path), "sha256": sha256_file(candidate_path), "validation": result}, indent=2))
            return 0 if result["status"] == "PASS" else 1
        if args.navigation_action == "generate":
            if config.get("integrations", {}).get("navigation", {}).get("mode", "adopt_existing") != "generate_from_sfm":
                raise WorkflowError("navigation generate requires integrations.navigation.mode=generate_from_sfm")
            with state.lock():
                result = run_workflow(root, config, state, resume=True)
            print(json.dumps(result, indent=2))
            return 0
        if args.navigation_action == "review":
            from .navigation_review import record_navigation_decision
            result = record_navigation_decision(root, config, state, args.decision, args.reviewer, args.rationale,
                                               candidate_path=args.artifact)
            result["next_step"] = "run resume to continue the downstream engine stages" if args.decision == "approve" else "edit or regenerate the proposal, then record a new review"
            print(json.dumps(result, indent=2))
            return 0
        raise WorkflowError(f"Unsupported navigation action: {args.navigation_action}")
    finally:
        state.close()


def cmd_config_set(args) -> int:
    root, config = load_project(args.project)
    state = ProjectState(root)
    before = {stage["id"]: stage_fingerprint(root, config, state, stage["id"]) for stage in STAGES}
    keys = args.key.split(".")
    cursor = config
    for key in keys[:-1]:
        if key not in cursor or not isinstance(cursor[key], dict): cursor[key] = {}
        cursor = cursor[key]
    try: value = json.loads(args.value)
    except json.JSONDecodeError: value = args.value
    cursor[keys[-1]] = value
    write_json(root / "project.json", config)
    invalidated = []
    try:
        for stage in STAGES:
            sid = stage["id"]
            after = stage_fingerprint(root, config, state, sid)
            prior = state.stage(sid)
            if prior and before[sid] != after and prior["status"] in {"COMPLETED", "ADOPTED", "NEEDS_REVIEW"}:
                state.set_stage(sid, "INVALIDATED", prior["input_fingerprint"], prior["output_digest"], prior["last_execution_id"], {"reason": f"configuration changed at {args.key}", "previous_details": json.loads(prior["details_json"] or "{}")})
                invalidated.append(sid)
            elif prior and prior["status"] == "INVALIDATED" and prior.get("input_fingerprint") == after:
                previous = state.db.execute("SELECT status,details_json FROM stage_executions WHERE execution_id=?", (prior.get("last_execution_id"),)).fetchone()
                if previous and previous["status"] in {"COMPLETED", "ADOPTED"}:
                    state.set_stage(sid, previous["status"], after, prior.get("output_digest"), prior.get("last_execution_id"), json.loads(previous["details_json"] or "{}"))
        if "navigation" in invalidated:
            from .navigation_review import invalidate_navigation_approval
            invalidate_navigation_approval(state, f"Navigation inputs changed at {args.key}", stage_fingerprint(root, config, state, "navigation"))
        print(json.dumps({"updated": args.key, "value": value, "invalidated": invalidated}, indent=2))
        return 0
    finally:
        state.close()


def cmd_review(args) -> int:
    root, config = load_project(args.project)
    review_config = config.get("integrations", {}).get("artwork", {}).get("review_server")
    if not review_config and config.get("integrations", {}).get("navigation", {}).get("mode") != "generate_from_sfm":
        raise WorkflowError("This project has no local artwork or generated-navigation review interface configured")
    if review_config:
        review_output = Path(review_config["output"]).expanduser()
        if not review_output.is_absolute(): review_output = (root / review_output).resolve()
        source_output = Path(review_config["source_output"]).expanduser().resolve()
        if review_output.resolve() == source_output:
            raise WorkflowError("Review output must be a project-local copy; refusing to write into the configured source evidence directory")
        if not review_output.exists():
            if not source_output.is_dir(): raise FileNotFoundError(f"Phase2EV review source output is missing: {source_output}")
            shutil.copytree(source_output, review_output)
        source_manifest = Path(review_config["source_project_manifest"]).expanduser().resolve()
        source_project_id = read_json(source_manifest).get("project_id") if source_manifest.is_file() else None
        if source_project_id and source_project_id != config["project_id"]:
            def rebind(value):
                changed = False
                if isinstance(value, dict):
                    result = {}
                    for key, child in value.items():
                        if key == "project_id" and child == source_project_id:
                            result[key] = config["project_id"]; changed = True
                        else:
                            result[key], nested_changed = rebind(child); changed |= nested_changed
                    if changed and value.get("project_id") == source_project_id:
                        result.setdefault("source_project_id", source_project_id)
                    return result, changed
                if isinstance(value, list):
                    result = []
                    for child in value:
                        item, nested_changed = rebind(child); result.append(item); changed |= nested_changed
                    return result, changed
                return value, False
            for review_file in review_output.rglob("*.json"):
                try:
                    value = read_json(review_file)
                    rebound, changed = rebind(value)
                    if changed: write_json(review_file, rebound)
                except (OSError, ValueError, UnicodeDecodeError):
                    continue
        review_manifest = Path(review_config["project_manifest"]).expanduser()
        if not review_manifest.is_absolute(): review_manifest = (root / review_manifest).resolve()
        if not review_manifest.exists():
            original_manifest = read_json(source_manifest)
            original_manifest["image_directory"] = str(project_path(root, config, config["source"]["photo_directory"]))
            original_manifest["project_id"] = config["project_id"]
            if source_project_id and source_project_id != config["project_id"]:
                original_manifest["source_project_id"] = source_project_id
            write_json(review_manifest, original_manifest)
    proc_file = root / "review-hub.json"
    if proc_file.exists():
        try:
            pid = int(read_json(proc_file).get("pid", 0))
            os.kill(pid, 0)
            print(json.dumps({"status": "RUNNING", "pid": pid, "url": read_json(proc_file).get("url"), "review_url": read_json(proc_file).get("review_url")}, indent=2))
            return 0
        except (ProcessLookupError, ValueError, OSError):
            proc_file.unlink(missing_ok=True)
    command = [sys.executable, "-m", "exhibition_archive.review_hub", "--project", str(root), "--port", str(args.port)]
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{PRODUCT_ROOT / 'src'}{os.pathsep}{env.get('PYTHONPATH', '')}"
    log = (root / "review-hub.log").open("ab")
    proc = subprocess.Popen(command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
    import time
    ready = False
    for _ in range(40):
        try:
            import urllib.request
            with urllib.request.urlopen(f"http://127.0.0.1:{args.port}/health", timeout=.3) as response:
                ready = response.status == 200
                if ready: break
        except Exception: time.sleep(.1)
    if not ready:
        raise WorkflowError(f"Review hub did not become ready. See {root / 'review-hub.log'}")
    meta = {"pid": proc.pid, "url": f"http://127.0.0.1:{args.port}/",
            "review_url": f"http://127.0.0.1:{args.port+1}/" if review_config else None,
            "navigation_url": f"http://127.0.0.1:{args.port}/navigation" if config.get("integrations", {}).get("navigation", {}).get("mode") == "generate_from_sfm" else None,
            "started_at": utc_now()}
    write_json(proc_file, meta)
    print(json.dumps({"status": "RUNNING", **meta, "dashboard": meta["url"], "phase2ev_review": meta["review_url"], "navigation_review": meta["navigation_url"], "log": str(root / "review-hub.log")}, indent=2))
    return 0


def cmd_stop_review(args) -> int:
    root, _ = load_project(args.project)
    meta_path = root / "review-hub.json"
    if not meta_path.exists(): print("Review hub is not recorded as running."); return 0
    meta = read_json(meta_path)
    try: os.kill(int(meta["pid"]), 15)
    except ProcessLookupError: pass
    meta_path.unlink(missing_ok=True)
    print("Review hub stopped")
    return 0


def cmd_reset_stage(args) -> int:
    root, _ = load_project(args.project)
    state = ProjectState(root)
    try:
        row = state.stage(args.stage)
        if not row or row["status"] not in {"FAILED", "INVALIDATED", "BLOCKED"}:
            raise WorkflowError(f"Stage {args.stage} is not failed, invalidated, or blocked")
        state.db.execute("UPDATE stage_executions SET status='FAILED_RETIRED' WHERE stage_id=? AND status='FAILED'", (args.stage,))
        state.db.commit()
        state.set_stage(args.stage, "PENDING", None, None, row.get("last_execution_id"), {"reset_reason": args.reason, "previous_attempts_retained": True})
        print(json.dumps({"stage": args.stage, "status": "PENDING", "reason": args.reason, "history_retained": True}, indent=2))
        return 0
    finally:
        state.close()


def cmd_verify(args) -> int:
    root, config = load_project(args.project)
    from .workflow import verify_outputs
    state = ProjectState(root)
    try:
        result = verify_outputs(root, config, output_path(root, config) / "work", state=state, force_hash=args.full_hash)
        from .release_gate import publication_release_status
        result["publication_release"] = publication_release_status(root, config, state)
        if args.public_release and not result["publication_release"]["public_release_ready"]:
            result["status"] = "FAIL"
            result["failures"].append("Public release approval is missing or stale; see publication_release for required human decisions")
        print(json.dumps(result, indent=2))
        return 0 if result["status"] == "PASS" else 1
    finally:
        state.close()


def cmd_publication_review(args) -> int:
    root, config = load_project(args.project)
    reviewer, rationale = args.reviewer.strip(), args.rationale.strip()
    if not reviewer or not rationale:
        raise ValueError("--reviewer and --rationale must be non-empty")
    state = ProjectState(root)
    try:
        from .release_gate import publication_release_status, release_candidate_identity
        from .workflow import verify_outputs
        current = release_candidate_identity(root, config)
        if current.get("status") != "READY_FOR_REVIEW":
            raise WorkflowError(f"A built website package is required before publication review: {current.get('missing_files')}")
        timestamp = utc_now()
        decisions = {"model_selection": args.model_selection, "navigation_approval": args.navigation_approval,
                     "artwork_metadata": args.artwork_metadata, "image_publication_rights": args.image_rights,
                     "artwork_publication_rights": args.artwork_rights, "access_policy": args.access_policy,
                     "public_private_data_policy": args.public_private_policy}
        if args.decision == "approve":
            if any(value in {None, "not-approved"} for value in decisions.values()):
                raise WorkflowError("Approval requires explicit positive decisions for model selection, navigation, metadata, image/artwork rights, access and public/private policy")
            technical = verify_outputs(root, config, output_path(root, config) / "work", state=state, force_hash=True)
            if technical.get("status") != "PASS":
                raise WorkflowError(f"Public release review requires a passing full-hash technical verification: {technical.get('failures')}")
            from .model_registry import selected_model_record
            selected_model = selected_model_record(root, config, state)
            if selected_model.get("run_type") == "smoke_test":
                raise WorkflowError("A smoke-test model cannot pass public release review")
            artwork_cfg = config.get("integrations", {}).get("artwork", {})
            catalogue_path = project_path(root, config, artwork_cfg["confirmed_artworks"]) if artwork_cfg.get("confirmed_artworks") else output_path(root, config) / "work" / "confirmed-artworks.json"
            catalogue = read_json(catalogue_path) if catalogue_path.is_file() else {"count": 0, "artworks": []}
            confirmed_count = int(catalogue.get("count", len(catalogue.get("artworks", []))))
            expected_metadata_decision = "confirmed-only" if confirmed_count else "empty-catalogue-approved"
            if decisions["artwork_metadata"] != expected_metadata_decision:
                raise WorkflowError(f"This package has {confirmed_count} confirmed public artwork records; the explicit metadata decision must be {expected_metadata_decision!r}")
            nav = config.get("integrations", {}).get("navigation", {})
            if nav.get("mode") == "generate_from_sfm":
                nav_stage = state.stage("navigation") or {}
                nav_gate = state.db.execute("SELECT status FROM review_gates WHERE gate_id='navigation-approval'").fetchone()
                if nav_stage.get("status") != "ADOPTED" or not nav_gate or nav_gate["status"] != "APPROVED":
                    raise WorkflowError("Public release review requires current durable navigation approval for generated navigation")
            elif nav.get("envelope") and nav.get("runtime_policy"):
                expected_model_hash = selected_model.get("sha256")
                envelope = project_path(root, config, nav["envelope"])
                policy = project_path(root, config, nav["runtime_policy"])
                validation = validate_navigation(envelope, policy, expected_model_hash)
                if validation.get("status") != "PASS":
                    raise WorkflowError(f"Configured adopted navigation failed technical validation: {validation}")
            else:
                raise WorkflowError("Public release review requires a configured navigation artifact")
        else:
            decisions = {key: (value or "not-approved") for key, value in decisions.items()}
        status = "APPROVED" if args.decision == "approve" else "REJECTED"
        decision = {"decision": args.decision.upper(), "reviewer": reviewer, "rationale": rationale,
                    "reviewed_at_utc": timestamp, "candidate_id": current["candidate_id"],
                    "candidate": current, "review_decisions": decisions,
                    "test_only": bool(config.get("diagnostic_policy", {}).get("mode") == "SMOKE_TEST_ONLY"),
                    "deployment_authorized": False,
                    "scope": "This record covers this exact local static package; it is not a deployment action."}
        state.db.execute("INSERT INTO review_gates(gate_id,status,required,updated_at,decision_json) VALUES(?,?,?,?,?) ON CONFLICT(gate_id) DO UPDATE SET status=excluded.status,required=excluded.required,updated_at=excluded.updated_at,decision_json=excluded.decision_json",
                         ("publication-rights-review", status, 1, timestamp, json.dumps(decision, sort_keys=True)))
        state.db.execute("INSERT INTO review_history(created_at,gate_id,reviewer,decision,payload_json) VALUES(?,?,?,?,?)",
                         (timestamp, "publication-rights-review", reviewer, args.decision.upper(), json.dumps(decision, sort_keys=True)))
        state.db.commit()
        print(json.dumps({"gate_id": "publication-rights-review", "status": status,
                          "public_release_ready": status == "APPROVED", "candidate_id": current["candidate_id"],
                          "deployment_authorized": False, "reviewer": reviewer,
                          "scope": decision["scope"]}, indent=2))
        return 0
    finally:
        state.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="exhibition-archive", description="Local-first capture-to-archive workflow engine")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init", help="Create a versioned project manifest and SQLite state store")
    p.add_argument("photos", nargs="?", help="Read-only source photo directory")
    p.add_argument("--project", help="Stable project ID")
    p.add_argument("--workspace", help="Workspace directory; defaults to exhibition-archive/projects/<project-id>")
    p.add_argument("--config", help="Complete generic project manifest JSON")
    p.set_defaults(func=cmd_init)
    p = sub.add_parser("plan", help="Show workflow stages, dependencies, readiness, and configured adapters")
    p.add_argument("project"); p.add_argument("--full-hash", action="store_true", help="Force-hash inputs and show the exact plan identity required for fresh SfM/training authorization"); p.set_defaults(func=cmd_plan)
    p = sub.add_parser("authorize", help="Record a one-time operator approval for the current fresh SfM or training plan")
    p.add_argument("project"); p.add_argument("--stage", choices=["sfm", "training"], required=True)
    p.add_argument("--plan-id", required=True); p.add_argument("--reviewer", required=True); p.add_argument("--rationale", required=True)
    p.set_defaults(func=cmd_authorize)
    p = sub.add_parser("run", help="Run ready local stages and adopt configured existing artifacts")
    p.add_argument("project"); p.add_argument("--full-hash", action="store_true", help="Re-hash all fingerprinted input files")
    p.add_argument("--authorization-id", action="append", default=[], help="One-time grant returned by authorize; repeat for separately approved stages")
    p.add_argument("--fail-at", choices=["viewer_package", "archive_package", "verification"], help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_run, resume=False)
    p = sub.add_parser("diagnostic-smoke-train", help="Run one explicitly authorized, isolated SMOKE_TEST_ONLY training attempt")
    p.add_argument("project", help="Isolated diagnostic project directory")
    p.add_argument("--authorize-gpu-training", action="store_true", required=True,
                   help="Confirm operator authorization for this project's bounded GPU smoke run")
    p.set_defaults(func=cmd_diagnostic_smoke_train)
    p = sub.add_parser("diagnostic-smoke-preflight", help="Validate an isolated smoke project and write readiness evidence without training")
    p.add_argument("project", help="Isolated diagnostic project directory")
    p.add_argument("--authorize-gpu-training", action="store_true", required=True,
                   help="Confirm operator authorization for the planned bounded GPU smoke run")
    p.set_defaults(func=cmd_diagnostic_smoke_preflight)
    p = sub.add_parser("diagnostic-smoke-validate", help="Revalidate and reconcile a completed smoke attempt without launching training")
    p.add_argument("project", help="Isolated diagnostic project directory")
    p.add_argument("attempt_id", help="Unique completed smoke attempt ID")
    p.set_defaults(func=cmd_diagnostic_smoke_validate)
    p = sub.add_parser("resume", help="Resume failed/invalidated stages while reusing valid upstream results")
    p.add_argument("project"); p.add_argument("--full-hash", action="store_true")
    p.add_argument("--authorization-id", action="append", default=[], help="One-time grant returned by authorize; repeat for separately approved stages")
    p.set_defaults(func=cmd_run, resume=True)
    p = sub.add_parser("status", help="Inspect durable workflow state and resume eligibility")
    p.add_argument("project"); p.set_defaults(func=cmd_status)
    p = sub.add_parser("package", help="Run packaging and verification stages without rerunning SfM or training")
    p.add_argument("project"); p.add_argument("--full-hash", action="store_true")
    p.set_defaults(func=cmd_run, resume=True, package_only=True)
    p = sub.add_parser("verify", help="Verify archive checksums, public-site path policy, and model identity")
    p.add_argument("project"); p.add_argument("--full-hash", action="store_true", help="Re-hash every source photo and both model copies")
    p.add_argument("--public-release", action="store_true", help="Also require current durable publication-rights approval for this exact package")
    p.set_defaults(func=cmd_verify)
    p = sub.add_parser("publication-review", help="Record human publication-rights review for one exact local static package")
    p.add_argument("project"); p.add_argument("--decision", choices=["approve", "reject"], required=True)
    p.add_argument("--reviewer", required=True); p.add_argument("--rationale", required=True)
    p.add_argument("--model-selection", choices=["accepted", "not-approved"])
    p.add_argument("--navigation-approval", choices=["approved", "not-approved"])
    p.add_argument("--artwork-metadata", choices=["confirmed-only", "empty-catalogue-approved", "not-approved"])
    p.add_argument("--image-rights", choices=["approved", "not-approved"])
    p.add_argument("--artwork-rights", choices=["approved", "not-approved"])
    p.add_argument("--access-policy", choices=["approved", "not-approved"])
    p.add_argument("--public-private-policy", choices=["approved", "not-approved"])
    p.set_defaults(func=cmd_publication_review)
    p = sub.add_parser("config-set", help="Change a project configuration value and selectively invalidate affected stages")
    p.add_argument("project"); p.add_argument("--key", required=True); p.add_argument("--value", required=True); p.set_defaults(func=cmd_config_set)
    p = sub.add_parser("review", help="Start the local workflow and configured review interfaces")
    p.add_argument("project"); p.add_argument("--port", type=int, default=4188); p.set_defaults(func=cmd_review)
    p = sub.add_parser("stop-review", help="Stop the local review interface")
    p.add_argument("project"); p.set_defaults(func=cmd_stop_review)
    p = sub.add_parser("reset-stage", help="Start a new bounded retry window for a corrected failed stage")
    p.add_argument("project"); p.add_argument("stage", choices=[x["id"] for x in STAGES]); p.add_argument("--reason", required=True); p.set_defaults(func=cmd_reset_stage)
    p = sub.add_parser("cancel", help="Gracefully cancel an active fresh SfM or training process")
    p.add_argument("project"); p.add_argument("stage", choices=["sfm", "training"]); p.add_argument("--grace-seconds", type=float, default=8.0); p.set_defaults(func=cmd_cancel)
    p = sub.add_parser("logs", help="Show the most recent lines from a fresh backend stage log")
    p.add_argument("project"); p.add_argument("stage", choices=["sfm", "training"]); p.add_argument("--lines", type=int, default=80)
    p.set_defaults(func=cmd_logs)
    p = sub.add_parser("review-model", help="Record an explicit human decision for a fresh candidate PLY")
    p.add_argument("project"); p.add_argument("--candidate-sha256", required=True)
    p.add_argument("--decision", choices=["approve", "reject"], required=True)
    p.add_argument("--reviewer", required=True); p.add_argument("--rationale", required=True)
    p.set_defaults(func=cmd_review_model)
    p = sub.add_parser("sfm-review", help="Record an artifact-bound decision for one COLMAP reconstruction component")
    p.add_argument("project"); p.add_argument("--component", required=True, help="Component ID relative to the reconstruction directory, such as 0")
    p.add_argument("--decision", choices=["accept-experimental-training", "reject", "defer"], required=True)
    p.add_argument("--reviewer", required=True); p.add_argument("--rationale", required=True)
    p.add_argument("--limitation", action="append", default=[], help="Unresolved limitation; required once or more for conditional acceptance")
    p.set_defaults(func=cmd_sfm_review)
    p = sub.add_parser("navigation", help="Assess, generate, validate, inspect, or review navigation geometry")
    nav = p.add_subparsers(dest="navigation_action", required=True)
    for action, help_text in (("assess", "Inspect configured geometry and navigation readiness"),
                              ("generate", "Run the cache-aware workflow through fresh navigation generation"),
                              ("status", "Show proposal, approval, and publishing state")):
        q = nav.add_parser(action, help=help_text); q.add_argument("project"); q.set_defaults(func=cmd_navigation)
    q = nav.add_parser("validate", help="Validate a generated or reviewed navigation artifact")
    q.add_argument("project"); q.add_argument("--artifact", help="Optional navigation JSON path; defaults to the current proposal")
    q.set_defaults(func=cmd_navigation)
    q = nav.add_parser("review", help="Record an explicit, auditable navigation approval or rejection")
    q.add_argument("project"); q.add_argument("--decision", choices=["approve", "reject"], required=True)
    q.add_argument("--reviewer", required=True); q.add_argument("--rationale", required=True)
    q.add_argument("--artifact", help="Optional corrected navigation JSON downloaded from the review page")
    q.set_defaults(func=cmd_navigation)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try: return int(args.func(args))
    except Exception as exc:
        print(f"exhibition-archive: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
