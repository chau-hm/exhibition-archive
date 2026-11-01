"""Read-only storage inventory for an Engine project workspace."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .backends import IMAGE_SUFFIXES, RAW_IMAGE_SUFFIXES, discover_checkpoints
from .dataset_prep import PREP_DIR, PREP_MANIFEST, SRGB_PREP_DIR
from .workflow import output_path, project_path


def _measure(paths: list[Path], image_suffixes: set[str] | None = None) -> dict[str, Any]:
    files: dict[str, int] = {}
    symlinks = 0
    present = []
    for base in paths:
        if not base.exists() and not base.is_symlink():
            continue
        present.append(str(base))
        candidates = [base] if base.is_file() or base.is_symlink() else base.rglob("*")
        for path in candidates:
            if path.is_symlink():
                symlinks += 1
                continue
            if not path.is_file() or (image_suffixes is not None and path.suffix.casefold() not in image_suffixes):
                continue
            try:
                files[str(path.resolve())] = path.stat().st_size
            except OSError:
                continue
    return {"paths": present, "file_count": len(files), "symlink_count": symlinks,
            "size_bytes": sum(files.values()), "_files": files}


def _category(category_id: str, classification: str, measurement: dict[str, Any], *, additive: bool,
              note: str, overlaps: list[str] | None = None) -> dict[str, Any]:
    return {"id": category_id, "classification": classification,
            "paths": measurement["paths"], "file_count": measurement["file_count"],
            "symlink_count": measurement["symlink_count"], "size_bytes": measurement["size_bytes"],
            "additive": additive, "overlaps_with": overlaps or [], "note": note,
            "_files": measurement["_files"]}


def _within_roots(path: Path, roots: list[Path]) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return any(resolved == base or base in resolved.parents for base in roots)


def _direct_real_directories(parent: Path) -> list[Path]:
    """List direct directories without following a symlinked parent or child."""
    if parent.is_symlink() or not parent.is_dir():
        return []
    try:
        resolved_parent = parent.resolve()
        children = list(parent.iterdir())
    except OSError:
        return []
    result = []
    for child in children:
        if child.is_symlink() or not child.is_dir():
            continue
        try:
            if child.resolve().parent == resolved_parent:
                result.append(child)
        except OSError:
            continue
    return sorted(result)


def _readonly_database_uri(path: Path) -> str:
    """Percent-encode URI-reserved characters in workspace paths for SQLite."""
    return f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"


def _validated_artifact_paths(root: Path, allowed_roots: list[Path]) -> list[Path]:
    database = root / "workflow.sqlite3"
    if not database.is_file():
        return []
    try:
        connection = sqlite3.connect(_readonly_database_uri(database), uri=True)
        try:
            rows = connection.execute("SELECT path FROM artifacts WHERE status='VALIDATED'").fetchall()
            return [path for row in rows if _within_roots(path := Path(row[0]), allowed_roots)]
        finally:
            connection.close()
    except sqlite3.Error:
        return []


def _review_referenced_paths(root: Path, allowed_roots: list[Path]) -> list[Path]:
    database = root / "workflow.sqlite3"
    if not database.is_file():
        return []
    found = []
    try:
        connection = sqlite3.connect(_readonly_database_uri(database), uri=True)
        try:
            rows = connection.execute("SELECT gate_id,status,decision_json FROM review_gates").fetchall()
            for gate_id, status, raw in rows:
                if status not in {"APPROVED", "ACCEPTED_FOR_EXPERIMENTAL_TRAINING"}:
                    continue
                try:
                    decision = json.loads(raw or "{}")
                except ValueError:
                    continue
                path = decision.get("candidate_path")
                if isinstance(path, str) and path.strip() and _within_roots(Path(path), allowed_roots):
                    found.append(Path(path))
                selected = decision.get("selected_component")
                path = selected.get("directory") if isinstance(selected, dict) else None
                if isinstance(path, str) and path.strip() and _within_roots(Path(path), allowed_roots):
                    found.append(Path(path))
        finally:
            connection.close()
    except sqlite3.Error:
        return []
    return found


def _current_prepared_directory(root: Path) -> Path | None:
    manifest_path = root / PREP_MANIFEST
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8")).get("effective_image_directory")
        return Path(value).resolve() if isinstance(value, str) else None
    except (OSError, ValueError):
        return None


def storage_report(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Return measured storage groups and non-destructive cleanup candidates."""
    root = root.resolve()
    output = output_path(root, config)
    source = project_path(root, config, config["source"]["photo_directory"])
    allowed_artifact_roots = [root.resolve(), output.resolve(), source.resolve()]
    for group, key in (("sfm", "directory"), ("model", "path")):
        configured = config.get("integrations", {}).get(group, {}).get(key)
        if configured:
            allowed_artifact_roots.append(project_path(root, config, configured))
    source_suffixes = IMAGE_SUFFIXES | RAW_IMAGE_SUFFIXES
    prepared_dirs = [root / PREP_DIR, root / SRGB_PREP_DIR]
    training_root = output / "backend-runs" / "training"
    smoke_training_root = output / "backend-runs" / "smoke-training"
    attempt_root = output / "backend-runs"
    cache_dirs = [root / ".cache", output / "cache", output / "caches"]
    backend_cache_dirs = []
    for stage_dir in _direct_real_directories(attempt_root):
        for attempt_dir in _direct_real_directories(stage_dir):
            attempt_resolved = attempt_dir.resolve()
            for path in attempt_dir.rglob("*"):
                if path.is_symlink() or not path.is_dir() or "cache" not in path.name.casefold():
                    continue
                try:
                    resolved = path.resolve()
                    if resolved == attempt_resolved or attempt_resolved in resolved.parents:
                        backend_cache_dirs.append(path)
                except OSError:
                    continue
    archive_dirs = [output / "archive", output / "website"]
    audit_paths = [root / "workflow.sqlite3", root / "workflow.sqlite3-wal",
                   root / "workflow.sqlite3-shm", output / "work"]

    categories = [
        _category("source_images", "PERMANENT_PROTECTED",
                  _measure([source], source_suffixes), additive=True,
                  note="Read-only original image payload; never automatically removed."),
        _category("prepared_derivatives", "WORKING_RETAIN_AS_NEEDED",
                  _measure(prepared_dirs), additive=True,
                  note="Verified JPEG derivatives and preparation provenance; active manifest target is in use."),
        _category("backend_attempts", "WORKING_RETAIN_AS_NEEDED",
                  _measure([attempt_root]), additive=True,
                  note="SfM and training attempts, logs, diagnostics, datasets, checkpoints and intermediate outputs."),
        _category("caches", "REGENERABLE_CLEANUP_CANDIDATE",
                  _measure(cache_dirs), additive=True,
                  note="Known project-local cache directories; inspect references before any manual cleanup."),
        _category("workflow_audit", "PERMANENT_PROTECTED",
                  _measure(audit_paths), additive=True,
                  note="Workflow database and work reports include required audit, review, input and provenance evidence."),
        _category("final_archive_deliverables", "PERMANENT_PROTECTED",
                  _measure(archive_dirs), additive=True,
                  note="Final private archive and deployable static website; package generation does not copy backend attempts."),
    ]

    training_attempts = [(stage, attempt)
                         for stage, stage_root in (("training", training_root),
                                                   ("smoke-training", smoke_training_root))
                         for attempt in _direct_real_directories(stage_root)]
    dataset_paths = [path for _stage, attempt in training_attempts
                     for path in _direct_real_directories(attempt) if path.name == "dataset"]
    categories.append(_category("training_datasets", "WORKING_RETAIN_AS_NEEDED",
                                _measure(dataset_paths), additive=False,
                                overlaps=["backend_attempts"],
                                note="Subset of attempts. Symlink targets are not followed or counted as duplicate image payload."))
    categories.append(_category("backend_caches", "REGENERABLE_CLEANUP_CANDIDATE",
                                _measure(backend_cache_dirs), additive=False,
                                overlaps=["backend_attempts"],
                                note="Backend caches retained within attempts; verify whether the active run or recovery depends on them."))

    checkpoint_candidates = []
    for stage, attempt in training_attempts:
        run_roots = [path for path in _direct_real_directories(attempt) if path.name == "outputs"]
        for run_root in run_roots:
            for item in discover_checkpoints(run_root):
                checkpoint_candidates.append({**item, "stage": stage, "attempt": str(attempt)})
    checkpoint_files = sorted({Path(item["path"]) for item in checkpoint_candidates})
    categories.append(_category("checkpoints", "WORKING_RETAIN_AS_NEEDED",
                                _measure(checkpoint_files), additive=False,
                                overlaps=["backend_attempts"],
                                note="Backend-discovered candidates; Engine never prunes the active recovery checkpoint."))

    artifact_paths = [path for path in _validated_artifact_paths(root, allowed_artifact_roots) if path.exists()]
    categories.append(_category("validated_artifacts", "PERMANENT_PROTECTED",
                                _measure(artifact_paths), additive=False,
                                overlaps=["backend_attempts", "workflow_audit", "final_archive_deliverables"],
                                note="Current artifact registry records technical validation; this label does not imply human or publication acceptance."))
    review_paths = [path for path in _review_referenced_paths(root, allowed_artifact_roots) if path.exists()]
    categories.append(_category("review_referenced_artifacts", "PERMANENT_PROTECTED",
                                _measure(review_paths), additive=False,
                                overlaps=["backend_attempts", "final_archive_deliverables"],
                                note="Paths referenced by positive review records; storage report does not revalidate approval freshness or scope."))

    additive_files: dict[str, int] = {}
    for item in categories:
        if item["additive"]:
            additive_files.update(item["_files"])
    current_prepared = _current_prepared_directory(root)
    cleanup_candidates = []
    for prepared_root in prepared_dirs:
        for dataset in _direct_real_directories(prepared_root):
            if not dataset.name.startswith(".staging-") and (current_prepared is None or dataset.resolve() != current_prepared):
                cleanup_candidates.append({"path": str(dataset), "kind": "unreferenced_prepared_derivative_set",
                                           "safe_to_delete": False, "review": "Confirm no historical SfM plan or provenance record references it."})
    for path in cache_dirs:
        if path.exists():
            cleanup_candidates.append({"path": str(path), "kind": "project_cache_directory",
                                       "safe_to_delete": False, "review": "Inspect cache ownership and active process references before manual removal."})
    for path in backend_cache_dirs:
        cleanup_candidates.append({"path": str(path), "kind": "backend_cache_directory",
                                   "safe_to_delete": False, "review": "Verify the backend attempt is inactive and no recovery or audit record depends on this cache."})
    for stage, attempt in training_attempts:
        datasets = [path for path in _direct_real_directories(attempt) if path.name == "dataset"]
        for dataset in datasets:
            cleanup_candidates.append({"path": str(dataset), "kind": f"retained_{stage}_dataset_workspace",
                                       "safe_to_delete": False, "review": "Retain while its attempt or recovery checkpoint is active; otherwise check provenance references."})
    for item in checkpoint_candidates:
        cleanup_candidates.append({"path": item["path"], "kind": "backend_checkpoint_candidate",
                                   "safe_to_delete": False, "review": "Let Spirula apply latest-only retention; do not manually remove a valid recovery checkpoint."})
    newest_checkpoint_by_attempt = {}
    for item in checkpoint_candidates:
        current = newest_checkpoint_by_attempt.get(item["attempt"])
        if current is None or (item["mtime_ns"], item["path"]) > (current["mtime_ns"], current["path"]):
            newest_checkpoint_by_attempt[item["attempt"]] = item
    for item in checkpoint_candidates:
        newest = newest_checkpoint_by_attempt[item["attempt"]]
        if item["path"] != newest["path"]:
            cleanup_candidates.append({"path": item["path"], "kind": "superseded_checkpoint_candidate",
                                       "safe_to_delete": False, "review": "Confirm the backend's current recovery point and attempt history before any removal."})
    png_files = set()
    for item in categories:
        if item["id"] in {"backend_attempts", "prepared_derivatives"}:
            png_files.update(path for path in item["_files"] if Path(path).suffix.casefold() == ".png")
    for path in sorted(png_files):
        cleanup_candidates.append({"path": path, "kind": "temporary_or_diagnostic_png_candidate",
                                   "safe_to_delete": False, "review": "Verify it is not source material, active conversion input, or current diagnostic evidence."})
    for stage, attempt in training_attempts:
        cleanup_candidates.append({"path": str(attempt), "kind": f"retained_{stage}_attempt_review",
                                   "safe_to_delete": False, "review": "Check accepted model references, audit history, and recovery needs before manual cleanup."})

    public = [{key: value for key, value in item.items() if key != "_files"} for item in categories]
    return {"schema_version": "exhibition-archive.storage-report.v1", "project_id": config["project_id"],
            "workspace": str(root), "source_photo_directory": str(source),
            "accounting": {"additive_category_unique_size_bytes": sum(additive_files.values()),
                           "note": "Measured regular-file bytes; symlinks are counted separately and targets are not followed. Overlapping subset categories are marked additive=false."},
            "categories": public, "checkpoint_candidates": checkpoint_candidates,
            "cleanup_candidates": cleanup_candidates,
            "archive_optimization_target": {"ratio_to_original_photo_payload": 10,
                                             "policy": "INITIAL_OPTIMIZATION_TARGET_NOT_A_DELETION_LIMIT"}}
