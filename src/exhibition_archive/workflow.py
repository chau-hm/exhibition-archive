from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .adapters import colmap_binary_counts, ply_identity, read_json, sha256_file, validate_navigation, write_json
from .navigation import generate_navigation, read_colmap_reconstruction, render_navigation_review_html
from .backends import (BackendError, CHECKPOINT_REPLACEMENT_OVERLAP_ASSUMPTION, IMAGE_SUFFIXES, build_sfm_command, build_training_command,
                       check_resources, create_attempt_dir, discover_checkpoints, discover_environment, discover_model_files,
                       execute_process, finalize_attempt_record, finalize_stage_attempt, image_inventory as backend_image_inventory, prepare_training_dataset,
                       parse_spirula_sfm_reprojection_summary, training_preset_configuration,
                       process_alive,
                       validate_colmap_directory, validate_gaussian_ply)
from .dataset_prep import (DATASET_MANIFEST, DatasetPreparationRequired, auto_preparation_required,
                           contains_heif, heif_converter_identity,
                           effective_sfm_dataset, effective_training_dataset, raw_sfm_image_directory,
                           image_gamut_mismatch, preparation_manifest_path,
                           prepare_spirula_dataset, preparation_settings_for,
                           validate_preparation_stage_outputs)
from .lidar import (attempt_similarity_alignment, discover_input_files,
                    inspect_optional_lidar, propose_lidar_ground_plane,
                    read_ply_xyz_sample)
from .state import ProjectState, utc_now


STAGES = [
    {"id": "photo_inventory", "deps": [], "inputs": ["original photographs", "Phase2EU inventory if supplied"], "outputs": ["source inventory and hashes"], "mode": "execute locally", "resource": "I/O bound"},
    {"id": "lidar_inspection", "deps": ["photo_inventory"], "inputs": ["optional non-Gaussian PLY candidates"], "outputs": ["PLY inspection report, raw preview, and discovery manifest"], "mode": "optional; failures recover to photo-only", "resource": "bounded I/O"},
    {"id": "capture_preflight", "deps": ["photo_inventory"], "inputs": ["image inventory", "optional SfM and region evidence"], "outputs": ["quality and capture-risk report"], "mode": "adopt or image-only adapter", "resource": "low / I/O bound"},
    {"id": "sfm_dataset_prep", "deps": ["photo_inventory", "capture_preflight"], "inputs": ["registered source inventory", "HEIC files when fresh Spirula SfM requires JPEG"], "outputs": ["verified effective image dataset and source-to-derivative manifest"], "mode": "safe local preparation; no backend execution", "resource": "I/O and CPU bound when HEIC is present"},
    {"id": "artwork_association", "deps": ["photo_inventory", "capture_preflight"], "inputs": ["image inventory", "optional geometry and review state"], "outputs": ["inventory, observations, association graph"], "mode": "adopt or configured Phase2EV adapter", "resource": "low when adopted"},
    {"id": "sfm", "deps": ["photo_inventory"], "inputs": ["source images or existing COLMAP reconstruction"], "outputs": ["validated cameras, registered images, sparse points and provenance"], "mode": "adopt_existing or spirula_fresh", "resource": "high when freshly executed"},
    {"id": "sfm_studio_export", "deps": ["photo_inventory", "sfm"], "inputs": ["effective SfM image dataset", "validated COLMAP sparse model"], "outputs": ["portable images/ + sparse/0/ COLMAP dataset and manifest"], "mode": "local copy; export failure is recoverable", "resource": "I/O bound"},
    {"id": "lidar_registration", "deps": ["photo_inventory", "lidar_inspection", "sfm"], "inputs": ["independently inspected raw point cloud and validated sparse SfM geometry"], "outputs": ["scale-aware alignment report and optional aligned preview"], "mode": "optional FPFH/RANSAC global registration; rejection is recoverable", "resource": "bounded CPU geometry"},
    {"id": "navigation", "deps": ["sfm", "lidar_registration"], "inputs": ["trusted camera poses, optional validated aligned LiDAR geometry, coordinate frame and review decisions"], "outputs": ["versioned navigation proposal or approved artifact"], "mode": "adopt_existing or generate_from_sfm", "resource": "low"},
    {"id": "training", "deps": ["photo_inventory", "sfm"], "inputs": ["images and validated SfM"], "outputs": ["validated candidate 3DGS PLY and training provenance"], "mode": "adopt_existing or spirula_fresh", "resource": "GPU intensive"},
    {"id": "first_visual", "deps": ["training"], "inputs": ["unreviewed candidate model, SfM, LiDAR and navigation diagnostics"], "outputs": ["private technical Reviewer and Visitor preview HTML"], "mode": "execute locally; no release approval", "resource": "model copy and local asset assembly"},
    {"id": "evaluation", "deps": ["training", "artwork_association"], "inputs": ["candidate model and fidelity evidence"], "outputs": ["model selection and limitations"], "mode": "adopt evidence / human gate" , "resource": "review dependent"},
    {"id": "viewer_package", "deps": ["training", "navigation", "evaluation", "artwork_association"], "inputs": ["selected model, navigation, confirmed catalogue, theme"], "outputs": ["independent static Review and Visitor websites"], "mode": "execute locally", "resource": "model copy and optional SOG conversion"},
    {"id": "archive_package", "deps": ["photo_inventory", "capture_preflight", "artwork_association", "sfm", "training", "evaluation", "navigation", "viewer_package"], "inputs": ["validated evidence and package"], "outputs": ["private archive metadata and manifest"], "mode": "execute locally", "resource": "low"},
    {"id": "verification", "deps": ["archive_package"], "inputs": ["website, manifest, checksums, policies"], "outputs": ["verification report"], "mode": "execute locally", "resource": "I/O bound"},
]
FIRST_VISUAL_STAGE_ORDER = ("photo_inventory", "lidar_inspection", "capture_preflight",
                           "sfm_dataset_prep", "artwork_association", "sfm",
                           "sfm_studio_export", "lidar_registration", "navigation",
                           "training", "first_visual")


class WorkflowError(RuntimeError):
    pass


class AuthorizationRequired(WorkflowError):
    """A fresh expensive backend stage has no current one-time approval."""

    def __init__(self, stage_id: str, message: str, plan: dict | None = None):
        super().__init__(message)
        self.stage_id = stage_id
        self.plan = plan


def resolve_project(value: str | Path) -> Path:
    p = Path(value).expanduser()
    if p.is_dir():
        config = p / "project.json"
        if config.is_file():
            return config.resolve()
    if p.is_file() and p.name.endswith(".json"):
        return p.resolve()
    candidate = Path(__file__).resolve().parents[2] / "projects" / str(value) / "project.json"
    if candidate.is_file():
        return candidate.resolve()
    raise FileNotFoundError(f"No project.json found for {value}")


def load_project(value: str | Path) -> tuple[Path, dict[str, Any]]:
    config_path = resolve_project(value)
    config = read_json(config_path)
    if config.get("schema_version") != "exhibition-archive.project.v1" or not config.get("project_id"):
        raise ValueError("Project config must use exhibition-archive.project.v1 and include project_id")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,127}", str(config["project_id"])):
        raise ValueError("project_id must be 2–128 letters, digits, dots, underscores, or hyphens and start with a letter or digit")
    if not isinstance(config.get("source", {}).get("photo_directory"), str) or not config["source"]["photo_directory"].strip():
        raise ValueError("Project config must include source.photo_directory")
    if not isinstance(config.get("output_directory", "output"), str) or not config.get("output_directory", "output").strip() or not isinstance(config.get("viewer", {}), dict):
        raise ValueError("Project output_directory and viewer settings have invalid types")
    if config.get("source", {}).get("read_only", True) is not True:
        raise ValueError("Project source.photo_directory must be read-only")
    if config.get("archive_policy", {}).get("bundle_source_photographs"):
        raise ValueError("Source-photo bundling is not implemented by this MVP")
    if config.get("publication", {}).get("include_source_photographs") or config.get("publication", {}).get("include_private_review_notes"):
        raise ValueError("The static publisher excludes source photographs and private review notes")
    return config_path.parent, config


def project_path(root: Path, config: dict, value: str | Path) -> Path:
    p = Path(value).expanduser()
    return (p if p.is_absolute() else root / p).resolve()


def output_path(root: Path, config: dict) -> Path:
    return project_path(root, config, config.get("output_directory", "output"))


def json_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def training_resume_identity(root: Path, config: dict, model_cfg: dict) -> tuple[Path | None, dict | None]:
    """Resolve and full-hash an explicitly selected prior Spirula run for --resume."""
    configured = model_cfg.get("resume_from")
    if configured is None:
        return None, None
    if not isinstance(configured, str) or not configured.strip():
        raise WorkflowError("model.resume_from must be a non-empty path to a prior training output directory")
    raw = Path(configured).expanduser()
    candidate = (raw if raw.is_absolute() else root / raw)
    if candidate.is_symlink():
        raise WorkflowError("model.resume_from may not be a symlink")
    resume_dir = candidate.resolve()
    allowed_root = (output_path(root, config) / "backend-runs" / "training").resolve()
    if allowed_root not in resume_dir.parents or not resume_dir.is_dir():
        raise WorkflowError("model.resume_from must name an existing training output directory inside this project's output/backend-runs/training")
    resume_entries = list(resume_dir.rglob("*"))
    if any(path.is_symlink() for path in resume_entries):
        raise WorkflowError("model.resume_from may not contain symlinks")
    existing_models = [path for path in resume_entries if path.suffix.casefold() == ".ply"]
    if existing_models:
        raise WorkflowError("Refusing to resume in place from a run directory containing a model PLY; preserve the existing candidate and prepare a separate clean resume directory")
    candidates = discover_checkpoints(resume_dir)
    if not candidates:
        raise WorkflowError("model.resume_from contains no Spirula checkpoint candidates")
    identities = []
    for item in candidates:
        checkpoint = Path(item["path"])
        if checkpoint.is_symlink():
            raise WorkflowError("Checkpoint candidates may not be symlinks")
        if checkpoint.is_file():
            identities.append({**item, "sha256": sha256_file(checkpoint)})
            continue
        files = []
        for child in sorted(checkpoint.rglob("*")):
            if child.is_symlink():
                raise WorkflowError(f"Checkpoint directory contains a symlink: {child}")
            if child.is_file():
                files.append({"relative_path": child.relative_to(checkpoint).as_posix(),
                              "size_bytes": child.stat().st_size, "sha256": sha256_file(child)})
        if not files:
            raise WorkflowError(f"Checkpoint directory contains no regular files: {checkpoint}")
        identities.append({**item, "size_bytes": sum(row["size_bytes"] for row in files),
                           "sha256": json_digest(files), "files": files})
    newest = max(identities, key=lambda row: (row["mtime_ns"], row["path"]))
    return resume_dir, {"output_directory": str(resume_dir), "checkpoint_candidates": identities,
                        "newest_by_mtime": {"path": newest["path"], "sha256": newest["sha256"]},
                        "selection_semantics": "ADVISORY_MTIME; Spirula --resume selects its own latest checkpoint"}


def validate_training_resource_policy(config: dict, model_cfg: dict) -> dict[str, Any]:
    policy = config.get("resource_policy", {})
    maximum = policy.get("max_training_iterations")
    timeout = model_cfg.get("timeout_seconds", policy.get("training_timeout_seconds"))
    minimum_disk = policy.get("minimum_free_disk_bytes_training")
    replacement_reserve = policy.get("checkpoint_replacement_reserve_bytes")
    replacement_reserve_basis = policy.get("checkpoint_replacement_reserve_basis", "NOT_RECORDED")
    values = {"max_training_iterations": maximum, "training_timeout_seconds": timeout,
              "minimum_free_disk_bytes_training": minimum_disk,
              "checkpoint_replacement_reserve_bytes": replacement_reserve}
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 1 for value in values.values()):
        raise WorkflowError("Fresh training requires positive max_training_iterations, training timeout, minimum_free_disk_bytes_training, and checkpoint_replacement_reserve_bytes")
    if (not isinstance(replacement_reserve_basis, str) or
            replacement_reserve_basis not in {"OPERATOR_PROVISIONED", "MEASURED", "NOT_RECORDED"}):
        raise WorkflowError("resource_policy.checkpoint_replacement_reserve_basis must be OPERATOR_PROVISIONED, MEASURED, or NOT_RECORDED")
    if not isinstance(model_cfg.get("iterations"), int) or isinstance(model_cfg.get("iterations"), bool) or not 1 <= model_cfg["iterations"] <= maximum:
        raise WorkflowError("Fresh training iterations must be positive and at or below max_training_iterations")
    return {**{key: int(value) for key, value in values.items()},
            "checkpoint_replacement_reserve_basis": replacement_reserve_basis,
            "checkpoint_replacement_overlap_assumption": CHECKPOINT_REPLACEMENT_OVERLAP_ASSUMPTION,
            "minimum_required_at_launch_bytes": int(minimum_disk) + int(replacement_reserve)}


def fingerprint_file(state: ProjectState, path: Path, force: bool = False) -> dict:
    return {"path": str(path.resolve()), "exists": path.is_file(), "size_bytes": path.stat().st_size if path.is_file() else None,
            "sha256": state.cached_hash(path, force) if path.is_file() else None}


def _integration_path(root: Path, config: dict, group: str, key: str) -> Path:
    value = config.get("integrations", {}).get(group, {}).get(key)
    if not value:
        raise WorkflowError(f"Missing configured integration path: integrations.{group}.{key}")
    p = Path(value).expanduser()
    return (p if p.is_absolute() else root / p).resolve()


def stage_fingerprint(root: Path, config: dict, state: ProjectState, stage_id: str, full_hash: bool = False) -> str:
    integration = config.get("integrations", {})
    selected: dict[str, Any] = {"project_id": config["project_id"], "stage": stage_id}
    definition = next(x for x in STAGES if x["id"] == stage_id)
    dependencies = list(definition["deps"])
    if stage_id == "sfm" and auto_preparation_required(root, config):
        dependencies.append("sfm_dataset_prep")
    selected["dependencies"] = {dep: ({k: state.stage(dep).get(k) for k in ("status", "input_fingerprint", "output_digest")} if state.stage(dep) else None) for dep in dependencies}
    paths_by_stage = {
        "photo_inventory": [integration.get("preflight", {}).get("image_inventory")],
        "capture_preflight": [integration.get("preflight", {}).get(x) for x in ("summary", "risk_register", "recapture_recommendations")] + [integration.get("sfm", {}).get("directory")],
        "artwork_association": [integration.get("artwork", {}).get(x) for x in ("summary", "state", "graph", "confirmed_artworks")],
        "sfm": [integration.get("sfm", {}).get("directory"), integration.get("sfm", {}).get("derived_image_manifest")],
        "training": [integration.get("model", {}).get("path"), integration.get("sfm", {}).get("derived_image_manifest")],
        "evaluation": integration.get("evaluation", {}).get("evidence", []),
        "navigation": [integration.get("navigation", {}).get(x) for x in ("envelope", "runtime_policy", "camera_centers", "trusted_pose_classification", "floor_plane", "ceiling", "transitions_file")],
        "viewer_package": [integration.get("model", {}).get("path"), integration.get("navigation", {}).get("envelope"), integration.get("navigation", {}).get("runtime_policy"), integration.get("artwork", {}).get("confirmed_artworks")],
        "archive_package": [],
        "verification": [],
    }
    selected["files"] = []
    for raw in paths_by_stage.get(stage_id, []):
        if not raw:
            continue
        if isinstance(raw, dict): raw = raw.get("path")
        if not raw: continue
        path = project_path(root, config, raw)
        if path.is_dir():
            for n in ("cameras.bin", "images.bin", "points3D.bin"):
                selected["files"].append(fingerprint_file(state, path / n, full_hash))
        elif path.is_file():
            selected["files"].append(fingerprint_file(state, path, full_hash))
        else:
            selected["files"].append({"path": str(path), "exists": False})
    if stage_id == "navigation":
        selected["navigation_inputs"] = integration.get("navigation", {})
        selected["files"].append(fingerprint_file(state, Path(__file__).with_name("navigation.py"), full_hash))
    if stage_id == "lidar_inspection":
        source_dir = project_path(root, config, config["source"]["photo_directory"])
        discovery = discover_input_files(source_dir, hash_images=False, hash_ply=False)
        selected["lidar_enabled"] = integration.get("lidar", {}).get("enabled", True)
        selected["point_cloud_inputs"] = [
            fingerprint_file(state, source_dir / row["relative_path"], full_hash)
            for row in discovery.get("ply_files", [])
        ]
        selected["implementation_files"] = [
            fingerprint_file(state, Path(__file__).with_name("lidar.py"), full_hash),
        ]
    if stage_id == "sfm_studio_export":
        sfm_details = json.loads((state.stage("sfm") or {}).get("details_json") or "{}")
        reconstruction = sfm_details.get("reconstruction_directory") or integration.get("sfm", {}).get("directory")
        if reconstruction:
            reconstruction_path = Path(reconstruction).expanduser()
            if not reconstruction_path.is_absolute():
                reconstruction_path = root / reconstruction_path
            selected["reconstruction"] = str(reconstruction_path.resolve())
            selected["files"].extend(fingerprint_file(state, reconstruction_path / name, full_hash)
                                     for name in ("cameras.bin", "images.bin", "points3D.bin"))
        image_directory = sfm_details.get("effective_image_directory")
        if not image_directory:
            try:
                image_directory, prep_manifest = effective_sfm_dataset(root, config, validate_files=full_hash)
            except DatasetPreparationRequired:
                image_directory, prep_manifest = raw_sfm_image_directory(root, config), None
        else:
            image_directory = Path(image_directory).expanduser()
            prep_manifest = None
        selected["image_directory"] = str(Path(image_directory).resolve())
        if prep_manifest:
            selected["preparation_manifest"] = prep_manifest
        selected["implementation_files"] = [fingerprint_file(state, Path(__file__).with_name("studio_export.py"), full_hash)]
    if stage_id == "lidar_registration":
        selected["files"].append(fingerprint_file(state, output_path(root, config) / "lidar" / "inspection.json", full_hash))
        selected["files"].append(fingerprint_file(state, Path(__file__).with_name("lidar.py"), full_hash))
        sfm_details = json.loads((state.stage("sfm") or {}).get("details_json") or "{}")
        selected["sparse_reconstruction"] = {"path": sfm_details.get("reconstruction_directory"),
                                             "validation_path": sfm_details.get("sfm_validation_path")}
        try:
            from importlib.metadata import PackageNotFoundError, version
            selected["registration_dependency"] = {"name": "open3d", "version": version("open3d"),
                                                   "status": "AVAILABLE"}
        except PackageNotFoundError:
            selected["registration_dependency"] = {"name": "open3d", "version": None,
                                                   "status": "UNAVAILABLE_OPTIONAL"}
        if (state.stage("sfm") or {}).get("status") == "NEEDS_REVIEW":
            try:
                from .sfm_review import geometry_proposal_scope
                selected["geometry_review_scope"] = geometry_proposal_scope(root, config, state)
            except Exception as exc:
                selected["geometry_review_scope"] = {"status": "UNAVAILABLE", "reason": str(exc)}
    if stage_id == "viewer_package":
        viewer_source = Path(__file__).resolve().parents[2] / "templates" / "viewer"
        for name in ("app.js", "index.html", "style.css", "vendor/index.js", "vendor/index.css"):
            selected["files"].append(fingerprint_file(state, viewer_source / name, full_hash))
        visitor_source = Path(__file__).resolve().parents[2] / "templates" / "visitor"
        for name in ("app.js", "index.html", "style.css", "runtime.js", "locales.json",
                     "vendor/visitor-app.js", "vendor/supersplat-viewer.js", "vendor/supersplat-viewer.css",
                     "vendor/SUPERSPLAT-VIEWER-LICENSE.txt", "vendor/PLAYCANVAS-LICENSE.txt",
                     "vendor/THIRD_PARTY-NOTICES.md", "vendor/runtime.json"):
            selected["files"].append(fingerprint_file(state, visitor_source / name, full_hash))
        product_root = Path(__file__).resolve().parents[2]
        for path in (product_root / "scripts" / "build_visitor_viewer.mjs", product_root / "package.json", product_root / "package-lock.json"):
            selected["files"].append(fingerprint_file(state, path, full_hash))
        selected["files"].append(fingerprint_file(state, Path(__file__).with_name("publisher.py"), full_hash))
        selected["files"].append(fingerprint_file(state, Path(__file__).with_name("visitor.py"), full_hash))
        selected["files"].append(fingerprint_file(state, Path(__file__).with_name("adapters.py"), full_hash))
        selected["files"].append(fingerprint_file(state, Path(__file__).with_name("navigation.py"), full_hash))
        selected["visitor_sog_tool"] = {"name": "@playcanvas/splat-transform", "version": "3.10.3", "sh_iterations": 20,
                                        "webp_effort": 9, "gpu": "cpu", "max_workers": 4,
                                        "diagnostics": ["verbose", "memory"], "timeout_seconds": 600,
                                        "node_heap_mb": 8192}
    if stage_id == "archive_package":
        selected["files"].append(fingerprint_file(state, Path(__file__).with_name("workflow.py"), full_hash))
    if stage_id == "verification":
        selected["files"].append(fingerprint_file(state, Path(__file__).with_name("workflow.py"), full_hash))
    if stage_id == "photo_inventory":
        selected["source"] = config["source"]
        source_dir = project_path(root, config, config["source"]["photo_directory"])
        suffixes = IMAGE_SUFFIXES
        selected["source_files"] = [fingerprint_file(state, file, full_hash) for file in sorted(source_dir.rglob("*") if source_dir.is_dir() else []) if file.is_file() and file.suffix.casefold() in suffixes]
        selected["image_preparation"] = fingerprint_file(state, Path(__file__).with_name("input_prep.py"), full_hash)
    if stage_id == "sfm_dataset_prep":
        sfm_cfg = integration.get("sfm", {})
        image_dir = project_path(root, config, sfm_cfg.get("image_directory", config["source"]["photo_directory"]))
        prep_settings = preparation_settings_for(config)
        selected["source"] = config["source"]
        selected["preparation_settings"] = prep_settings
        selected["implementation_files"] = [
            fingerprint_file(state, Path(__file__).with_name("dataset_prep.py"), full_hash),
        ]
        if contains_heif(image_dir):
            # Use a valid prepared dataset's recorded toolchain when computing
            # its historical stage identity. Only a missing/stale preparation
            # needs the current converter identity for a future preparation.
            try:
                _, historical_manifest = effective_sfm_dataset(root, config, validate_files=full_hash)
            except (BackendError, DatasetPreparationRequired):
                historical_manifest = None
            if historical_manifest:
                selected["converter"] = historical_manifest.get("conversion", {}).get("backend")
            else:
                selected["converter"] = heif_converter_identity(prep_settings)
        selected["input_images"] = [fingerprint_file(state, file, full_hash)
                                     for file in sorted(image_dir.rglob("*") if image_dir.is_dir() else [])
                                     if file.is_file() and file.suffix.casefold() in IMAGE_SUFFIXES]
        selected["sfm_configuration"] = sfm_cfg
    if stage_id in {"sfm", "training"}:
        try:
            if stage_id == "sfm":
                image_dir, prep_manifest = effective_sfm_dataset(root, config, validate_files=full_hash)
            else:
                image_dir, prep_manifest = effective_training_dataset(root, config)
        except DatasetPreparationRequired:
            image_dir = raw_sfm_image_directory(root, config) if stage_id == "sfm" else project_path(
                root, config, integration.get("model", {}).get("image_directory",
                    integration.get("sfm", {}).get("image_directory", config["source"]["photo_directory"])))
            prep_manifest = None
            selected["dataset_preparation_required"] = True
        selected["selected_input_images"] = [fingerprint_file(state, file, full_hash) for file in sorted(image_dir.rglob("*")) if file.is_file() and file.suffix.casefold() in IMAGE_SUFFIXES] if image_dir.is_dir() else [{"path": str(image_dir), "exists": False}]
        if prep_manifest:
            prep_path = preparation_manifest_path(root)
            selected["dataset_preparation"] = {
                "manifest_path": str(prep_path.resolve()),
                "manifest_sha256": sha256_file(prep_path),
                "preparation_fingerprint": prep_manifest.get("preparation_fingerprint"),
                "mapping_sha256": json_digest(prep_manifest.get("image_mappings", [])),
                "mapped_image_count": len(prep_manifest.get("image_mappings", [])),
            }
        if stage_id == "training":
            model_cfg = integration.get("model", {})
            selected["training_configuration"] = {k: v for k, v in model_cfg.items() if k != "environment_overrides"}
            selected["training_environment_overrides_sha256"] = json_digest(model_cfg.get("environment_overrides", {}))
            if model_cfg.get("mode", "adopt_existing") == "spirula_fresh":
                from .sfm_review import training_component_scope
                try:
                    scope = training_component_scope(root, config, state)
                    selected["sfm_training_scope"] = {
                        "status": "CURRENT",
                        "component_id": scope["component_id"],
                        "component_artifact_tree_sha256": scope["component"]["artifact_tree_sha256"],
                        "registered_image_count": scope["registered_image_count"],
                        "registered_image_membership_sha256": scope["registered_image_membership_sha256"],
                        "reconstruction_binding_sha256": scope["sfm_snapshot"]["binding_sha256"],
                        "acceptance_audit_identity": (scope["acceptance"] or {}).get("audit_identity"),
                    }
                except (BackendError, KeyError, TypeError, ValueError) as exc:
                    selected["sfm_training_scope"] = {"status": "BLOCKED", "reason": str(exc)}
    elif stage_id == "viewer_package":
        selected["theme"] = config.get("viewer", {}).get("theme", "dark")
        selected["viewer_title"] = config.get("viewer", {}).get("title", config["project_id"])
        selected["geometry_views"] = {
            "public_geometry_view_ids": config.get("viewer", {}).get("public_geometry_view_ids", []),
            "default_geometry_view_id": config.get("viewer", {}).get("default_geometry_view_id"),
        }
        selected["public_policy"] = config.get("publication", {})
    elif stage_id == "first_visual":
        selected["training_candidate"] = ({k: state.stage("training").get(k) for k in ("status", "input_fingerprint", "output_digest")} if state.stage("training") else None)
        selected["training_configuration"] = {k: v for k, v in integration.get("model", {}).items() if k != "environment_overrides"}
        selected["diagnostic_stages"] = {sid: ({k: state.stage(sid).get(k) for k in ("status", "input_fingerprint", "output_digest")} if state.stage(sid) else None)
                                          for sid in ("sfm", "lidar_inspection", "lidar_registration", "navigation")}
        selected["navigation_configuration"] = integration.get("navigation", {})
        selected["implementation_files"] = [
            fingerprint_file(state, Path(__file__).with_name("preview.py"), full_hash),
            fingerprint_file(state, Path(__file__).with_name("lidar.py"), full_hash),
            fingerprint_file(state, Path(__file__).with_name("visitor.py"), full_hash),
            fingerprint_file(state, Path(__file__).resolve().parents[2] / "templates" / "visitor" / "index.html", full_hash),
            fingerprint_file(state, Path(__file__).resolve().parents[2] / "templates" / "visitor" / "app.js", full_hash),
            fingerprint_file(state, Path(__file__).resolve().parents[2] / "templates" / "visitor" / "style.css", full_hash),
        ]
    elif stage_id == "archive_package":
        selected["archive_policy"] = config.get("archive_policy", {})
        selected["publication"] = config.get("publication", {})
        selected["upstream"] = {s["id"]: {k: state.stage(s["id"]).get(k) for k in ("status", "input_fingerprint", "output_digest")} if state.stage(s["id"]) else None for s in STAGES if s["id"] not in {"archive_package", "verification"}}
    elif stage_id == "verification":
        selected["upstream"] = {s: ({k: state.stage(s).get(k) for k in ("status", "input_fingerprint", "output_digest")} if state.stage(s) else None) for s in ("viewer_package", "archive_package")}
    else:
        selected["config_group"] = integration.get({"photo_inventory":"preflight", "capture_preflight":"preflight", "sfm_dataset_prep":"sfm", "artwork_association":"artwork", "sfm":"sfm", "training":"model", "evaluation":"evaluation", "navigation":"navigation"}.get(stage_id, ""), {})
    return json_digest(selected)


def expensive_execution_plan(root: Path, config: dict, state: ProjectState, stage_id: str,
                             *, backend_info: dict | None = None) -> dict[str, Any]:
    """Build the consent identity for one bounded fresh backend attempt.

    Paths that vary only by the next generated attempt ID are normalized. Input
    files are force-hashed, so the consent identity changes when content changes
    even if size and timestamps are preserved.
    """
    if stage_id not in {"sfm", "training"}:
        raise WorkflowError(f"Execution authorization applies only to fresh sfm/training stages, not {stage_id!r}")
    group = "sfm" if stage_id == "sfm" else "model"
    cfg = config.get("integrations", {}).get(group, {})
    if cfg.get("mode", "adopt_existing") != "spirula_fresh":
        raise WorkflowError(f"Stage {stage_id} is configured to adopt existing artifacts and does not need GPU authorization")
    training_scope = None
    resume_output_dir = None
    resume_identity = None
    training_limits = None
    if stage_id == "training":
        from .sfm_review import training_component_scope
        try:
            training_scope = training_component_scope(root, config, state)
        except BackendError as exc:
            raise WorkflowError(f"Training authorization requires a current accepted SfM component: {exc}") from exc
        if cfg.get("run_type") not in {"smoke_test", "candidate"}:
            raise WorkflowError("Fresh training requires model.run_type to be explicitly smoke_test or candidate")
        training_limits = validate_training_resource_policy(config, cfg)
        resume_output_dir, resume_identity = training_resume_identity(root, config, cfg)

    from .backends import discover_environment
    backend = backend_info or discover_environment(cfg.get("executable"))
    executable = Path(backend["tool"]["executable"])
    attempt = output_path(root, config) / "backend-runs" / stage_id / "<next-unique-attempt>"
    input_cfg = config.get("integrations", {}).get("sfm", {})
    if stage_id == "sfm":
        image_dir, prep_manifest = effective_sfm_dataset(root, config, validate_files=True)
        command = build_sfm_command(executable, image_dir, attempt / "workspace", cfg, backend)
        limits = {**config.get("resource_policy", {}), "timeout_seconds": cfg.get("timeout_seconds", config.get("resource_policy", {}).get("sfm_timeout_seconds"))}
        quality_gates = cfg.get("quality_gate", {})
        backend_capabilities = backend.get("sfm", {})
    else:
        image_dir, prep_manifest = effective_training_dataset(root, config)
        dataset = attempt / "<prepared-dataset>"
        command = build_training_command(executable, dataset, attempt, cfg, backend,
                                         f"{config['project_id']}-<next-execution-id>", resume_output_dir)
        training_effective_configuration = training_preset_configuration(
            executable, cfg["preset"], cfg.get("quality", "high"), command)
        limits = {**config.get("resource_policy", {}), "timeout_seconds": cfg.get("timeout_seconds", config.get("resource_policy", {}).get("training_timeout_seconds")),
                  "max_training_iterations": config.get("resource_policy", {}).get("max_training_iterations"),
                  "iterations": cfg.get("iterations"),
                  "minimum_free_disk_bytes_training": training_limits["minimum_free_disk_bytes_training"],
                  "checkpoint_replacement_reserve_bytes": training_limits["checkpoint_replacement_reserve_bytes"],
                  "checkpoint_replacement_reserve_basis": training_limits["checkpoint_replacement_reserve_basis"],
                  "checkpoint_replacement_overlap_assumption": training_limits["checkpoint_replacement_overlap_assumption"],
                  "minimum_required_at_launch_bytes": training_limits["minimum_required_at_launch_bytes"]}
        quality_gates = {"sfm_quality_gate": input_cfg.get("quality_gate", {}), "training": cfg.get("quality_gate", {})}
        backend_capabilities = backend.get("training", {})
    gamut_error = image_gamut_mismatch(config, prep_manifest) if stage_id == "sfm" else None
    if gamut_error:
        raise BackendError(gamut_error, failure_kind="INVALID_CONFIGURATION")
    source_images = sorted(path for path in image_dir.rglob("*") if path.is_file() and path.suffix.casefold() in IMAGE_SUFFIXES) if image_dir.is_dir() else []
    if stage_id == "training":
        source_by_name = {row["relative_path"]: row for row in backend_image_inventory(image_dir)["images"]}
        expected_by_name = {row["relative_path"]: row for row in training_scope["registered_image_rows"]}
        if set(expected_by_name) - set(source_by_name):
            raise WorkflowError("Accepted COLMAP component contains names outside the current training image directory")
        if any(source_by_name[name]["sha256"] != row["sha256"] for name, row in expected_by_name.items()):
            raise WorkflowError("Accepted registered image hashes differ from the effective training image directory")
        input_images = [Path(source_by_name[name]["source_path"]) for name in training_scope["registered_image_names"]]
    else:
        input_images = source_images
    fingerprint = stage_fingerprint(root, config, state, stage_id, full_hash=True)
    selected_cfg = {k: v for k, v in cfg.items() if k != "environment_overrides"}
    env = cfg.get("environment_overrides", {})
    descriptor = {
        "schema_version": "exhibition-archive.execution-authorization-plan.v1",
        "project_id": config["project_id"], "stage": stage_id,
        "operation": "fresh_sfm" if stage_id == "sfm" else "fresh_training_candidate",
        "input_fingerprint_sha256": fingerprint,
        "input_dataset": str(dataset if stage_id == "training" else image_dir),
        "source_image_directory": str(image_dir),
        "source_image_count": len(input_images),
        "source_directory_image_count": len(source_images),
        "input_image_count": len(input_images),
        "input_image_extensions": sorted({p.suffix.casefold() for p in input_images}),
        "input_dataset_provenance": ({
            "kind": "ENGINE_VERIFIED_HEIC_JPEG_DERIVATIVES",
            "manifest_path": str(preparation_manifest_path(root).resolve()),
            "manifest_sha256": sha256_file(preparation_manifest_path(root)),
            "preparation_fingerprint": prep_manifest.get("preparation_fingerprint"),
            "mapped_image_count": len(prep_manifest.get("image_mappings", [])),
            "mapping_sha256": json_digest(prep_manifest.get("image_mappings", [])),
            "source_directory": prep_manifest.get("source_directory"),
            "converter": prep_manifest.get("conversion", {}).get("backend"),
            "color_handling": prep_manifest.get("conversion", {}).get("color_handling"),
        } if prep_manifest else {
            "kind": "CONFIGURED_IMAGE_DIRECTORY",
            "source_directory": str(image_dir),
            "explicit_derived_image_manifest": input_cfg.get("derived_image_manifest"),
        }),
        "backend": {"executable": str(executable), "version": backend.get("tool", {}).get("version"),
                    "capabilities": backend_capabilities},
        "normalized_command": command,
        "output_workspace": str(attempt), "configuration": selected_cfg,
        "training_effective_configuration": (training_effective_configuration if stage_id == "training" else None),
        "environment_override_keys": sorted(env),
        "environment_overrides_sha256": json_digest(env),
        "resource_limits": limits, "quality_gates": quality_gates,
        "checkpoint_policy": ({"steps_per_save": cfg.get("steps_per_save", 100),
                               "save_only_latest_checkpoint": cfg.get("save_only_latest_checkpoint", True),
                               "save_full_checkpoint": cfg.get("save_full_checkpoint", True),
                               "checkpoint_replacement_reserve_bytes": training_limits["checkpoint_replacement_reserve_bytes"],
                               "checkpoint_replacement_reserve_basis": training_limits["checkpoint_replacement_reserve_basis"],
                               "checkpoint_replacement_overlap_assumption": training_limits["checkpoint_replacement_overlap_assumption"],
                               "minimum_required_at_launch_bytes": training_limits["minimum_required_at_launch_bytes"],
                               "retention_owner": "SPIRULA_BACKEND"} if stage_id == "training" else None),
        "resume_checkpoint_identity": resume_identity,
        "reuse": {"valid_cached_result": False, "policy": "valid cache is reused before authorization is checked"},
        "cancellation_and_recovery": ("Cancellation retains attempt files and history; no automatic retry or cleanup occurs. An explicitly configured model.resume_from selects an existing run directory and passes Spirula --resume; optimizer restoration remains NOT_TESTED." if stage_id == "training" else "Cancellation retains attempt files and history; retry starts a new authorized attempt."),
        "execution_context": {"kind": "operator-initiated-local-cli", "host_sandbox_authorization": "NOT_EXPOSED_TO_ENGINE"},
    }
    if training_scope:
        descriptor["sfm_component_scope"] = {
            "component_id": training_scope["component_id"],
            "component_artifact_tree_sha256": training_scope["component"]["artifact_tree_sha256"],
            "registered_image_count": training_scope["registered_image_count"],
            "registered_image_membership_sha256": training_scope["registered_image_membership_sha256"],
            "sfm_source_image_count": training_scope["source_image_count"],
            "training_source_directory_image_count": len(source_images),
            "excluded_training_source_image_count": len(source_images) - training_scope["registered_image_count"],
            "reconstruction_binding_sha256": training_scope["sfm_snapshot"]["binding_sha256"],
            "acceptance": training_scope["acceptance"],
        }
    descriptor["plan_id"] = json_digest(descriptor)
    return descriptor


def assert_authorization_plan_current(root: Path, config: dict, state: ProjectState, stage_id: str,
                                      plan_id: str, backend_info: dict | None = None) -> dict:
    current = expensive_execution_plan(root, config, state, stage_id, backend_info=backend_info)
    if current["plan_id"] != plan_id:
        raise AuthorizationRequired(stage_id, f"The approved {stage_id} execution plan changed before backend launch. Re-run plan --full-hash and authorize the new plan.", current)
    return current


def _copy_json(source: Path, target: Path) -> dict:
    value = read_json(source)
    write_json(target, value)
    return {"source": str(source), "output": str(target), "sha256": sha256_file(target), "size_bytes": target.stat().st_size}


def image_inventory(root: Path, config: dict, state: ProjectState, force_hash: bool = False) -> dict:
    source_dir = project_path(root, config, config["source"]["photo_directory"])
    if not source_dir.is_dir():
        raise WorkflowError(f"Source photo directory does not exist: {source_dir}")
    preflight = config.get("integrations", {}).get("preflight", {})
    adopted = Path(preflight["image_inventory"]).expanduser() if preflight.get("image_inventory") else None
    if adopted:
        adopted = adopted if adopted.is_absolute() else (root / adopted)
        rows = read_json(adopted).get("images", [])
        if not rows:
            raise WorkflowError("Configured Phase2EU image inventory contains no images")
        listed = {str(row.get("relative_path") or row.get("filename")) for row in rows}
        suffixes = IMAGE_SUFFIXES
        observed = {p.relative_to(source_dir).as_posix() for p in source_dir.rglob("*") if p.is_file() and p.suffix.casefold() in suffixes}
        if listed != observed:
            raise WorkflowError(f"Source inventory membership changed: {len(listed-observed)} expected images missing; {len(observed-listed)} new images not represented in adopted inventory")
    else:
        suffixes = IMAGE_SUFFIXES
        rows = []
        for file in sorted(p for p in source_dir.rglob("*") if p.is_file() and p.suffix.casefold() in suffixes):
            relative = file.relative_to(source_dir).as_posix()
            rows.append({"image_id": file.stem, "relative_path": relative, "format": file.suffix.lstrip(".").upper(), "file_bytes": file.stat().st_size, "sha256": None, "width": None, "height": None, "exif": "NOT_PARSED_BY_STDLIB_IMAGE_INVENTORY"})
    inventory_rows = []
    seen = set()
    for row in rows:
        rel = row.get("relative_path") or row.get("filename")
        if not rel:
            raise WorkflowError(f"Image inventory row has no relative_path: {row.get('image_id')}")
        file = (source_dir / rel).resolve()
        if source_dir not in file.parents or not file.is_file():
            raise WorkflowError(f"Image inventory source is missing or escapes the source directory: {rel}")
        digest = state.cached_hash(file, force_hash)
        expected = row.get("sha256")
        if expected and digest != expected:
            raise WorkflowError(f"Source checksum differs from the adopted image inventory for {rel}")
        if rel in seen:
            raise WorkflowError(f"Duplicate relative source path in inventory: {rel}")
        seen.add(rel)
        width, height = row.get("width"), row.get("height")
        if not width or not height:
            width, height = _image_dimensions(file)
        inventory_rows.append({"image_id": row.get("image_id", Path(rel).stem), "relative_path": rel,
                               "sha256": digest, "size_bytes": file.stat().st_size,
                               "width": width, "height": height, "format": row.get("format") or file.suffix.lstrip(".").upper(),
                               "exif": row.get("exif", "RETAINED_IN_SOURCE; PARSED_FIELDS_FROM_ADOPTED_INVENTORY"),
                               "registration_status": row.get("registration_status", "UNKNOWN"),
                               "pose_trust_status": row.get("pose_trust_status", "UNKNOWN"),
                               "quality_labels": row.get("image_quality_labels", row.get("quality_labels", []))})
    return {"schema_version": "exhibition-archive.source-index.v1", "project_id": config["project_id"],
            "source_directory": str(source_dir), "source_is_read_only": True, "image_count": len(inventory_rows),
            "images": inventory_rows, "duplicate_source_paths": [],
            "metadata_policy": "Original files remain in place. EXIF values are recorded when adopted from an existing inventory; source image bytes are not modified.",
            "generated_at_utc": config.get("created_at_utc")}


def _image_dimensions(path: Path) -> tuple[int | None, int | None]:
    """Read common raster dimensions without adding an image-library dependency."""
    try:
        with path.open("rb") as f:
            header = f.read(32)
            if header.startswith(b"\x89PNG\r\n\x1a\n") and len(header) >= 24:
                import struct
                return struct.unpack(">II", header[16:24])
            if header[:2] == b"BM" and len(header) >= 26:
                import struct
                return struct.unpack("<ii", header[18:26])
            if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
                return None, None
            if header[:2] != b"\xff\xd8": return None, None
            f.seek(2)
            while True:
                marker_start = f.read(1)
                if not marker_start: break
                if marker_start != b"\xff": continue
                marker = f.read(1)
                while marker == b"\xff": marker = f.read(1)
                if not marker or marker in (b"\xd8", b"\xd9"): continue
                length_data = f.read(2)
                if len(length_data) != 2: break
                length = int.from_bytes(length_data, "big")
                if marker[0] in {0xC0,0xC1,0xC2,0xC3,0xC5,0xC6,0xC7,0xC9,0xCA,0xCB,0xCD,0xCE,0xCF}:
                    body = f.read(5)
                    if len(body) == 5: return int.from_bytes(body[3:5], "big"), int.from_bytes(body[1:3], "big")
                    break
                f.seek(length - 2, 1)
    except (OSError, ValueError):
        pass
    return None, None


def _training_resolution_request(image_directory: Path, divisor: int) -> dict[str, Any]:
    images = []
    try:
        from PIL import Image
    except ImportError:
        Image = None
    for path in sorted(item for item in image_directory.rglob("*") if item.is_file()):
        size = None
        if Image is not None:
            try:
                with Image.open(path) as image:
                    size = image.size
            except Exception:
                size = None
        if size is None:
            size = _image_dimensions(path)
        width, height = size
        row = {"relative_path": path.relative_to(image_directory).as_posix(),
               "width": width, "height": height}
        if width and height and divisor == 1:
            row["configured_effective_training_dimensions"] = [width, height]
            row["configured_dimensions_basis"] = "Source image pixels; Spirula train-resolution-divisor=1; no max-image-size argument"
        else:
            row["configured_effective_training_dimensions"] = None
        images.append(row)
    known = [row for row in images if row["width"] and row["height"]]
    return {"status": "REQUEST_RECORDED", "configured_resolution_divisor": divisor,
            "configured_resolution_policy": "full source pixels" if divisor == 1 else "backend divisor configured",
            "configured_effective_dimensions_basis": ("Per-image source dimensions are retained because the backend command uses divisor 1 and does not set max_image_size; backend log observation is recorded separately."
                                                        if divisor == 1 else "Backend divisor is recorded; exact integer rounding is backend-specific."),
            "registered_image_count": len(images), "image_dimensions_known_count": len(known),
            "image_dimensions_unknown_count": len(images) - len(known),
            "source_image_dimensions": images, "backend_reported_training_resolution": None,
            "backend_report_status": "PENDING_TRAINING_LOG_INSPECTION"}


def _training_resolution_from_log(request: dict[str, Any], log_path: Path) -> dict[str, Any]:
    observed = dict(request)
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as stream:
            text = stream.read()
    except OSError:
        observed["backend_report_status"] = "TRAINING_LOG_UNAVAILABLE"
        return observed
    patterns = (r"(?:training\s+|train(?:ing)?\s+|input\s+|image\s+)?(?:resolution|size)\s*[:=]\s*(\d+)\s*[x×]\s*(\d+)",
                r"(?:training\s+|train(?:ing)?\s+)(\d+)\s*[x×]\s*(\d+)")
    for line in text.splitlines():
        for pattern in patterns:
            match = re.search(pattern, line, re.IGNORECASE)
            if match:
                observed["backend_reported_training_resolution"] = [int(match.group(1)), int(match.group(2))]
                observed["backend_report_status"] = "REPORTED_IN_SPIRULA_LOG"
                observed["backend_report_evidence"] = line.strip()[:500]
                return observed
    observed["backend_report_status"] = "NOT_REPORTED_BY_BACKEND"
    observed["note"] = "Source image dimensions and configured divisor are recorded; Spirula did not emit a recognizable effective tensor size in its log."
    return observed


def _navigation_mode(config: dict) -> str:
    mode = config.get("integrations", {}).get("navigation", {}).get("mode", "adopt_existing")
    if mode not in {"adopt_existing", "generate_from_sfm"}:
        raise WorkflowError(f"Unsupported navigation mode {mode!r}; choose adopt_existing or generate_from_sfm")
    return mode


def _navigation_input_value(root: Path, config: dict, value: Any) -> tuple[Any, list[str]]:
    if isinstance(value, str):
        path = project_path(root, config, value)
        if not path.is_file():
            raise WorkflowError(f"Configured navigation evidence file does not exist: {path}")
        return read_json(path), [str(path.resolve())]
    return value, []


def _camera_capture_metadata(image_path: Path) -> dict[str, Any]:
    """Read ordering and pixel-orientation evidence from the prepared SfM image."""
    from PIL import Image

    with Image.open(image_path) as image:
        exif = image.getexif()
        exif_ifd = exif.get_ifd(34665) if exif else {}
        date_original = exif_ifd.get(36867)
        subseconds = str(exif_ifd.get(37521, "0")).strip()
        offset = str(exif_ifd.get(36881, "")).strip()
        result: dict[str, Any] = {"capture_metadata_status": "DATE_TIME_ORIENTATION_READ",
                                  "capture_order_source": "EXIF DateTimeOriginal",
                                  "prepared_image_orientation": int(exif.get(274, 1)),
                                  "prepared_image_width": int(image.width),
                                  "prepared_image_height": int(image.height)}
        if date_original:
            parsed = datetime.strptime(str(date_original), "%Y:%m:%d %H:%M:%S")
            digits = "".join(character for character in subseconds if character.isdigit())
            if digits:
                parsed = parsed.replace(microsecond=int((digits + "000000")[:6]))
            match = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", offset)
            if match:
                minutes = (int(match.group(2)) * 60 + int(match.group(3))) * (1 if match.group(1) == "+" else -1)
                parsed = parsed.replace(tzinfo=timezone(timedelta(minutes=minutes)))
                parsed = parsed.astimezone(timezone.utc)
            result["capture_timestamp"] = parsed.isoformat()
            result["capture_metadata_status"] = "ORDERED_EXIF_TIMESTAMP"
        else:
            result["capture_metadata_status"] = "CAPTURE_TIME_UNAVAILABLE"
            result["capture_order_source"] = None
        return result


def _latest_navigation_baseline(root: Path, config: dict[str, Any]) -> dict[str, Any] | None:
    """Read the stable pre-reconciliation proposal used as comparison evidence.

    New trajectory-reconciled proposals point back to their original baseline.
    Follow that hash-verified link so repeated v2 generations do not compare
    against the previous v2 result and silently erase the v1 delta.
    """
    proposal_dir = output_path(root, config) / "navigation" / "proposals"
    if not proposal_dir.is_dir():
        return None
    paths = sorted(proposal_dir.glob("navigation-proposal-v*.json"), reverse=True)
    documents: list[tuple[Path, dict[str, Any]]] = []
    for path in paths:
        if path.is_symlink() or not path.is_file():
            continue
        try:
            document = read_json(path)
        except (OSError, ValueError, TypeError):
            continue
        if document.get("schema_version") == "exhibition-archive.navigation.v1":
            documents.append((path, document))

    def is_pre_reconciliation(document: dict[str, Any]) -> bool:
        return not document.get("coverage_diagnostics", {}).get("trajectory_reconciliation")

    def result(path: Path, document: dict[str, Any]) -> dict[str, Any]:
        return {"document": document, "path": str(path.resolve()), "sha256": sha256_file(path)}

    # Follow the newest v2 proposal's pinned v1 provenance first. Resolve only
    # files inside the proposal directory and verify the recorded digest before
    # treating the referenced document as comparison or vertical-limit evidence.
    for _, document in documents:
        anchor = document.get("coverage_diagnostics", {}).get(
            "trajectory_reconciliation", {}).get("baseline_proposal", {})
        anchor_path = anchor.get("path") if isinstance(anchor, dict) else None
        anchor_sha = anchor.get("sha256") if isinstance(anchor, dict) else None
        if not isinstance(anchor_path, str) or not isinstance(anchor_sha, str):
            continue
        candidate = Path(anchor_path)
        try:
            candidate_resolved = candidate.resolve(strict=True)
            proposal_dir_resolved = proposal_dir.resolve(strict=True)
            if (candidate_resolved.parent != proposal_dir_resolved or candidate.is_symlink() or
                    not candidate_resolved.is_file() or sha256_file(candidate_resolved) != anchor_sha):
                continue
            anchored_document = read_json(candidate_resolved)
        except (OSError, ValueError, TypeError):
            continue
        if (anchored_document.get("schema_version") == "exhibition-archive.navigation.v1" and
                is_pre_reconciliation(anchored_document)):
            return result(candidate_resolved, anchored_document)

    # For workspaces created before v2, use the newest legacy proposal. If no
    # stable v1 exists, report no baseline instead of comparing v2 with itself.
    for path, document in documents:
        if is_pre_reconciliation(document):
            return result(path, document)
    return None


def _navigation_source(root: Path, config: dict, state: ProjectState | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve trusted poses, coordinate frame and geometry for fresh navigation generation."""
    nav_cfg = config.get("integrations", {}).get("navigation", {})
    sfm_cfg = config.get("integrations", {}).get("sfm", {})
    source_files: list[str] = []
    center_doc = None
    sparse_point_tracks: list[int] = []
    sparse_point_errors: list[float] = []
    if nav_cfg.get("camera_centers"):
        center_doc, paths = _navigation_input_value(root, config, nav_cfg["camera_centers"])
        source_files.extend(paths)
    coordinate_frame = dict(nav_cfg.get("coordinate_frame", {}))
    if center_doc and center_doc.get("stations") and any("centerCommonViewer" in row for row in center_doc["stations"]):
        viewer_transform_verified = nav_cfg.get("viewer_transform_verified", coordinate_frame.get("viewer_transform_verified", False))
        identity = {"scale_status": "UNKNOWN_NON_METRIC", "viewer_frame": "configured PlayCanvas common viewer frame",
                    "coordinates_are_viewer": True, "source_to_viewer": {"uniform_scale": 1, "rotation": [[1,0,0],[0,1,0],[0,0,1]], "translation": [0,0,0], "verified": bool(viewer_transform_verified)},
                    **coordinate_frame}
        coordinate_frame = identity
        stations = [{**row, "trusted": True, "trust_reason": row.get("trust_reason", "CONFIGURED_TRUSTED_CAMERA_STATION_ARTIFACT")} for row in center_doc["stations"]]
        source_rec = {"identity": {"reconstruction_id": nav_cfg.get("reconstruction_id", project_path(root, config, sfm_cfg["directory"]).name if sfm_cfg.get("directory") else config["project_id"])},
                      "coordinate_frame": center_doc.get("coordinateFrame", "precomputed camera centers")}
        cameras = stations
        sparse_points, camera_models = [], []
        floor_default = center_doc.get("floorPlane")
        if floor_default:
            floor_default = {"normal": floor_default.get("normalUnit", floor_default.get("normal")), "offset": floor_default.get("offset", 0),
                             "coordinate_frame": "viewer", "classification": "GEOMETRY_SUPPORTED",
                             "source": "supplied precomputed floor-plane evidence"}
    else:
        if sfm_cfg.get("directory"):
            reconstruction_dir = project_path(root, config, sfm_cfg["directory"])
        else:
            sfm_summary = output_path(root, config) / "work" / "sfm-summary.json"
            reconstruction_value = read_json(sfm_summary).get("reconstruction_directory") if sfm_summary.is_file() else None
            if not reconstruction_value:
                raise WorkflowError("generate_from_sfm requires integrations.sfm.directory or the validated fresh-SfM summary")
            reconstruction_dir = Path(reconstruction_value).expanduser().resolve()
        if state is not None:
            try:
                from .sfm_review import geometry_proposal_scope
                geometry_scope = geometry_proposal_scope(root, config, state)
            except Exception:
                geometry_scope = None
            if geometry_scope:
                reconstruction_dir = Path(geometry_scope["reconstruction_directory"]).resolve()
        trust = None
        if nav_cfg.get("trusted_pose_classification"):
            trust, paths = _navigation_input_value(root, config, nav_cfg["trusted_pose_classification"])
            source_files.extend(paths)
            trust = dict(trust)
            excluded = trust.get("excluded_image_names", trust.get("excludedPoseOutlierKeys", []))
            if excluded and not trust.get("untrusted_image_names"):
                trust["untrusted_image_names"] = excluded
        reconstruction = read_colmap_reconstruction(reconstruction_dir, trust)
        source_files.extend(str((reconstruction_dir / name).resolve()) for name in ("cameras.bin", "images.bin", "points3D.bin"))
        cameras, sparse_points, camera_models = reconstruction["cameras"], reconstruction["sparse_points"], reconstruction["camera_models"]
        try:
            image_directory, _ = effective_sfm_dataset(root, config, validate_files=False)
        except (BackendError, DatasetPreparationRequired, OSError, ValueError):
            image_directory = None
        orientation_values: Counter[int] = Counter()
        dimension_mismatches = []
        if image_directory is not None:
            for camera in cameras:
                image_path = image_directory / str(camera.get("image_name", ""))
                try:
                    metadata = _camera_capture_metadata(image_path)
                except (OSError, ValueError, TypeError):
                    metadata = {"capture_metadata_status": "CAPTURE_METADATA_UNAVAILABLE",
                                "capture_order_source": None}
                camera.update(metadata)
                orientation = camera.get("prepared_image_orientation")
                if isinstance(orientation, int):
                    orientation_values[orientation] += 1
                camera_model = next((item for item in camera_models if item["camera_id"] == camera["camera_id"]), None)
                if (camera_model and camera.get("prepared_image_width") is not None and
                        (camera["prepared_image_width"], camera["prepared_image_height"]) !=
                        (camera_model["width"], camera_model["height"])):
                    dimension_mismatches.append(camera["image_name"])
        source_rec = reconstruction["source_reconstruction"]
        source_rec["identity"] = {"reconstruction_id": nav_cfg.get("reconstruction_id", reconstruction_dir.name)}
        floor_default = None
        coordinate_frame = {**reconstruction["source_reconstruction"]["coordinate_frame"], **coordinate_frame}
        # Navigation consumes the same non-destructive rotation used by the
        # first-visual model and opening camera. This verifies coordinate
        # consistency, while the orientation evidence remains a camera-up
        # proposal rather than a claim about physical gravity.
        from .viewer_orientation import estimate_y_up_transform, rotate_vector_xyzw
        orientation = estimate_y_up_transform(cameras)
        quaternion = orientation["rotation_quaternion_xyzw"]
        axes = [rotate_vector_xyzw(quaternion, axis) for axis in
                ([1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0])]
        rotation = [[axes[column][row] for column in range(3)] for row in range(3)]
        coordinate_frame.update({
            "viewer_frame": "PlayCanvas model frame; identical orientation transform to first-visual",
            "navigation_up_viewer": [0.0, 1.0, 0.0],
            "navigation_reference_axis_viewer": [1.0, 0.0, 0.0],
            "up_axis": "VIEWER_Y_FROM_REGISTERED_CAMERA_UP_PROPOSAL",
            "navigation_up_evidence": orientation,
            "transform_provenance": (
                "The source-to-viewer rotation is the same camera-up proposal applied to the Gaussian model and "
                "opening camera; this marks coordinate consistency only, not physical gravity or a ground plane."
            ),
            "source_to_viewer": {"uniform_scale": 1.0, "rotation": rotation,
                                 "translation": [0.0, 0.0, 0.0], "verified": True},
        })
        sparse_point_tracks = reconstruction.get("sparse_point_tracks", [])
        sparse_point_errors = reconstruction.get("sparse_point_errors", [])
    aligned_lidar_diagnostics: dict[str, Any] = {"status": "NOT_AVAILABLE", "navigation_consumed": False}
    if not center_doc:
        lidar_dir = output_path(root, config) / "lidar"
        lidar_stage_row = state.stage("lidar_registration") if state is not None else None
        try:
            lidar_stage_details = json.loads((lidar_stage_row or {}).get("details_json") or "{}")
        except (TypeError, ValueError):
            lidar_stage_details = {}
        alignment_path = Path(lidar_stage_details.get("alignment_report_path") or lidar_dir / "alignment-report.json").expanduser()
        if not alignment_path.is_absolute():
            alignment_path = root / alignment_path
        aligned_preview_value = lidar_stage_details.get("aligned_preview_path")
        aligned_preview_path = Path(aligned_preview_value).expanduser() if aligned_preview_value else None
        if aligned_preview_path is None and alignment_path.is_file():
            try:
                aligned_preview_value = read_json(alignment_path).get("aligned_preview_path")
                aligned_preview_path = Path(aligned_preview_value).expanduser() if aligned_preview_value else None
            except (OSError, ValueError, TypeError):
                aligned_preview_path = None
        if aligned_preview_path is not None and not aligned_preview_path.is_absolute():
            aligned_preview_path = root / aligned_preview_path
        if (alignment_path.is_file() and not alignment_path.is_symlink() and aligned_preview_path is not None and
                aligned_preview_path.is_file() and not aligned_preview_path.is_symlink()):
            try:
                alignment = read_json(alignment_path)
                allowed_root = lidar_dir.resolve()
                if allowed_root not in alignment_path.resolve().parents or allowed_root not in aligned_preview_path.resolve().parents:
                    raise ValueError("Alignment diagnostics resolve outside the configured LiDAR output directory")
                if alignment.get("accepted") is True and alignment.get("status") == "ACCEPTED" and alignment.get("aligned_preview_status") == "WRITTEN":
                    sample = read_ply_xyz_sample(aligned_preview_path, maximum=20_000)
                    camera_centers = [row.get("camera_center") for row in cameras if row.get("camera_center") is not None]
                    plane_candidate = propose_lidar_ground_plane(sample["points"], camera_centers)
                    aligned_lidar_diagnostics = {
                        "status": "ALIGNED_GEOMETRY_CONSUMED_FOR_NAVIGATION_DIAGNOSTICS",
                        "navigation_consumed": True,
                        "alignment_report": str(alignment_path.resolve()),
                        "aligned_preview": str(aligned_preview_path.resolve()),
                        "coordinate_frame": "COLMAP arbitrary non-metric reconstruction coordinates",
                        "sample": {key: sample.get(key) for key in
                                   ("point_count", "sampled_point_count", "sample_stride", "invalid_sampled_point_count", "bounding_box")},
                        "ground_plane_candidate": plane_candidate,
                    }
                    source_files.extend([str(alignment_path.resolve()), str(aligned_preview_path.resolve())])
                    if not nav_cfg.get("floor_plane") and plane_candidate.get("status") == "PROPOSED":
                        floor_default = {
                            "normal": plane_candidate["normal"], "offset": plane_candidate["offset"],
                            "coordinate_frame": "source", "classification": plane_candidate["classification"],
                            "source": plane_candidate["source"], "evidence": plane_candidate["evidence"],
                        }
            except (OSError, ValueError, TypeError, KeyError) as exc:
                aligned_lidar_diagnostics = {"status": "UNVERIFIED", "navigation_consumed": False,
                                             "reason": f"Accepted alignment could not be sampled for navigation: {exc}"}
    floor_value = nav_cfg.get("floor_plane", floor_default)
    if isinstance(floor_value, str):
        floor_value, paths = _navigation_input_value(root, config, floor_value); source_files.extend(paths)
        floor_value = floor_value.get("floor_plane", floor_value.get("floor", floor_value))
    ceiling_value = nav_cfg.get("ceiling")
    if isinstance(ceiling_value, str):
        ceiling_value, paths = _navigation_input_value(root, config, ceiling_value); source_files.extend(paths)
        ceiling_value = ceiling_value.get("ceiling", ceiling_value)
    if floor_value:
        floor_value = dict(floor_value)
        if floor_value.get("normal") is None and floor_value.get("normalUnit") is not None:
            floor_value["normal"] = floor_value["normalUnit"]
        floor_value.setdefault("coordinate_frame", "viewer" if coordinate_frame.get("coordinates_are_viewer") else "source")
        floor_value.setdefault("classification", "GEOMETRY_SUPPORTED" if floor_value.get("source") else "PROPOSED")
    source = {"source_reconstruction": source_rec, "coordinate_frame": coordinate_frame, "cameras": cameras,
              "camera_models": camera_models, "sparse_points": sparse_points,
              "sparse_point_tracks": sparse_point_tracks,
              "sparse_point_errors": sparse_point_errors,
              "sparse_point_count": len(sparse_points),
              "floor_plane": floor_value, "ceiling": ceiling_value,
              "aligned_lidar_geometry_used": aligned_lidar_diagnostics.get("navigation_consumed", False),
              "aligned_lidar_diagnostics": aligned_lidar_diagnostics,
              "transitions": nav_cfg.get("transitions", []), "guided_paths": nav_cfg.get("guided_paths", []),
              "viewpoints": nav_cfg.get("viewpoints", []), "source_files": source_files,
              "source_model_sha256": nav_cfg.get("source_model_sha256")}
    if not center_doc:
        source["image_orientation_evidence"] = {
            "prepared_orientation_counts": dict(orientation_values),
            "camera_dimension_mismatch_count": len(dimension_mismatches),
            "camera_dimension_mismatch_image_names": dimension_mismatches,
            "interpretation": "Prepared-image metadata is used to establish sequence order and check image dimensions; it does not change camera poses.",
        }
    baseline = _latest_navigation_baseline(root, config)
    if baseline:
        source["baseline_navigation"] = baseline["document"]
        source["baseline_navigation_provenance"] = {"path": baseline["path"], "sha256": baseline["sha256"]}
    generation_config = {key: value for key, value in nav_cfg.items() if key not in {"mode", "camera_centers", "trusted_pose_classification", "floor_plane", "ceiling", "envelope", "runtime_policy"}}
    return source, generation_config


def execute_navigation_generation(root: Path, config: dict, state: ProjectState, output_dir: Path, execution_id: int) -> dict:
    try:
        source, generation_config = _navigation_source(root, config, state)
        proposal = generate_navigation(source, generation_config)
    except Exception as exc:
        proposal = {"schema_version": "exhibition-archive.navigation-fallback.v1",
                    "status": "NAVIGATION_UNVERIFIED", "regions": [],
                    "coordinate_frame": {"source_frame": "COLMAP_WORLD", "up_axis": "UNSPECIFIED"},
                    "coverage_assumptions": {"scene_type": config.get("integrations", {}).get("navigation", {}).get("scene_type", "auto"),
                                              "boundary": "NOT_GENERATED", "physical_walls_or_ceiling_inferred": False},
                    "review": {"status": "REQUIRED", "approved": False},
                    "validation": {"status": "UNVERIFIED", "errors": [], "warnings": [str(exc)]},
                    "reason": f"Automatic navigation proposal could not be generated: {exc}",
                    "provenance": {"generator": "exhibition_archive.navigation", "generation_execution_id": execution_id}}
    current_fingerprint = state.stage("navigation").get("input_fingerprint") if state.stage("navigation") else None
    proposal["provenance"]["navigation_input_fingerprint"] = current_fingerprint
    proposal["provenance"]["generation_execution_id"] = execution_id
    proposal_dir = output_path(root, config) / "navigation" / "proposals"
    proposal_dir.mkdir(parents=True, exist_ok=True)
    proposal_path = proposal_dir / f"navigation-proposal-v{execution_id:06d}.json"
    review_path = proposal_dir / f"navigation-review-v{execution_id:06d}.html"
    write_json(proposal_path, proposal)
    if proposal.get("schema_version") == "exhibition-archive.navigation-fallback.v1":
        import html
        review_path.write_text("<!doctype html><meta charset='utf-8'><title>Navigation unverified</title>"
                               "<main style='font:16px system-ui;max-width:48rem;margin:3rem auto'>"
                               "<h1>Navigation proposal unavailable</h1><p>Visitor navigation remains unverified. "
                               "No scene boundary was inferred or published.</p><pre>" + html.escape(proposal["reason"]) +
                               "</pre></main>", encoding="utf-8")
    else:
        review_path.write_text(render_navigation_review_html(proposal), encoding="utf-8")
    from .navigation_review import invalidate_navigation_approval
    invalidated = invalidate_navigation_approval(state, "A new generated navigation proposal replaced the previously approved artifact",
                                                 current_fingerprint)
    gate_decision = {"decision": "PENDING", "candidate_path": str(proposal_path.resolve()),
                     "candidate_sha256": sha256_file(proposal_path),
                     "navigation_input_fingerprint": current_fingerprint,
                     "previous_approval_invalidated": bool(invalidated and invalidated.get("previous_status") == "APPROVED"),
                     "previous_gate_invalidated": invalidated is not None}
    state.db.execute("INSERT INTO review_gates(gate_id,status,required,updated_at,decision_json) VALUES(?,?,?,?,?) "
                     "ON CONFLICT(gate_id) DO UPDATE SET status=excluded.status,required=excluded.required,updated_at=excluded.updated_at,decision_json=excluded.decision_json",
                     ("navigation-approval", "PENDING", 1, utc_now(), json.dumps(gate_decision, sort_keys=True)))
    state.db.commit()
    summary_path = output_dir / "navigation-summary.json"
    pose_evidence = proposal.get("pose_evidence", {})
    summary = {"status": proposal["status"], "proposal_id": proposal.get("id", f"unverified-{execution_id:06d}"),
               "envelope_id": proposal.get("id"), "proposal_path": str(proposal_path),
               "review_path": str(review_path), "region_count": len(proposal.get("regions", [])),
               "registered_camera_position_candidate_count": pose_evidence.get("used_camera_station_count", 0),
               "explicitly_trusted_input_camera_count": pose_evidence.get("trusted_input_count", 0),
               "provisional_registered_camera_position_count": pose_evidence.get("provisional_registered_pose_count", 0),
               "excluded_pose_count": pose_evidence.get("excluded_pose_count", 0),
               "geometry_unbounded_camera_position_count": pose_evidence.get("geometry_unbounded_station_count", 0),
               "camera_registration_confidence": pose_evidence.get("camera_registration_confidence"),
               "aligned_lidar_used": proposal.get("coverage_assumptions", {}).get("aligned_lidar_used", False),
               "aligned_lidar_navigation_consumed": bool(proposal.get("provenance", {}).get("aligned_lidar_diagnostics", {}).get("navigation_consumed", False)),
               "ground_plane_candidate_status": proposal.get("coverage_assumptions", {}).get("ground_plane"),
               "validation": proposal.get("validation", {"status": "UNVERIFIED"}), "publishing_ready": False,
               "training_dependency": False, "sfm_dependency": True}
    write_json(summary_path, summary)
    return {"status": "NEEDS_REVIEW", "artifacts": [proposal_path, review_path, summary_path],
            "details": summary}


def execute_lidar_inspection(root: Path, config: dict, output_dir: Path) -> dict:
    """Inspect optional point clouds in their own cacheable stage."""
    source = project_path(root, config, config["source"]["photo_directory"])
    discovery = discover_input_files(source, hash_images=False)
    discovery_path = output_path(root, config) / "inputs" / "discovery.json"
    write_json(discovery_path, discovery)
    lidar_dir = output_path(root, config) / "lidar"
    # These are engine-owned previews. A changed or missing scan must never
    # leave a prior preview visible as though it belonged to the new inputs.
    for name in ("lidar-preview.ply", "aligned-cloud-preview.ply"):
        (lidar_dir / name).unlink(missing_ok=True)
    lidar_enabled = config.get("integrations", {}).get("lidar", {}).get("enabled", True)
    if lidar_enabled:
        report = inspect_optional_lidar(discovery, source, lidar_dir)
    else:
        report = inspect_optional_lidar({**discovery, "ply_files": [],
                                         "raw_lidar_status": "DISABLED_BY_CONFIGURATION"},
                                        source, lidar_dir)
        report["inspection_status"] = "DISABLED_BY_CONFIGURATION"
        write_json(lidar_dir / "inspection.json", report)
    artifacts = [discovery_path, lidar_dir / "inspection.json"]
    preview_path = lidar_dir / "lidar-preview.ply"
    if preview_path.is_file():
        artifacts.append(preview_path)
    return {"status": "COMPLETED", "artifacts": artifacts,
            "details": {"inspection_status": report.get("inspection_status"),
                        "raw_lidar_status": report.get("selection_status"),
                        "detected": report.get("detected", False),
                        "inspection_report_path": str((lidar_dir / "inspection.json").resolve()),
                        "discovery_report_path": str(discovery_path.resolve()),
                        "photo_only_fallback_available": True}}


def execute_lidar_registration(root: Path, config: dict, state: ProjectState, output_dir: Path,
                               execution_id: int | None = None) -> dict:
    """Attempt bounded global registration; all optional alignment failures remain recoverable."""
    lidar_root = output_path(root, config) / "lidar"
    # Stage execution IDs are monotonic and keep prior accepted alignment artifacts intact.
    execution_label = f"{execution_id:06d}" if execution_id is not None else "untracked"
    registration_dir = lidar_root / "registrations" / f"alignment-execution-{execution_label}"
    registration_dir.mkdir(parents=True, exist_ok=True)
    target = registration_dir / "alignment-report.json"
    latest_report_path = lidar_root / "alignment-report.json"
    inspection_path = output_path(root, config) / "lidar" / "inspection.json"
    inspection = read_json(inspection_path) if inspection_path.is_file() else {
        "inspection_status": "NOT_DETECTED", "detected": False,
        "alignment_attempted": False, "warnings": ["No LiDAR inspection report is available."]}
    sfm_details = json.loads((state.stage("sfm") or {}).get("details_json") or "{}")
    reconstruction_value = sfm_details.get("reconstruction_directory")
    try:
        from .sfm_review import geometry_proposal_scope
        geometry_scope = geometry_proposal_scope(root, config, state)
    except Exception:
        geometry_scope = None
    if geometry_scope:
        reconstruction_value = geometry_scope["reconstruction_directory"]
    if not reconstruction_value:
        sfm_cfg = config.get("integrations", {}).get("sfm", {})
        if sfm_cfg.get("directory"):
            reconstruction_value = str(project_path(root, config, sfm_cfg["directory"]))
    reconstruction = Path(reconstruction_value).expanduser() if reconstruction_value else None
    if reconstruction is None or not reconstruction.is_dir():
        report = {"schema_version": "exhibition-archive.lidar-alignment.v1",
                  "alignment_attempted": False, "status": "REJECTED", "accepted": False,
                  "reason": "Validated SfM sparse geometry is unavailable; LiDAR is not consumed.",
                  "training_consumed": False, "navigation_consumed": False}
        write_json(target, report)
    else:
        try:
            from .lidar import sample_colmap_sparse_points
            points = sample_colmap_sparse_points(reconstruction, maximum=100_000)
            report = attempt_similarity_alignment(inspection, points, target.parent)
        except Exception as exc:
            report = {"schema_version": "exhibition-archive.lidar-alignment.v1",
                      "alignment_attempted": False, "status": "REJECTED", "accepted": False,
                      "reason": f"Optional LiDAR registration could not prepare valid sparse geometry: {exc}",
                      "training_consumed": False, "navigation_consumed": False}
            write_json(target, report)
    write_json(latest_report_path, report)
    artifacts = [target, latest_report_path]
    aligned_preview = target.parent / "aligned-cloud-preview.ply"
    if aligned_preview.is_file():
        artifacts.append(aligned_preview)
    return {"status": "COMPLETED", "artifacts": artifacts,
            "details": {"alignment_status": report.get("status"),
                        "alignment_attempted": report.get("alignment_attempted", False),
                        "accepted": report.get("accepted", False),
                        "training_consumed": report.get("training_consumed", False),
                        "navigation_consumed": report.get("navigation_consumed", False),
                        "geometry_review_scope": geometry_scope.get("review_scope") if geometry_scope else "NOT_REQUIRED_FOR_COMPLETED_SFM",
                        "selected_sfm_component_id": geometry_scope.get("component_id") if geometry_scope else None,
                        "alignment_report_path": str(target.resolve()),
                        "latest_alignment_report_path": str(latest_report_path.resolve()),
                        "aligned_preview_path": str(aligned_preview.resolve()) if aligned_preview.is_file() else None,
                        "reason": report.get("reason")}}


def execute_sfm_studio_export(root: Path, config: dict, state: ProjectState,
                              output_dir: Path) -> dict:
    """Prepare a self-contained COLMAP folder for local Studio review."""
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "export-report.json"
    try:
        from .studio_export import (build_studio_export_stage_report,
                                    export_spirula_colmap_dataset)
        sfm_details = json.loads((state.stage("sfm") or {}).get("details_json") or "{}")
        reconstruction_value = (sfm_details.get("reconstruction_directory") or
                                config.get("integrations", {}).get("sfm", {}).get("directory"))
        if not reconstruction_value:
            raise BackendError("SfM stage has no recorded/configured sparse reconstruction directory",
                               failure_kind="SFM_INVALID")
        reconstruction = Path(reconstruction_value).expanduser()
        if not reconstruction.is_absolute():
            reconstruction = root / reconstruction
        image_directory, preparation = effective_sfm_dataset(root, config, validate_files=True)
        derivative = sfm_details.get("image_derivative_provenance") or {}
        provenance = {"source_photographs_modified": False,
                      "effective_dataset_kind": "validated_SfM_input_dataset",
                      "preparation_manifest_sha256": preparation.get("manifest_sha256") if preparation else None,
                      "prepared_image_count": preparation.get("mapped_image_count") if preparation else None,
                      "derivative_provenance_recorded": bool(derivative)}
        result = export_spirula_colmap_dataset(
            image_directory, reconstruction, output_path(root, config) / "sfm" / "spirula-studio-export",
            project_id=config["project_id"], source_provenance=provenance)
        report = build_studio_export_stage_report(result, output_path(root, config))
        write_json(report_path, report)
        return {"status": "COMPLETED", "artifacts": [*result["artifacts"], report_path],
                "details": {"export_status": result["status"],
                            "package_directory": result["package_directory"],
                            "manifest_path": result["manifest_path"],
                            "report_path": str(report_path.resolve()),
                            "validation": result["validation"],
                            "gui_reopen_status": "NOT_GUI_VERIFIED"}}
    except (BackendError, OSError, ValueError, ImportError) as exc:
        report = {"schema_version": "exhibition-archive.spirula-export-stage.v1",
                  "status": "WARNING", "gui_reopen_status": "NOT_GUI_VERIFIED",
                  "error": f"{type(exc).__name__}: {exc}",
                  "photo_reconstruction_affected": False,
                  "action": "Inspect the SfM image mapping and COLMAP camera dimensions; the validated reconstruction remains available.",
                  "details": getattr(exc, "details", {})}
        write_json(report_path, report)
        return {"status": "WARNING", "artifacts": [report_path], "details": report}


def adopt_stage(root: Path, config: dict, state: ProjectState, stage_id: str, output_dir: Path) -> dict:
    i = config.get("integrations", {})
    output_dir.mkdir(parents=True, exist_ok=True)
    if stage_id == "photo_inventory":
        inventory = image_inventory(root, config, state)
        from .input_prep import materialize_image_only_dataset
        normalized_images = materialize_image_only_dataset(root, config, inventory)
        if normalized_images:
            inventory["normalized_image_dataset"] = normalized_images
        target = output_dir / "source-index.json"
        write_json(target, inventory)
        artifacts = [target]
        if normalized_images:
            artifacts.append(Path(normalized_images["manifest_path"]))
        return {"status": "COMPLETED", "artifacts": artifacts,
                "details": {"image_count": inventory["image_count"], "source_is_read_only": True,
                            "adopted_inventory_rows": bool(i.get("preflight", {}).get("image_inventory")),
                            "normalized_image_dataset": normalized_images}}
    if stage_id == "capture_preflight":
        cfg = i.get("preflight", {})
        if not cfg:
            inventory = read_json(output_dir / "source-index.json")
            sfm_cfg = i.get("sfm", {})
            sfm_directory = project_path(root, config, sfm_cfg["directory"]) if sfm_cfg.get("directory") else None
            sfm_statistics = None
            if sfm_directory and sfm_directory.is_dir():
                try:
                    sfm_statistics = colmap_binary_counts(sfm_directory)
                    sfm_status = "AVAILABLE_BASIC_COUNTS_ONLY"
                except Exception as exc:
                    sfm_status = "CONFIGURED_BUT_UNAVAILABLE_OR_INVALID"
                    sfm_statistics = {"validation_error": str(exc)}
            else:
                sfm_status = "UNKNOWN_NOT_CONFIGURED" if not sfm_cfg else "CONFIGURED_BUT_UNAVAILABLE_OR_INVALID"
            from collections import defaultdict
            by_hash = defaultdict(list)
            low_resolution = []
            unresolved_dimensions = []
            for row in inventory.get("images", []):
                by_hash[row["sha256"]].append(row["image_id"])
                width, height = row.get("width"), row.get("height")
                if width and height and width * height < int(config.get("preflight", {}).get("minimum_pixels", 2_000_000)):
                    low_resolution.append({"image_id": row["image_id"], "dimensions": [width, height], "risk": "LOW_RESOLUTION_REVIEW"})
                elif not width or not height:
                    unresolved_dimensions.append(row["image_id"])
            duplicates = [ids for ids in by_hash.values() if len(ids) > 1]
            angular_status = "UNKNOWN_SFM_UNAVAILABLE" if sfm_status == "UNKNOWN_NOT_CONFIGURED" else "UNKNOWN_REGION_ORIENTATION_ANALYSIS_NOT_IMPLEMENTED"
            summary = {"schema_version": "exhibition-archive.image-only-preflight.v1", "project_id": config["project_id"], "mode": "IMAGE_ONLY", "image_count": inventory["image_count"], "sfm_available": sfm_status == "AVAILABLE_BASIC_COUNTS_ONLY", "sfm_status": sfm_status, "sfm_statistics": sfm_statistics, "dimensions_available": inventory["image_count"] - len(unresolved_dimensions), "dimension_unknown_count": len(unresolved_dimensions), "exact_duplicate_groups": duplicates, "low_resolution_review_count": len(low_resolution), "blur_status": "UNKNOWN_IMAGE_OPERATOR_NOT_CONFIGURED", "exposure_status": "UNKNOWN_IMAGE_OPERATOR_NOT_CONFIGURED", "angular_coverage": angular_status, "generated_at_utc": config.get("created_at_utc")}
            risks = {"schema_version": "exhibition-archive.capture-risk-register.v1", "project_id": config["project_id"], "risks": [{"classification": "IMAGE_QUALITY_REVIEW", "risk": x, "source_image_id": x["image_id"]} for x in low_resolution], "known_unknowns": ["Blur and exposure are not diagnosed by the stdlib-only image inventory.", "Region visibility, camera-station diversity, and angular coverage are not computed by this image-only adapter.", "No artwork inventory was supplied."]}
            recommendations = {"schema_version": "exhibition-archive.recapture-recommendations.v1", "project_id": config["project_id"], "recommendations": [], "manual_review": "Inspect contact sheets and exhibition layout; this image-only run cannot infer spatial gaps or artwork regions."}
            targets = [output_dir / "capture-quality-summary.json", output_dir / "capture-risk-register.json", output_dir / "recapture-recommendations.json"]
            for target, value in zip(targets, (summary, risks, recommendations)): write_json(target, value)
            return {"status": "COMPLETED", "artifacts": targets, "details": summary}
        summary_path = project_path(root, config, cfg["summary"])
        summary = read_json(summary_path)
        risks_path = project_path(root, config, cfg["risk_register"])
        recapture_path = project_path(root, config, cfg["recapture_recommendations"])
        outputs = []
        for source, name in ((summary_path, "capture-quality-summary.json"), (risks_path, "capture-risk-register.json"), (recapture_path, "recapture-recommendations.json")):
            target = output_dir / name
            _copy_json(source, target)
            outputs.append(target)
        return {"status": "ADOPTED", "artifacts": outputs, "details": {"image_count": summary.get("image_count"), "sfm_statistics": summary.get("sfm_statistics"), "image_quality_counts": summary.get("image_quality_counts"), "human_review_required": True, "source_run": cfg.get("run_id", "configured existing Phase2EU report")}}
    if stage_id == "artwork_association":
        cfg = i.get("artwork", {})
        if not cfg:
            inventory = read_json(output_dir / "source-index.json")
            docs = {
                "phase2ev-state.private.json": {"schema_version": "exhibition-archive.artwork-inventory.v1", "project_id": config["project_id"], "status": "NO_ARTWORK_INVENTORY_SUPPLIED", "artworks": [], "context_candidates": [], "observations": [], "associations": [], "review_queue": []},
                "association-graph.private.json": {"schema_version": "exhibition-archive.association-graph.v1", "project_id": config["project_id"], "identity_nodes": [], "observation_nodes": [], "association_edges": []},
                "artwork-association-summary.json": {"schema_version": "exhibition-archive.artwork-association-summary.v1", "project_id": config["project_id"], "identity_status": "NOT_SUPPLIED", "artwork_count": 0, "observation_count": 0, "association_count": 0, "source_image_count": inventory["image_count"]},
                "confirmed-artworks.json": {"schema_version": "exhibition-archive.confirmed-artworks.v1", "project_id": config["project_id"], "artworks": [], "count": 0, "note": "No identities were invented."},
            }
            targets = []
            for name, value in docs.items():
                target = output_dir / name; write_json(target, value); targets.append(target)
            return {"status": "COMPLETED", "artifacts": targets, "details": {"provisional_artwork_count": 0, "confirmed_artwork_count": 0, "observation_count": 0, "association_count": 0, "review_pending": False, "identity_status": "NOT_SUPPLIED"}}
        state_path, graph_path, summary_path, confirmed_path = [project_path(root, config, cfg[k]) for k in ("state", "graph", "summary", "confirmed_artworks")]
        values = {"association_state": read_json(state_path), "association_graph": read_json(graph_path), "summary": read_json(summary_path), "confirmed_artworks": read_json(confirmed_path)}
        targets = []
        for key, name in (("association_state", "phase2ev-state.private.json"), ("association_graph", "association-graph.private.json"), ("summary", "artwork-association-summary.json"), ("confirmed_artworks", "confirmed-artworks.json")):
            target = output_dir / name
            write_json(target, values[key]); targets.append(target)
        summary = values["summary"]
        pending_count = sum(1 for row in values["association_state"].get("review_queue", []) if row.get("status") == "REVIEW_PENDING")
        state.db.execute("INSERT INTO review_gates(gate_id,status,required,updated_at,decision_json) VALUES(?,?,?,?,?) ON CONFLICT(gate_id) DO NOTHING", ("artwork-catalogue-review", "PENDING" if pending_count or values["confirmed_artworks"].get("count", 0) == 0 else "COMPLETE", 0, utc_now(), json.dumps({"pending_items": pending_count, "confirmed_identity_count": values["confirmed_artworks"].get("count", 0), "policy": "Provisional records may be archived privately; unconfirmed catalogue entries are omitted from public website."}, sort_keys=True)))
        state.db.commit()
        return {"status": "ADOPTED", "artifacts": targets, "details": {"provisional_artwork_count": summary.get("counts", {}).get("provisional_artwork_count", len(values["association_state"].get("artworks", []))), "confirmed_artwork_count": values["confirmed_artworks"].get("count", len(values["confirmed_artworks"].get("artworks", []))), "observation_count": len(values["association_state"].get("observations", [])), "association_count": len(values["association_state"].get("associations", [])), "review_pending": True}}
    if stage_id == "sfm":
        cfg = i["sfm"]
        stats = colmap_binary_counts(project_path(root, config, cfg["directory"]))
        prior = read_json(project_path(root, config, cfg["summary"])) if cfg.get("summary") else {}
        result = {**stats, "trusted_pose_count": prior.get("sfm_statistics", {}).get("trusted_pose_count"), "pose_outlier_count": prior.get("sfm_statistics", {}).get("pose_outlier_count"), "registered_ratio": prior.get("sfm_statistics", {}).get("registered_ratio"), "adoption_source": cfg.get("description", "Existing COLMAP binary reconstruction"), "adopted_not_executed": True, "sfm_configuration_sha256": json_digest(cfg)}
        target = output_dir / "sfm-summary.json"; write_json(target, result)
        return {"status": "ADOPTED", "artifacts": [target], "details": result}
    if stage_id == "training":
        cfg = i["model"]
        identity = ply_identity(project_path(root, config, cfg["path"]))
        for key in ("expected_sha256", "expected_size_bytes", "expected_gaussian_count"):
            expected = cfg.get(key)
            field = {"expected_sha256": "sha256", "expected_size_bytes": "size_bytes", "expected_gaussian_count": "gaussian_count"}[key]
            if expected is not None and identity[field] != expected:
                raise WorkflowError(f"Adopted model failed identity gate: {field} expected {expected}, got {identity[field]}")
        result = {**identity, "status": "ADOPTED_EXISTING_MODEL", "trainer": cfg.get("trainer", "unknown"), "training_rerun": False, "selection": "accepted baseline"}
        target = output_dir / "adopted-model.json"; write_json(target, result)
        selection = output_dir / "model-selection.json"
        write_json(selection, {"schema_version": "exhibition-archive.model-selection.v1", "project_id": config["project_id"], **result, "public_selection_allowed": True})
        return {"status": "ADOPTED", "artifacts": [target, selection], "details": result}
    if stage_id == "evaluation":
        cfg = i.get("evaluation", {})
        evidence = []
        for idx, item in enumerate(cfg.get("evidence", [])):
            path = project_path(root, config, item["path"] if isinstance(item, dict) else item)
            target = output_dir / f"evidence-{idx+1:02d}-{path.name}"
            _copy_json(path, target); evidence.append({"path": str(target), "source": str(path), "sha256": sha256_file(target)})
        result = {"schema_version": "exhibition-archive.evaluation.v1", "model_selection": "accepted baseline retained", "evidence": evidence, "human_assessment": cfg.get("human_assessment", "See the adopted fidelity reports; no new evaluation was run."), "limitations": cfg.get("limitations", []), "training_rerun": False}
        target = output_dir / "model-evaluation.json"; write_json(target, result)
        return {"status": "ADOPTED", "artifacts": [target], "details": result}
    if stage_id == "navigation":
        cfg = i["navigation"]
        if _navigation_mode(config) == "generate_from_sfm":
            raise WorkflowError("generate_from_sfm must use the navigation generation adapter")
        envelope_path = project_path(root, config, cfg["envelope"])
        policy_path = project_path(root, config, cfg["runtime_policy"])
        if _integration_mode(config, "training") == "spirula_fresh":
            from .model_registry import selected_model_record
            selected_model = selected_model_record(root, config, state)
            expected_model_sha = selected_model["sha256"]
        else:
            expected_model_sha = config["integrations"]["model"].get("expected_sha256")
        validation = validate_navigation(envelope_path, policy_path, expected_model_sha)
        env_target, pol_target = output_dir / "selected-envelope.json", output_dir / "runtime-policy.json"
        _copy_json(envelope_path, env_target)
        _copy_json(policy_path, pol_target)
        target = output_dir / "navigation-summary.json"; write_json(target, validation)
        return {"status": "ADOPTED", "artifacts": [target, env_target, pol_target], "details": validation}
    raise WorkflowError(f"No stage adapter is implemented for {stage_id}")


def _integration_mode(config: dict, stage_id: str) -> str:
    group = "sfm" if stage_id == "sfm" else "model"
    mode = config.get("integrations", {}).get(group, {}).get("mode", "adopt_existing")
    if mode not in {"adopt_existing", "spirula_fresh"}:
        raise WorkflowError(f"Unsupported {stage_id} mode {mode!r}; choose adopt_existing or spirula_fresh")
    return mode


def _validate_read_only_image_scope(root: Path, config: dict, image_dir: Path) -> None:
    source_dir = project_path(root, config, config["source"]["photo_directory"]).resolve()
    resolved = image_dir.resolve()
    within_originals = resolved == source_dir or source_dir in resolved.parents
    prep_settings = preparation_settings_for(config)
    managed_root = (root / prep_settings.get("dataset_directory", "derived/spirula-jpeg")).resolve()
    within_engine_managed = (managed_root in resolved.parents and
                             (resolved / DATASET_MANIFEST).is_file())
    within_registered_derived = False
    for raw in config.get("source", {}).get("derived_image_directories", []):
        declared = Path(raw).expanduser()
        if declared.is_absolute():
            continue
        allowed = (root / declared).resolve()
        if root.resolve() in allowed.parents and (resolved == allowed or allowed in resolved.parents):
            within_registered_derived = True
            break
    if not within_originals and not within_registered_derived and not within_engine_managed:
        raise BackendError(f"Fresh SfM image directory must be inside the configured read-only source photo directory: {resolved}", failure_kind="INVALID_INPUT")
    if not resolved.is_dir():
        raise BackendError(f"Fresh SfM image directory is missing: {resolved}", failure_kind="INVALID_INPUT")


def _validate_image_derivative_manifest(root: Path, config: dict, image_dir: Path, image_rows: list[dict], manifest_value: str | None) -> dict | None:
    source_dir = project_path(root, config, config["source"]["photo_directory"]).resolve()
    resolved = image_dir.resolve()
    if resolved == source_dir or source_dir in resolved.parents:
        return None
    if not manifest_value:
        raise BackendError("A derived image directory requires an explicit integrations.sfm.derived_image_manifest mapping", failure_kind="INVALID_INPUT")
    manifest_path = project_path(root, config, manifest_value).resolve()
    if root.resolve() not in manifest_path.parents or not manifest_path.is_file():
        raise BackendError("Derived image provenance manifest must be a project-local readable file", failure_kind="INVALID_INPUT")
    manifest = read_json(manifest_path)
    mappings = manifest.get("image_mappings")
    if not isinstance(mappings, list) or not mappings:
        raise BackendError("Derived image provenance manifest has no image_mappings", failure_kind="INVALID_INPUT")
    expected = {row["relative_path"]: row for row in image_rows}
    seen = set()
    for mapping in mappings:
        derived_rel = str(mapping.get("derived_relative_path", ""))
        source_rel = str(mapping.get("source_relative_path", ""))
        if not derived_rel or derived_rel in seen:
            raise BackendError("Derived image manifest contains a missing or duplicate output path", failure_kind="INVALID_INPUT")
        seen.add(derived_rel)
        derived = (resolved / derived_rel).resolve()
        source = (source_dir / source_rel).resolve()
        if resolved not in derived.parents or source_dir not in source.parents or not derived.is_file() or not source.is_file():
            raise BackendError("Derived image manifest maps an image outside its declared project directories", failure_kind="INVALID_INPUT")
        current = expected.get(derived_rel)
        if not current or current["sha256"] != mapping.get("derived_sha256"):
            raise BackendError(f"Derived image hash does not match current input inventory: {derived_rel}", failure_kind="INVALID_INPUT")
        if sha256_file(source) != mapping.get("source_sha256"):
            raise BackendError(f"Original source hash does not match derived image provenance: {source_rel}", failure_kind="INVALID_INPUT")
    if seen != set(expected):
        raise BackendError("Derived image manifest and selected image directory do not contain the same image set", failure_kind="INVALID_INPUT", details={"unmapped_outputs": sorted(set(expected)-seen), "missing_outputs": sorted(seen-set(expected))})
    return {"manifest_path": str(manifest_path), "manifest_sha256": sha256_file(manifest_path), "conversion": manifest.get("conversion"), "mapped_image_count": len(mappings), "source_images_modified": False}


def execute_fresh_sfm(root: Path, config: dict, state: ProjectState, output_dir: Path, execution_id: int,
                      authorization_plan_id: str | None = None, *, progress_format: str = "text",
                      verbose: bool = False) -> dict:
    cfg = config.get("integrations", {}).get("sfm", {})
    if _integration_mode(config, "sfm") != "spirula_fresh":
        return adopt_stage(root, config, state, "sfm", output_dir)
    image_dir, auto_manifest = effective_sfm_dataset(root, config, validate_files=True)
    _validate_read_only_image_scope(root, config, image_dir)
    dataset = backend_image_inventory(image_dir)
    auto_manifest_value = (str(preparation_manifest_path(root).relative_to(root)) if auto_manifest else None)
    derivative_provenance = _validate_image_derivative_manifest(
        root, config, image_dir, dataset["images"], auto_manifest_value or cfg.get("derived_image_manifest"))
    gamut_error = image_gamut_mismatch(config, auto_manifest)
    if gamut_error:
        raise BackendError(gamut_error, failure_kind="INVALID_CONFIGURATION")
    policy = config.get("resource_policy", {})
    attempt = create_attempt_dir(root, output_path(root, config), "sfm", execution_id, policy.get("retained_attempt_limit"))
    resources = check_resources(attempt, policy, "sfm")
    capabilities = discover_environment(cfg.get("executable"), attempt / "backend-discovery")
    output = attempt / "workspace"
    output.mkdir(mode=0o700)
    input_manifest = {"schema_version": "exhibition-archive.backend-input-manifest.v1", "project_id": config["project_id"], "source_directory": str(image_dir.resolve()), "image_count": dataset["image_count"], "formats": dataset["formats"], "images": dataset["images"], "dataset_selection": cfg.get("dataset_selection", "all images in configured read-only directory"), "source_photos_modified": False, "image_derivative_provenance": derivative_provenance, "unsupported_images": dataset["known_unsupported_images"]}
    input_manifest_path = attempt / "input-manifest.json"
    write_json(input_manifest_path, input_manifest)
    command = build_sfm_command(Path(capabilities["tool"]["executable"]), image_dir, output, cfg, capabilities)
    env_overrides = cfg.get("environment_overrides", {})
    if not isinstance(env_overrides, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env_overrides.items()):
        raise BackendError("sfm.environment_overrides must be an object of string values", failure_kind="INVALID_CONFIGURATION")
    private_environment_path = attempt / "environment-overrides.private.json"
    write_json(private_environment_path, env_overrides)
    try: private_environment_path.chmod(0o600)
    except OSError: pass
    command_record = {"stage": "sfm", "attempt_id": attempt.name, "engine_execution_id": execution_id, "command": command, "cwd": str(attempt.resolve()), "environment_override_keys": sorted(env_overrides), "resource_check": resources, "expected_outputs": [str(output / "features"), str(output / "matches.bin"), str(output / "sparse" / "0" / "cameras.bin"), str(output / "sparse" / "0" / "images.bin"), str(output / "sparse" / "0" / "points3D.bin")], "started_at_utc": utc_now(), "artifact_discovery_status": "NOT_STARTED", "validation_result": {"status": "NOT_RUN"}}
    write_json(attempt / "command.json", command_record)
    if not authorization_plan_id:
        raise AuthorizationRequired("sfm", "Fresh SfM execution requires an explicit one-time authorization. Run plan --full-hash, then authorize the displayed plan_id.")
    assert_authorization_plan_current(root, config, state, "sfm", authorization_plan_id, backend_info=capabilities)
    execution = execute_process(state, execution_id, "sfm", command, attempt, attempt / "spirula.log", env_overrides,
                               cfg.get("timeout_seconds", policy.get("sfm_timeout_seconds")), float(policy.get("poll_interval_seconds", 1.0)),
                               progress_format=progress_format, verbose=verbose,
                               progress_context={"target_images": dataset["image_count"]})
    command_record = read_json(attempt / "command.json")
    sparse_root = output / "sparse"
    backend_reprojection_summary = parse_spirula_sfm_reprojection_summary(execution.get("log_path"))
    validation = validate_colmap_directory(
        sparse_root, dataset["images"], cfg.get("quality_gate", {}),
        backend_reprojection_summary=backend_reprojection_summary)
    quality = validation["quality_gate"]
    partial_output = bool(execution.get("partial_output", False))
    if partial_output:
        validation["warnings"].append("SPIRULA_REPORTED_PARTIAL_OUTPUT")
        if quality == "SFM_VALID":
            quality = "SFM_VALID_WITH_WARNINGS"
            validation["quality_gate"] = quality
        validation["backend_partial_output"] = True
    if quality == "SFM_INVALID":
        raise BackendError("Spirula output failed COLMAP reconstruction integrity/quality validation", failure_kind="SFM_INVALID", details=validation)
    allow_warnings = bool(cfg.get("quality_gate", {}).get("allow_training_with_warnings", False))
    review_required = quality == "SFM_NEEDS_REVIEW" or (quality == "SFM_VALID_WITH_WARNINGS" and not allow_warnings)
    if quality == "SFM_VALID_WITH_WARNINGS" and allow_warnings:
        validation["warnings_accepted_by_configuration"] = True
    validation_path = attempt / "sfm-validation.json"
    write_json(validation_path, validation)
    produced = []
    for path in sorted(output.rglob("*")):
        if path.is_file() and not path.is_symlink():
            produced.append({"relative_path": path.relative_to(attempt).as_posix(), "size_bytes": path.stat().st_size, "category": "sparse-model" if "sparse" in path.parts else "sfm-intermediate"})
    command_record.update({"ended_at_utc": utc_now(), "exit_code": execution["exit_code"], "elapsed_seconds": execution["elapsed_seconds"], "produced_files": produced, "quality_gate": quality, "backend_partial_output": partial_output, "output_directory": str(output.resolve()), "reconstruction_directory": validation["primary_component"]})
    write_json(attempt / "command.json", command_record)
    finalize_attempt_record(attempt, "PARTIAL_OUTPUT" if partial_output else "SUCCEEDED", exit_code=execution["exit_code"], pid=execution["pid"],
        log_path=execution["log_path"], elapsed_seconds=execution["elapsed_seconds"],
        artifact_discovery_status="FOUND_VALIDATED", validation_result={"status": "PASSED_STRUCTURAL_VALIDATION", "quality_gate": quality, "backend_partial_output": partial_output, "validation_path": str(validation_path.resolve())},
        recovery_guidance="Spirula reported partial output; inspect the validated components and warnings before deciding whether a new authorized attempt is justified." if partial_output else "The process completed; downstream training remains blocked if the production SfM quality gate requires review.")
    primary = validation["components"][0]
    summary = {"schema_version": "exhibition-archive.sfm-summary.v2", "project_id": config["project_id"], "backend": "spirula", "backend_version": capabilities["tool"]["version"], "execution_status": "COMPLETED_WITH_PARTIAL_OUTPUT" if partial_output else "COMPLETED", "backend_partial_output": partial_output, "quality_gate": quality, "registered_image_count": primary["registered_image_count"], "source_image_count": dataset["image_count"], "registered_image_ratio": validation["registered_image_ratio"], "camera_count": primary["camera_count"], "camera_pose_count": primary["registered_image_count"], "camera_distribution": primary["camera_distribution"], "sparse_point_count": primary["sparse_point_count"], "component_count": validation["component_count"], "mean_track_length": primary["mean_track_length"], "mean_point_reprojection_error_px": primary["mean_point_reprojection_error_px"], "reprojection_metrics": primary.get("reprojection_metrics"), "backend_observation_reprojection_summary": validation.get("backend_observation_reprojection_summary"), "warnings": validation["warnings"], "reconstruction_directory": validation["primary_component"], "effective_image_directory": str(image_dir.resolve()), "image_derivative_provenance": derivative_provenance, "input_manifest": str(input_manifest_path.resolve()), "attempt_directory": str(attempt.resolve()), "log_path": execution["log_path"], "command": command, "environment_overrides_private_record": str(private_environment_path.resolve()), "resource_check": resources, "source_photo_hashes_recorded": True, "produced_file_count": len(produced), "limitations": ["Technical COLMAP integrity does not guarantee a suitable 3DGS training set or faithful artwork reconstruction.", "Camera-center coordinates are arbitrary non-metric SfM units and do not by themselves establish artwork-surface coverage or visitor-safe navigation.", "The installed help does not enumerate all accepted source image formats; this run records what Spirula actually accepts."]}
    summary_path = output_dir / "sfm-summary.json"
    write_json(summary_path, summary)
    artifacts = [summary_path, validation_path, input_manifest_path, attempt / "command.json", attempt / "spirula.log"]
    primary_dir = Path(validation["primary_component"])
    for filename in ("cameras.bin", "images.bin", "points3D.bin"):
        artifacts.append(primary_dir / filename)
    return {"status": "NEEDS_REVIEW" if review_required else "COMPLETED", "artifacts": artifacts,
            "artifact_types": {str(path): ("colmap-binary" if path.suffix == ".bin" else ("json" if path.suffix == ".json" else "text")) for path in artifacts},
            "details": {**summary, "sfm_validation_path": str(validation_path.resolve()), "quality_review_required": review_required, "training_may_start": not review_required}}


def execute_fresh_training(root: Path, config: dict, state: ProjectState, output_dir: Path, execution_id: int,
                           authorization_plan_id: str | None = None, *, progress_format: str = "text",
                           verbose: bool = False) -> dict:
    cfg = config.get("integrations", {}).get("model", {})
    if _integration_mode(config, "training") != "spirula_fresh":
        return adopt_stage(root, config, state, "training", output_dir)
    if cfg.get("run_type") not in {"smoke_test", "candidate"}:
        raise BackendError("Fresh training requires model.run_type to be explicitly smoke_test or candidate", failure_kind="INVALID_CONFIGURATION")
    if "iterations" not in cfg or "max_training_iterations" not in config.get("resource_policy", {}):
        raise BackendError("Fresh training requires explicit model.iterations and resource_policy.max_training_iterations", failure_kind="INVALID_CONFIGURATION")
    maximum = int(config["resource_policy"]["max_training_iterations"])
    if not isinstance(cfg["iterations"], int) or cfg["iterations"] < 1:
        raise BackendError("model.iterations must be a positive integer", failure_kind="INVALID_CONFIGURATION")
    if cfg["iterations"] > maximum:
        raise BackendError(f"Requested {cfg['iterations']} training iterations exceed the configured cap {maximum}", failure_kind="RESOURCE_LIMIT")
    training_limits = validate_training_resource_policy(config, cfg)
    resume_output_dir, resume_identity = training_resume_identity(root, config, cfg)
    from .sfm_review import training_component_scope
    sfm_scope = training_component_scope(root, config, state)
    sfm_state = state.stage("sfm") or {}
    sfm_cfg = config.get("integrations", {}).get("sfm", {})
    reconstruction = Path(sfm_scope["reconstruction_directory"])
    if not reconstruction.is_dir(): raise BackendError(f"Validated COLMAP directory is missing: {reconstruction}", failure_kind="SFM_INVALID")
    image_dir, auto_manifest = effective_training_dataset(root, config)
    _validate_read_only_image_scope(root, config, image_dir)
    dataset_images = backend_image_inventory(image_dir)
    source_by_name = {row["relative_path"]: row for row in dataset_images["images"]}
    registered_names = list(sfm_scope["registered_image_names"])
    missing_registered = sorted(set(registered_names) - set(source_by_name))
    if missing_registered:
        raise BackendError("Accepted SfM component references images missing from the effective training dataset",
                           failure_kind="SFM_INVALID", details={"missing_images": missing_registered[:20]})
    selected_source_rows = [source_by_name[name] for name in registered_names]
    sfm_source_by_name = {row["relative_path"]: row for row in sfm_scope["registered_image_rows"]}
    changed_registered = [name for name in registered_names
                          if source_by_name[name]["sha256"] != sfm_source_by_name[name]["sha256"]]
    if changed_registered:
        raise BackendError("Registered training image hashes differ from the images bound to the SfM acceptance",
                           failure_kind="SFM_INPUTS_CHANGED", details={"changed_images": changed_registered[:20]})
    auto_manifest_value = (str(preparation_manifest_path(root).relative_to(root)) if auto_manifest else None)
    derivative_provenance = _validate_image_derivative_manifest(
        root, config, image_dir, dataset_images["images"], auto_manifest_value or sfm_cfg.get("derived_image_manifest"))
    sfm_snapshot = sfm_scope["sfm_snapshot"]
    source_validation = sfm_snapshot["validation"]
    input_quality = sfm_snapshot["original_quality_gate"]
    original_warnings = list(sfm_snapshot["original_quality_warnings"])
    current_warnings = list(sfm_snapshot["current_revalidation_warnings"])
    current_quality = sfm_snapshot["current_revalidation_quality_gate"]
    allow_warnings = bool(sfm_cfg.get("quality_gate", {}).get("allow_training_with_warnings", False))
    if input_quality == "SFM_INVALID" or current_quality == "SFM_INVALID":
        raise BackendError("Training input failed current COLMAP validation: SFM_INVALID", failure_kind="QUALITY_GATE_BLOCKED", details=source_validation)
    if ((sfm_state.get("status") == "NEEDS_REVIEW" or input_quality == "SFM_NEEDS_REVIEW" or
         current_quality == "SFM_NEEDS_REVIEW" or
         (input_quality == "SFM_VALID_WITH_WARNINGS" and not allow_warnings) or
         (current_quality == "SFM_VALID_WITH_WARNINGS" and not allow_warnings)) and sfm_scope["acceptance"] is None):
        raise BackendError("Training requires a current component-scoped experimental acceptance for this SfM result",
                           failure_kind="QUALITY_GATE_BLOCKED", details=source_validation)
    selected_validation = validate_colmap_directory(reconstruction, selected_source_rows, sfm_cfg.get("quality_gate", {}))
    if selected_validation["quality_gate"] == "SFM_INVALID":
        raise BackendError("Selected component failed structural COLMAP validation", failure_kind="SFM_INVALID", details=selected_validation)
    input_validation = {
        "schema_version": "exhibition-archive.training-input-validation.v1",
        "quality_gate": input_quality,
        "warnings": original_warnings,
        "current_revalidation_quality_gate": current_quality,
        "current_revalidation_warnings": current_warnings,
        "source_sfm_validation": source_validation,
        "selected_component_validation": selected_validation,
        "selected_component_id": sfm_scope["component_id"],
        "selected_registered_image_count": sfm_scope["registered_image_count"],
        "selected_registered_image_membership_sha256": sfm_scope["registered_image_membership_sha256"],
        "sfm_component_acceptance": sfm_scope["acceptance"],
        "final_archive_completeness_accepted": False,
    }
    policy = config.get("resource_policy", {})
    attempt = create_attempt_dir(root, output_path(root, config), "training", execution_id, policy.get("retained_attempt_limit"))
    # A resume writes checkpoints beside the selected prior checkpoint, which
    # may reside on a different filesystem from this new Engine attempt log.
    resources = check_resources(resume_output_dir or attempt, policy, "training")
    capabilities = discover_environment(cfg.get("executable"), attempt / "backend-discovery")
    training_dataset = prepare_training_dataset(attempt, image_dir, reconstruction,
                                                registered_image_names=registered_names)
    materialized_images = backend_image_inventory(training_dataset / "images")
    materialized_names = sorted(row["relative_path"] for row in materialized_images["images"])
    if materialized_names != sorted(registered_names):
        raise BackendError("Prepared training dataset does not exactly match the selected COLMAP registered-image membership",
                           failure_kind="INVALID_INPUT", details={"expected_count": len(registered_names),
                                                                    "actual_count": len(materialized_names)})
    resolution_request = _training_resolution_request(training_dataset / "images", int(cfg.get("resolution_divisor", 1)))
    selected_formats = materialized_images["formats"]
    source_names = set(source_by_name)
    excluded_names = sorted(source_names - set(registered_names))
    run_name = resume_output_dir.name if resume_output_dir else f"{config['project_id']}-attempt-{execution_id:04d}"
    command = build_training_command(Path(capabilities["tool"]["executable"]), training_dataset, attempt, cfg, capabilities, run_name,
                                     resume_output_dir)
    effective_training_configuration = training_preset_configuration(
        Path(capabilities["tool"]["executable"]), cfg["preset"], cfg.get("quality", "high"), command)
    backend_output_dir = resume_output_dir or (attempt / "outputs" / run_name)
    checkpoint_root = resume_output_dir or (attempt / "outputs")
    checkpoint_policy = {"steps_per_save": cfg.get("steps_per_save", 100),
                         "save_only_latest_checkpoint": cfg.get("save_only_latest_checkpoint", True),
                         "save_full_checkpoint": cfg.get("save_full_checkpoint", True),
                         "checkpoint_replacement_reserve_bytes": training_limits["checkpoint_replacement_reserve_bytes"],
                         "checkpoint_replacement_reserve_basis": training_limits["checkpoint_replacement_reserve_basis"],
                         "checkpoint_replacement_overlap_assumption": training_limits["checkpoint_replacement_overlap_assumption"],
                         "minimum_required_at_launch_bytes": training_limits["minimum_required_at_launch_bytes"],
                         "retention_owner": "SPIRULA_BACKEND"}
    env_overrides = cfg.get("environment_overrides", {})
    if not isinstance(env_overrides, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env_overrides.items()):
        raise BackendError("model.environment_overrides must be an object of string values", failure_kind="INVALID_CONFIGURATION")
    private_environment_path = attempt / "environment-overrides.private.json"
    write_json(private_environment_path, env_overrides)
    try: private_environment_path.chmod(0o600)
    except OSError: pass
    selected_identity = [{"image_id": row["image_id"], "sha256": row["sha256"]} for row in selected_source_rows]
    training_manifest = {
        "schema_version": "exhibition-archive.backend-input-manifest.v2",
        "project_id": config["project_id"],
        "source_directory": str(image_dir.resolve()),
        "source_image_count": dataset_images["image_count"],
        "source_image_inventory_sha256": json_digest([{"relative_path": row["relative_path"], "sha256": row["sha256"]}
                                                        for row in dataset_images["images"]]),
        "image_count": len(selected_source_rows),
        "registered_image_count": len(selected_source_rows),
        "registered_image_names": registered_names,
        "registered_image_membership_sha256": sfm_scope["registered_image_membership_sha256"],
        "excluded_source_image_count": len(excluded_names),
        "excluded_source_images": [{"relative_path": name, "sha256": source_by_name[name]["sha256"]} for name in excluded_names],
        "formats": selected_formats,
        "training_resolution": resolution_request,
        "effective_training_configuration": effective_training_configuration,
        "images": selected_source_rows,
        "image_derivative_provenance": derivative_provenance,
        "reconstruction_directory": str(reconstruction.resolve()),
        "sfm_component_id": sfm_scope["component_id"],
        "sfm_component_artifact_tree_sha256": sfm_scope["component"]["artifact_tree_sha256"],
        "sfm_reconstruction_binding_sha256": sfm_scope["sfm_snapshot"]["binding_sha256"],
        "sfm_output_digest": sfm_state.get("output_digest"),
        "sfm_component_acceptance": sfm_scope["acceptance"],
        "dataset_links": {"images": str((training_dataset / "images").resolve()),
                          "sparse/0": str((training_dataset / "sparse/0").resolve())},
        "run_type": cfg["run_type"],
        "training_profile_provenance": config.get("training_profile"),
        "source_photos_modified": False,
    }
    manifest_path = attempt / "training-input-manifest.json"
    write_json(manifest_path, training_manifest)
    input_validation_path = attempt / "training-input-validation.json"
    write_json(input_validation_path, input_validation)
    command_record = {"stage": "training", "attempt_id": attempt.name, "engine_execution_id": execution_id, "backend_version": capabilities["tool"]["version"], "run_name": run_name, "run_type": cfg["run_type"], "configuration": {k:v for k,v in cfg.items() if k not in {"environment_overrides"}}, "training_profile_provenance": config.get("training_profile"), "effective_training_configuration": effective_training_configuration, "training_resolution": resolution_request, "command": command, "cwd": str(attempt.resolve()), "environment_override_keys": sorted(env_overrides), "resource_check": resources, "resource_limits": training_limits, "checkpoint_policy": checkpoint_policy, "resume_checkpoint_identity": resume_identity, "expected_output_prefix": str(backend_output_dir.resolve()), "checkpoint_resume_capability": capabilities["training"]["checkpoint"], "started_at_utc": utc_now(), "artifact_discovery_status": "NOT_STARTED", "validation_result": {"status": "NOT_RUN"}}
    write_json(attempt / "command.json", command_record)
    if not authorization_plan_id:
        raise AuthorizationRequired("training", "Fresh training execution requires an explicit one-time authorization. Run plan --full-hash, then authorize the displayed plan_id.")
    assert_authorization_plan_current(root, config, state, "training", authorization_plan_id, backend_info=capabilities)
    execution = execute_process(state, execution_id, "training", command, attempt, attempt / "spirula.log", env_overrides,
                               training_limits["training_timeout_seconds"], float(policy.get("poll_interval_seconds", 1.0)),
                               checkpoint_root=checkpoint_root,
                               minimum_free_disk_bytes=training_limits["minimum_free_disk_bytes_training"],
                               progress_format=progress_format, verbose=verbose,
                               progress_context={"target_iterations": cfg["iterations"]})
    command_record = read_json(attempt / "command.json")
    training_resolution = _training_resolution_from_log(resolution_request, Path(execution["log_path"]))
    command_record["training_resolution"] = training_resolution
    from .backends import discover_checkpoints
    checkpoints = discover_checkpoints(checkpoint_root)
    candidates = discover_model_files(backend_output_dir, cfg.get("artifact_selector"))
    model_validation = validate_gaussian_ply(candidates[0])
    model_validation.update({"producer": "Spirula", "producer_version": capabilities["tool"]["version"], "attempt_id": attempt.name,
                             "run_type": cfg["run_type"], "smoke_test_marker": "SMOKE_TEST_MODEL" if cfg["run_type"] == "smoke_test" else None,
                             "training_configuration": {k:v for k,v in cfg.items() if k not in {"environment_overrides"}},
                             "source_dataset_identity": hashlib.sha256(json.dumps(selected_identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                             "sfm_input_identity": sfm_scope["sfm_snapshot"]["binding_sha256"],
                             "sfm_component_id": sfm_scope["component_id"],
                             "sfm_component_acceptance_audit_identity": (sfm_scope["acceptance"] or {}).get("audit_identity"),
                             "public_selection_allowed": False})
    model_path = candidates[0]
    validation_path = attempt / "model-validation.json"
    write_json(validation_path, model_validation)
    command_record.update({"ended_at_utc": utc_now(), "exit_code": execution["exit_code"], "elapsed_seconds": execution["elapsed_seconds"], "model_path": str(model_path.resolve()), "model_sha256": model_validation["sha256"], "gaussian_count": model_validation["gaussian_count"], "model_bytes": model_validation["file_bytes"]})
    write_json(attempt / "command.json", command_record)
    finalize_attempt_record(attempt, "SUCCEEDED", exit_code=execution["exit_code"], pid=execution["pid"],
        log_path=execution["log_path"], elapsed_seconds=execution["elapsed_seconds"],
        artifact_discovery_status="FOUND_VALIDATED", validation_result={"status": "PLY_SCHEMA_VALID", "model_path": str(model_path.resolve()), "model_sha256": model_validation["sha256"], "gaussian_count": model_validation["gaussian_count"]},
        recovery_guidance="The model artifact is validated as a technical candidate; publication still requires the existing independent review and navigation gates.")
    run_path = output_dir / "training-run.json"
    run_report = {"schema_version": "exhibition-archive.training-run.v1", "project_id": config["project_id"], "attempt_id": attempt.name,
                  "status": "MODEL_VALIDATED", "model_status": model_validation["smoke_test_marker"] or "UNREVIEWED_CANDIDATE",
                  "public_selection_allowed": False, "backend": "spirula", "backend_version": capabilities["tool"]["version"],
                  "elapsed_seconds": execution["elapsed_seconds"], "exit_code": execution["exit_code"], "log_path": execution["log_path"],
                  "command": command, "configuration": command_record["configuration"],
                  "training_profile_provenance": config.get("training_profile"),
                  "effective_training_configuration": effective_training_configuration, "resource_check": resources,
                  "resource_limits": training_limits, "checkpoint_policy": checkpoint_policy,
                  "resume_checkpoint_identity": resume_identity,
                  "checkpoint_resume_capability": capabilities["training"]["checkpoint"], "checkpoint_resume_execution_status": "NOT_TESTED",
                  "discovered_checkpoints": checkpoints,
                  "model": model_validation, "input_manifest": str(manifest_path.resolve()), "input_validation_report": str(input_validation_path.resolve()),
                  "training_resolution": training_resolution,
                  "sfm_input_quality_gate": input_validation["quality_gate"],
                  "sfm_input_warnings": input_validation["warnings"],
                  "sfm_current_revalidation_quality_gate": input_validation["current_revalidation_quality_gate"],
                  "sfm_current_revalidation_warnings": input_validation["current_revalidation_warnings"],
                  "sfm_component_id": sfm_scope["component_id"],
                  "sfm_component_acceptance_audit_identity": (sfm_scope["acceptance"] or {}).get("audit_identity"),
                  "training_registered_image_count": len(selected_source_rows),
                  "training_source_directory_image_count": dataset_images["image_count"],
                  "image_derivative_provenance": derivative_provenance,
                  "training_dataset_workspace": str(training_dataset.resolve()),
                  "environment_overrides_private_record": str(private_environment_path.resolve()),
                  "limitations": ["This output is a technical smoke/candidate artifact, not a curator-approved exhibition model.", "Training loss, convergence and visual fidelity were not evaluated by the orchestration backend.", "Optimizer-state resume is advertised by CLI help but has not been executed or integrated."]}
    write_json(run_path, run_report)
    artifacts = [model_path, validation_path, run_path, manifest_path, input_validation_path, attempt / "command.json", attempt / "spirula.log"]
    return {"status": "COMPLETED", "artifacts": artifacts,
            "artifact_types": {str(path): ("3dgs-ply" if path.suffix.casefold() == ".ply" else ("text" if path.suffix == ".log" else "json")) for path in artifacts},
            "details": {"model_status": run_report["model_status"], "run_type": cfg["run_type"], "public_selection_allowed": False, "model_path": str(model_path.resolve()), "model_sha256": model_validation["sha256"], "gaussian_count": model_validation["gaussian_count"], "file_bytes": model_validation["file_bytes"], "validation_path": str(validation_path.resolve()), "training_run_path": str(run_path.resolve()), "attempt_directory": str(attempt.resolve()), "log_path": execution["log_path"], "backend_version": capabilities["tool"]["version"], "training_iterations_requested": cfg["iterations"], "training_profile_provenance": config.get("training_profile"), "training_effective_configuration_status": effective_training_configuration["status"], "training_effective_configuration_sha256": json_digest(effective_training_configuration), "training_resolution": training_resolution, "training_progress": "FINAL_EXIT_ONLY; intermediate progress is available only in the log", "checkpoint_resume_execution_status": "NOT_TESTED", "discovered_checkpoints": checkpoints, "sfm_component_id": sfm_scope["component_id"], "sfm_component_acceptance_audit_identity": (sfm_scope["acceptance"] or {}).get("audit_identity"), "training_registered_image_count": len(selected_source_rows), "training_source_directory_image_count": dataset_images["image_count"]}}


def fresh_evaluation_stage(root: Path, config: dict, state: ProjectState, output_dir: Path) -> dict:
    training = state.stage("training") or {}
    details = json.loads(training.get("details_json") or "{}")
    gate_row = state.db.execute("SELECT * FROM review_gates WHERE gate_id='fresh-model-selection'").fetchone()
    gate_status = gate_row["status"] if gate_row else "PENDING"
    candidate = None
    candidate_error = None
    try:
        from .model_registry import fresh_candidate_record
        candidate = fresh_candidate_record(root, config, state)
    except BackendError as exc:
        candidate_error = str(exc)
    approved = False
    selection = None
    if gate_status == "APPROVED" and candidate:
        decision = json.loads(gate_row["decision_json"] or "{}")
        approved = candidate["run_type"] == "candidate" and decision.get("candidate_sha256") == candidate["sha256"] and decision.get("candidate_path") == candidate["path"]
        if approved:
            selection = {"schema_version": "exhibition-archive.model-selection.v1", "project_id": config["project_id"],
                         **candidate, "selection": "human-approved fresh candidate", "review": decision,
                         "public_selection_allowed": True, "note": "Approval selects this candidate for downstream evaluation/navigation/package prerequisites; publication rights and navigation remain separately gated."}
    report_status = "COMPLETED" if approved else "NEEDS_REVIEW"
    report = {"schema_version": "exhibition-archive.evaluation.v2", "project_id": config["project_id"],
              "status": "HUMAN_APPROVED_CANDIDATE" if approved else "NEEDS_REVIEW", "model_selection": "HUMAN_SELECTED" if approved else "NOT_SELECTED", "candidate_model": details.get("model_path"),
              "candidate_sha256": details.get("model_sha256"), "candidate_status": details.get("model_status"),
              "candidate_validation_error": candidate_error, "review_gate_status": gate_status,
              "automatic_publication": False, "human_review_required": not approved,
              "review_requirements": [] if approved else ["Inspect model fidelity across viewpoints", "Confirm artwork identities and rights", "Provide or review a navigation enclosure"],
              "note": "Fresh Spirula output is registered as a candidate only. It is not automatically selected for public archival publication." if not approved else "An explicit human review selected the validated fresh candidate. Navigation, reproduction rights, package verification, and any publication decision remain independent requirements."}
    target = output_dir / "model-evaluation.json"
    write_json(target, report)
    artifacts = [target]
    if selection:
        selection_path = output_dir / "model-selection.json"
        write_json(selection_path, selection)
        artifacts.append(selection_path)
    if not gate_row:
        state.db.execute("INSERT INTO review_gates(gate_id,status,required,updated_at,decision_json) VALUES(?,?,?,?,?)", ("fresh-model-selection", "PENDING", 1, utc_now(), json.dumps(report, sort_keys=True)))
    state.db.commit()
    return {"status": report_status, "artifacts": artifacts, "details": report}


def _stage_execution_artifacts_valid(root: Path, config: dict, state: ProjectState, stage_id: str,
                                     execution: dict, *, expected_output_digest: str | None = None) -> bool:
    """Validate one historical execution's artifact records without trusting its current stage pointer."""
    if execution.get("status") not in {"COMPLETED", "ADOPTED"}:
        return False
    output_digest = execution.get("output_digest")
    if not output_digest or (expected_output_digest is not None and output_digest != expected_output_digest):
        return False
    try:
        details = json.loads(execution.get("details_json") or "{}")
        paths = [Path(value).resolve() for value in details.get("artifacts", [])]
        if not paths or not all(path.is_file() for path in paths):
            return False
        relative_digest_rows = []
        absolute_digest_rows = []
        for path in paths:
            artifact = state.db.execute(
                "SELECT sha256,size_bytes,status FROM artifacts WHERE stage_id=? AND path=?",
                (stage_id, str(path)),
            ).fetchone()
            if not artifact or artifact["status"] != "VALIDATED":
                return False
            actual_sha256 = sha256_file(path)
            if actual_sha256 != artifact["sha256"] or path.stat().st_size != artifact["size_bytes"]:
                return False
            absolute_digest_rows.append({"path": str(path), "sha256": actual_sha256})
            try:
                relative_path = path.relative_to(Path(root).resolve())
            except ValueError:
                relative_path = None
            if relative_path is not None:
                relative_digest_rows.append({"path": relative_path.as_posix(), "sha256": actual_sha256})
        if (json_digest(absolute_digest_rows) != output_digest and
                (not relative_digest_rows or json_digest(relative_digest_rows) != output_digest)):
            return False
        if stage_id == "sfm_dataset_prep" and not validate_preparation_stage_outputs(root, config):
            return False
        return True
    except Exception:
        return False


def _stage_artifacts_valid(state: ProjectState, stage_id: str, output_dir: Path) -> bool:
    row = state.stage(stage_id)
    if not row or row.get("status") not in {"COMPLETED", "ADOPTED"}:
        return False
    execution_row = state.db.execute("SELECT * FROM stage_executions WHERE execution_id=?",
                                     (row.get("last_execution_id"),)).fetchone()
    if not execution_row:
        return False
    execution = dict(execution_row)
    if stage_id == "sfm_dataset_prep":
        try:
            config = json.loads((state.root / "project.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return _stage_execution_artifacts_valid(state.root, config, state, stage_id, execution,
                                                expected_output_digest=row.get("output_digest"))
    try:
        paths = json.loads(execution.get("details_json") or "{}").get("artifacts", [])
        return bool(paths) and all(Path(path).is_file() for path in paths)
    except Exception:
        return False


def _interruption_details(details: dict, *, exit_code: int, evidence: str,
                          exit_code_source: str, reconciled: bool) -> dict:
    result = dict(details)
    result.update({
        "process_state": "INTERRUPTED",
        "exit_code": exit_code,
        "exit_code_source": exit_code_source,
        "interruption_evidence": evidence,
        "reconciled_at_utc": utc_now(),
        "error": "Workflow execution was interrupted; no automatic retry or process reattachment was attempted.",
    })
    if reconciled:
        result["reconciliation"] = "OPERATOR_CONFIRMED_INACTIVE_PROCESS"
    return result


def _mark_active_run_interrupted(state: ProjectState, run_id: int, summary: dict) -> None:
    run = state.db.execute("SELECT status FROM workflow_runs WHERE run_id=?", (run_id,)).fetchone()
    if not run or run["status"] != "RUNNING":
        return
    evidence = "Python KeyboardInterrupt; conventional SIGINT exit code 130."
    for row in state.db.execute("SELECT * FROM stage_executions WHERE run_id=? AND status='RUNNING'",
                                (run_id,)).fetchall():
        execution = dict(row)
        details = _interruption_details(json.loads(execution.get("details_json") or "{}"),
                                        exit_code=130, evidence=evidence,
                                        exit_code_source="python_keyboard_interrupt", reconciled=False)
        state.finish_stage(execution["execution_id"], "INTERRUPTED", None, details, details["error"])
        stage = state.stage(execution["stage_id"])
        if stage and stage.get("last_execution_id") == execution["execution_id"]:
            state.set_stage(execution["stage_id"], "FAILED", execution.get("input_fingerprint"), None,
                            execution["execution_id"], details)
        summary.setdefault("interrupted_executions", []).append({
            "execution_id": execution["execution_id"], "stage_id": execution["stage_id"],
            "exit_code": 130,
        })
    summary["status"] = "INTERRUPTED"
    summary.setdefault("interruption_evidence", evidence)
    state.end_run(run_id, "INTERRUPTED", summary)


def reconcile_interrupted_execution(root: Path, config: dict, state: ProjectState, *,
                                    run_id: int, execution_id: int, exit_code: int,
                                    evidence: str, confirm_no_active_processes: bool) -> dict:
    """Reconcile one orphaned execution without retrying it or discarding valid prior cache."""
    if not isinstance(exit_code, int) or isinstance(exit_code, bool) or not 1 <= exit_code <= 255:
        raise WorkflowError("Interrupted-execution recovery requires a nonzero process exit code from 1 through 255")
    if not isinstance(evidence, str) or not evidence.strip():
        raise WorkflowError("Interrupted-execution recovery requires recorded process-check and interruption evidence")
    if not confirm_no_active_processes:
        raise WorkflowError("Confirm that no Engine, backend, or preparation-helper process remains active before recovery")

    try:
        lock_context = state.lock()
        lock_context.__enter__()
    except BlockingIOError as exc:
        raise WorkflowError("The project workflow lock is held; an Engine process may still be active") from exc
    try:
        run_row = state.db.execute("SELECT * FROM workflow_runs WHERE run_id=?", (run_id,)).fetchone()
        execution = state.execution(execution_id)
        if not run_row or run_row["status"] != "RUNNING":
            raise WorkflowError(f"Workflow run {run_id} is not recorded as RUNNING")
        if not execution or execution.get("run_id") != run_id or execution.get("status") != "RUNNING":
            raise WorkflowError(f"Execution {execution_id} is not a RUNNING execution in workflow run {run_id}")
        if execution.get("stage_id") != "sfm_dataset_prep":
            raise WorkflowError(
                "This recovery path is limited to interrupted local HEIC preparation; "
                "fresh SfM and training use their backend-specific reconciliation paths."
            )
        stage = state.stage(execution["stage_id"])
        if not stage or stage.get("status") != "RUNNING" or stage.get("last_execution_id") != execution_id:
            raise WorkflowError("The requested execution is not the current RUNNING stage attempt")
        other_running = state.db.execute(
            "SELECT execution_id,run_id,stage_id FROM stage_executions WHERE status='RUNNING' AND execution_id!=?",
            (execution_id,),
        ).fetchone()
        if other_running:
            raise WorkflowError(
                f"Another workflow execution remains RUNNING: {other_running['execution_id']} "
                f"(run {other_running['run_id']}, stage {other_running['stage_id']})"
            )
        if execution.get("pid"):
            command = json.loads(execution.get("command_json") or "[]")
            expected = command[0] if command else None
            if process_alive(int(execution["pid"]), expected):
                raise WorkflowError(f"Recorded process {execution['pid']} is still active; refusing recovery")

        details = _interruption_details(json.loads(execution.get("details_json") or "{}"),
                                        exit_code=exit_code, evidence=evidence.strip(),
                                        exit_code_source="operator_reported", reconciled=True)
        state.finish_stage(execution_id, "INTERRUPTED", None, details, details["error"])

        restored_execution = None
        if execution["stage_id"] == "sfm_dataset_prep":
            candidates = state.db.execute(
                "SELECT * FROM stage_executions WHERE stage_id=? AND execution_id<? "
                "AND status IN ('COMPLETED','ADOPTED') ORDER BY execution_id DESC",
                (execution["stage_id"], execution_id),
            ).fetchall()
            for candidate_row in candidates:
                candidate = dict(candidate_row)
                if _stage_execution_artifacts_valid(root, config, state, execution["stage_id"], candidate):
                    restored_execution = candidate
                    break

        if restored_execution:
            prior_details = json.loads(restored_execution.get("details_json") or "{}")
            state.set_stage(execution["stage_id"], restored_execution["status"],
                            restored_execution.get("input_fingerprint"),
                            restored_execution.get("output_digest"),
                            restored_execution["execution_id"], prior_details.get("details", {}))
        else:
            state.set_stage(execution["stage_id"], "FAILED", execution.get("input_fingerprint"), None,
                            execution_id, details)

        summary = json.loads(run_row["summary_json"] or "{}")
        summary.update({
            "status": "INTERRUPTED",
            "reconciliation": {
                "execution_id": execution_id,
                "stage_id": execution["stage_id"],
                "exit_code": exit_code,
                "exit_code_source": "operator_reported",
                "evidence": evidence.strip(),
                "restored_validated_execution_id": (restored_execution or {}).get("execution_id"),
                "automatic_retry": False,
            },
        })
        state.end_run(run_id, "INTERRUPTED", summary)
        return {
            "status": "RECONCILED_INTERRUPTED",
            "run_id": run_id,
            "execution_id": execution_id,
            "stage_id": execution["stage_id"],
            "exit_code": exit_code,
            "stage_status": restored_execution["status"] if restored_execution else "FAILED",
            "restored_validated_execution_id": (restored_execution or {}).get("execution_id"),
            "automatic_retry": False,
        }
    finally:
        lock_context.__exit__(None, None, None)


def plan(root: Path, config: dict, state: ProjectState, discover_backends: bool = False) -> list[dict]:
    stages = state.stages()
    result = []
    for stage in STAGES:
        sid = stage["id"]
        integration = config.get("integrations", {})
        planned_command = None
        if sid in {"capture_preflight", "artwork_association", "sfm", "training", "evaluation", "navigation"}:
            group = {"capture_preflight":"preflight", "artwork_association":"artwork", "sfm":"sfm", "training":"model", "evaluation":"evaluation", "navigation":"navigation"}[sid]
            group_cfg = integration.get(group, {})
            has_cfg = bool(group_cfg)
            configured_mode = group_cfg.get("mode", "adopt_existing") if group in {"sfm", "model", "navigation"} else "adopt_existing"
            mode = configured_mode.upper() if has_cfg else ("IMAGE_ONLY_ADAPTER" if sid == "capture_preflight" else "INTERFACE_ONLY")
            backend_probe_status = "unknown" if configured_mode == "spirula_fresh" else "not-required"
            readiness = "READY" if has_cfg else ("READY_WITH_IMAGE_ONLY" if sid == "capture_preflight" else "BLOCKED_NO_CONFIGURED_ARTIFACT")
            planned_command = None
            if has_cfg and sid in {"sfm", "training"} and configured_mode == "spirula_fresh":
                try:
                    backend = None
                    if discover_backends:
                        backend_probe_status = "unknown"
                        try:
                            backend = discover_environment(group_cfg.get("executable"))
                            backend_probe_status = "available"
                        except (BackendError, OSError, KeyError, TypeError, ValueError):
                            backend_probe_status = "unavailable"
                            raise
                    executable = Path(backend["tool"]["executable"]) if backend else Path(group_cfg.get("executable") or "spirula")
                    attempt_id = "<next-unique-attempt>"
                    attempt = output_path(root, config) / "backend-runs" / sid / attempt_id
                    if sid == "sfm":
                        images, prep_manifest = effective_sfm_dataset(root, config)
                        gamut_error = image_gamut_mismatch(config, prep_manifest)
                        if gamut_error:
                            raise BackendError(gamut_error, failure_kind="INVALID_CONFIGURATION")
                        planned_command = build_sfm_command(executable, images, attempt / "workspace", group_cfg, backend or {"sfm": {"auto_arguments": ["--max-image-size"]}})
                        if not images.is_dir(): readiness = "BLOCKED_IMAGE_DIRECTORY_MISSING"
                    else:
                        effective_training_dataset(root, config)
                        model_caps = backend or {"training": {"presets": [group_cfg.get("preset", "3dgs")], "arguments": ["--train-resolution-divisor", "--device", "--steps-per-save", "--save-only-latest-checkpoint", "--save-full-checkpoint", "--resume", "--quality", "--view-sampling", "--cache-images", "--load-masks", "--disable-viewer", "--keep-viewer-alive"], "checkpoint": {"latest_only_retention": "VERIFIED_BY_HELP", "latest_only_prunes_older": "VERIFIED_BY_HELP", "full_state": "VERIFIED_BY_HELP", "resume_cli_flag": "VERIFIED_BY_HELP"}}}
                        dataset_placeholder = attempt / "dataset"
                        resume_output_dir, _ = training_resume_identity(root, config, group_cfg)
                        planned_command = build_training_command(executable, dataset_placeholder, attempt, group_cfg, model_caps, f"{config['project_id']}-{attempt_id}", resume_output_dir)
                        validate_training_resource_policy(config, group_cfg)
                        if "iterations" not in group_cfg or "max_training_iterations" not in config.get("resource_policy", {}): readiness = "BLOCKED_EXPLICIT_ITERATION_CAP_REQUIRED"
                        elif group_cfg["iterations"] > config["resource_policy"]["max_training_iterations"]: readiness = "BLOCKED_ITERATION_CAP_EXCEEDED"
                    if planned_command and readiness == "READY": readiness = "READY_BACKEND_DRY_RUN"
                except DatasetPreparationRequired as exc:
                    readiness = f"BLOCKED_DATASET_PREPARATION_REQUIRED:{exc}"
                except WorkflowError as exc:
                    readiness = f"BLOCKED_TRAINING_CONFIGURATION:{exc}"
                except (BackendError, KeyError, TypeError, ValueError) as exc:
                    readiness = f"BLOCKED_BACKEND_CONFIGURATION:{exc}"
            if sid == "navigation" and has_cfg and configured_mode == "generate_from_sfm":
                fresh_sfm = integration.get("sfm", {}).get("mode") == "spirula_fresh"
                if not group_cfg.get("camera_centers") and not integration.get("sfm", {}).get("directory") and not fresh_sfm:
                    readiness = "BLOCKED_NO_CAMERA_POSE_SOURCE"
                elif group_cfg.get("scene_type") == "outdoor":
                    readiness = "READY_OPEN_AIR_CAMERA_COVERAGE_REQUIRES_REVIEW"
                elif not group_cfg.get("floor_plane") and not group_cfg.get("camera_centers"):
                    readiness = "REVIEW_REQUIRED_FLOOR_PLANE_NOT_CONFIGURED"
                else:
                    readiness = "READY_GENERATE_PROPOSAL_REQUIRES_REVIEW"
        elif sid == "sfm_dataset_prep":
            mode = "LOCAL"
            readiness = ("READY_SAFE_PREPARATION" if auto_preparation_required(root, config)
                         else "READY_NO_CONVERSION_REQUIRED")
        else:
            mode = "LOCAL"
            readiness = "READY"
        deps = list(stage["deps"])
        if sid == "sfm" and auto_preparation_required(root, config):
            deps.append("sfm_dataset_prep")
        missing_deps = [x for x in deps if stages.get(x, {}).get("status") not in {"COMPLETED", "ADOPTED"}]
        training_scope = None
        if sid == "training" and configured_mode == "spirula_fresh":
            from .sfm_review import training_component_scope
            try:
                training_scope = training_component_scope(root, config, state)
                if "sfm" in missing_deps:
                    missing_deps.remove("sfm")
            except BackendError as exc:
                readiness = f"BLOCKED_SFM_ACCEPTANCE_REQUIRED:{exc}"
        if missing_deps:
            if sid == "training" and readiness.startswith("BLOCKED_SFM_ACCEPTANCE_REQUIRED:"):
                other_missing = [item for item in missing_deps if item != "sfm"]
                if other_missing:
                    readiness += "; WAITING:" + ",".join(other_missing)
            elif not (sid == "sfm" and auto_preparation_required(root, config) and "sfm_dataset_prep" in missing_deps):
                readiness = "WAITING:" + ",".join(missing_deps)
            else:
                readiness = "BLOCKED_DATASET_PREPARATION_REQUIRED"
        if sid == "training" and training_scope and readiness in {"READY_BACKEND_DRY_RUN", "READY"}:
            readiness = ("READY_EXPERIMENTAL_TRAINING_WITH_SCOPED_ACCEPTANCE"
                         if training_scope["acceptance_required"] else "READY_BACKEND_DRY_RUN")
        row = {**stage, "deps": deps, "execution_mode": mode, "readiness": readiness, "current_status": stages.get(sid, {}).get("status", "PENDING")}
        if sid in {"sfm", "training"} and configured_mode == "spirula_fresh":
            row["backend_probe_status"] = backend_probe_status
        if planned_command is not None: row["planned_command"] = planned_command
        result.append(row)
    return result


def _execute_stage(root: Path, config: dict, state: ProjectState, stage_id: str, output_dir: Path, execution_id: int, fail_at: str | None = None, force_hash: bool = False,
                   authorization_plan_id: str | None = None, progress_format: str = "text",
                   verbose: bool = False) -> dict:
    if stage_id == "sfm_dataset_prep":
        inventory = read_json(output_dir / "source-index.json")
        result = prepare_spirula_dataset(root, config, inventory)
        manifest = Path(result["manifest_path"])
        return {"status": "COMPLETED", "artifacts": [manifest], "details": result["details"]}
    if stage_id == "lidar_inspection":
        return execute_lidar_inspection(root, config, output_dir)
    if stage_id == "sfm_studio_export":
        return execute_sfm_studio_export(root, config, state, output_dir)
    if stage_id in {"photo_inventory", "capture_preflight", "artwork_association", "sfm", "training", "evaluation", "navigation"}:
        if stage_id == "sfm": return execute_fresh_sfm(root, config, state, output_dir, execution_id, authorization_plan_id,
                                                        progress_format=progress_format, verbose=verbose)
        if stage_id == "training": return execute_fresh_training(root, config, state, output_dir, execution_id, authorization_plan_id,
                                                                  progress_format=progress_format, verbose=verbose)
        if stage_id == "evaluation" and _integration_mode(config, "training") == "spirula_fresh":
            return fresh_evaluation_stage(root, config, state, output_dir)
        if stage_id == "navigation" and _navigation_mode(config) == "generate_from_sfm":
            return execute_navigation_generation(root, config, state, output_dir, execution_id)
        return adopt_stage(root, config, state, stage_id, output_dir)
    if stage_id == "lidar_registration":
        return execute_lidar_registration(root, config, state, output_dir, execution_id)
    if stage_id == "first_visual":
        from .preview import build_first_visual_preview
        return build_first_visual_preview(root, config, state, output_dir, execution_id)
    if stage_id == "viewer_package":
        from .publisher import build_website
        details = build_website(root, config, state, output_dir)
        if fail_at == stage_id:
            raise WorkflowError("Injected one-shot failure after website assembly, before stage validation/commit")
        return details
    if stage_id == "archive_package":
        result = build_archive(root, config, state, output_dir)
        if fail_at == stage_id:
            raise WorkflowError("Injected one-shot archive packaging failure before stage validation/commit")
        return result
    if stage_id == "verification":
        result = verify_outputs(root, config, output_dir, state=state, force_hash=force_hash)
        target = output_dir / "verification-report.json"; write_json(target, result)
        if fail_at == stage_id:
            raise WorkflowError("Injected verification failure before stage validation/commit")
        if result.get("status") != "PASS":
            raise WorkflowError(f"Archive verification failed: {result.get('failures', [])}")
        return {"status": "COMPLETED", "artifacts": [target], "details": result}
    raise WorkflowError(f"Unknown stage: {stage_id}")


def run_navigation_generation_only(root: Path, config: dict, state: ProjectState) -> dict[str, Any]:
    """Regenerate only the navigation proposal from the current validated SfM result."""
    if _navigation_mode(config) != "generate_from_sfm":
        raise WorkflowError("Navigation-only generation requires integrations.navigation.mode=generate_from_sfm")
    sfm = state.stage("sfm") or {}
    sfm_fingerprint = stage_fingerprint(root, config, state, "sfm", full_hash=True)
    if (sfm.get("status") not in {"COMPLETED", "ADOPTED"} or
            sfm.get("input_fingerprint") != sfm_fingerprint or
            not _stage_artifacts_valid(state, "sfm", output_path(root, config) / "work")):
        raise WorkflowError("Navigation-only generation requires a current, validated SfM stage; no upstream stage was run.")

    fingerprint = stage_fingerprint(root, config, state, "navigation", full_hash=True)
    stage_root = output_path(root, config) / "work"
    stage_root.mkdir(parents=True, exist_ok=True)
    run_id = state.start_run("navigation_generate_only")
    execution_id = state.start_stage(
        run_id, "navigation", fingerprint, False,
        {"stage_contract": next(stage for stage in STAGES if stage["id"] == "navigation"),
         "execution_context": {"kind": "operator-initiated-local-cli", "backend_execution": False,
                               "upstream_stages_executed": []}},
    )
    state.set_stage("navigation", "RUNNING", fingerprint, None, execution_id,
                    {"navigation_only": True, "upstream_stages_executed": []})
    summary: dict[str, Any] = {
        "run_id": run_id, "mode": "navigation_generate_only", "status": "RUNNING",
        "stages": {"navigation": "RUNNING"}, "completed": [], "needs_review": [],
        "blocked": [], "skipped": list(stage["id"] for stage in STAGES if stage["id"] != "navigation"),
    }
    try:
        result = execute_navigation_generation(root, config, state, stage_root, execution_id)
        artifacts = [Path(item).resolve() for item in result.get("artifacts", [])]
        if not artifacts or any(not path.is_file() for path in artifacts):
            raise WorkflowError("Navigation generation reported missing artifacts")
        for path in artifacts:
            kind = "json" if path.suffix.casefold() == ".json" else "file"
            state.add_artifact("navigation", path, kind, sha256_file(path), path.stat().st_size,
                               {"project_id": config["project_id"], "navigation_only": True})
        digest = json_digest([
            {"path": str(path.relative_to(root) if root in path.parents else path),
             "sha256": state.cached_hash(path)} for path in artifacts
        ])
        status = result["status"]
        details = {**result.get("details", {}), "artifacts": [str(path) for path in artifacts],
                   "navigation_only": True, "upstream_stages_executed": []}
        state.finish_stage(execution_id, status, digest, {**result, "artifacts": [str(path) for path in artifacts]})
        state.set_stage("navigation", status, fingerprint, digest, execution_id, details)
        summary["stages"]["navigation"] = status
        summary["status"] = status
        if status == "NEEDS_REVIEW":
            summary["needs_review"].append("navigation")
            summary["stop_reason"] = "The navigation proposal is ready for explicit human review."
        else:
            summary["completed"].append("navigation")
        state.end_run(run_id, summary["status"], summary)
        return summary
    except Exception as exc:
        state.finish_stage(execution_id, "FAILED", None, {"error": str(exc), "navigation_only": True}, str(exc))
        state.set_stage("navigation", "FAILED", fingerprint, None, execution_id,
                        {"error": str(exc), "navigation_only": True})
        summary["status"] = "FAILED"
        summary["stages"]["navigation"] = "FAILED"
        summary["failed"] = {"stage": "navigation", "error": str(exc)}
        state.end_run(run_id, "FAILED", summary)
        raise


def run_first_visual_preview_only(root: Path, config: dict, state: ProjectState) -> dict[str, Any]:
    """Rebuild the private Reviewer/Visitor from current local artifacts only."""
    training = state.stage("training") or {}
    training_fingerprint = stage_fingerprint(root, config, state, "training", full_hash=False)
    if (training.get("status") not in {"COMPLETED", "ADOPTED"} or
            training.get("input_fingerprint") != training_fingerprint or
            not _stage_artifacts_valid(state, "training", output_path(root, config) / "work")):
        raise WorkflowError("Viewer-only regeneration requires a current validated training stage; no upstream stage was run.")
    navigation_context = None
    if config.get("integrations", {}).get("navigation", {}).get("mode") == "generate_from_sfm":
        from .navigation_review import preview_navigation_context
        try:
            navigation_context = preview_navigation_context(root, config, state)
        except (OSError, ValueError, TypeError) as exc:
            raise WorkflowError(f"Viewer-only regeneration cannot use current navigation state: {exc}") from exc

    fingerprint = stage_fingerprint(root, config, state, "first_visual", full_hash=False)
    stage_root = output_path(root, config) / "work"
    stage_root.mkdir(parents=True, exist_ok=True)
    run_id = state.start_run("first_visual_preview_only")
    execution_id = state.start_stage(
        run_id, "first_visual", fingerprint, False,
        {"stage_contract": next(stage for stage in STAGES if stage["id"] == "first_visual"),
         "execution_context": {"kind": "operator-initiated-local-cli", "backend_execution": False,
                               "upstream_stages_executed": []}},
    )
    state.set_stage("first_visual", "RUNNING", fingerprint, None, execution_id,
                    {"preview_only": True, "upstream_stages_executed": []})
    try:
        from .preview import build_first_visual_preview
        result = build_first_visual_preview(root, config, state, stage_root, execution_id,
                                            navigation_context=navigation_context)
        artifacts = [Path(item).resolve() for item in result.get("artifacts", [])]
        if not artifacts or any(not path.is_file() for path in artifacts):
            raise WorkflowError("First-visual generation reported missing artifacts")
        for path in artifacts:
            kind = "json" if path.suffix.casefold() == ".json" else "file"
            state.add_artifact("first_visual", path, kind, sha256_file(path), path.stat().st_size,
                               {"project_id": config["project_id"], "preview_only": True})
        digest = json_digest([
            {"path": str(path.relative_to(root) if root in path.parents else path),
             "sha256": state.cached_hash(path)} for path in artifacts
        ])
        status = result["status"]
        details = {**result.get("details", {}), "artifacts": [str(path) for path in artifacts],
                   "preview_only": True, "upstream_stages_executed": []}
        state.finish_stage(execution_id, status, digest, {**result, "artifacts": [str(path) for path in artifacts]})
        state.set_stage("first_visual", status, fingerprint, digest, execution_id, details)
        summary = {"run_id": run_id, "mode": "first_visual_preview_only",
                   "status": "AWAITING_VISUAL_REVIEW", "stages": {"first_visual": status},
                   "completed": ["first_visual"], "needs_review": [], "blocked": [],
                   "skipped": [stage["id"] for stage in STAGES if stage["id"] != "first_visual"],
                   "stop_reason": "The private Reviewer and Visitor previews are ready for visual review."}
        state.end_run(run_id, summary["status"], summary)
        return {**summary, "first_visual": details}
    except Exception as exc:
        state.finish_stage(execution_id, "FAILED", None, {"error": str(exc), "preview_only": True}, str(exc))
        state.set_stage("first_visual", "FAILED", fingerprint, None, execution_id,
                        {"error": str(exc), "preview_only": True})
        state.end_run(run_id, "FAILED", {"status": "FAILED", "stage": "first_visual", "error": str(exc)})
        raise


def run_workflow(root: Path, config: dict, state: ProjectState, resume: bool = False, force_hash: bool = False,
                 fail_at: str | None = None, authorization_ids: list[str] | None = None,
                 package_only: bool = False, stop_after_stage: str | None = None,
                 progress_format: str = "text", verbose: bool = False,
                 progress_callback: Callable[[dict[str, Any]], None] | None = None) -> dict:
    if config.get("diagnostic_policy", {}).get("mode") == "SMOKE_TEST_ONLY":
        raise WorkflowError("This isolated diagnostic project cannot run the production workflow DAG; use the explicit diagnostic smoke-training command.")
    from .backends import reconcile_running_stage
    active = []
    for sid in ("sfm", "training"):
        status = reconcile_running_stage(state, sid)
        if status and status.get("status") == "RUNNING": active.append({"stage": sid, **status})
    if active:
        raise WorkflowError(f"Fresh backend process is still running; inspect status/logs or cancel it before another run: {active}")
    local_running = [stage["id"] for stage in STAGES
                     if stage["id"] not in {"sfm", "training"}
                     and (state.stage(stage["id"]) or {}).get("status") == "RUNNING"]
    if local_running:
        raise WorkflowError(
            f"Local stage execution is still recorded as RUNNING: {local_running}. "
            "Verify that its process has exited and reconcile the interrupted execution before resuming."
        )
    run_id = state.start_run("package" if package_only else ("resume" if resume else "run"))
    stage_root = output_path(root, config) / "work"
    stage_root.mkdir(parents=True, exist_ok=True)
    summary = {"run_id": run_id, "mode": "package_only" if package_only else ("resume" if resume else "run"),
               "status": "RUNNING", "stages": {}, "adopted": [], "completed": [], "needs_review": [],
               "blocked": [], "skipped": [], "failed": None}
    authorization_ids = authorization_ids or []
    scoped_sfm_training = None
    scoped_sfm_geometry = None
    sfm_stage_for_scope = state.stage("sfm") or {}
    if sfm_stage_for_scope.get("status") == "NEEDS_REVIEW":
        try:
            from .sfm_review import geometry_proposal_scope
            scoped_sfm_geometry = geometry_proposal_scope(root, config, state)
        except Exception:
            scoped_sfm_geometry = None
    if not package_only and _integration_mode(config, "training") == "spirula_fresh":
        from .sfm_review import training_acceptance_for_run
        current_training_scope = training_acceptance_for_run(root, config, state)
        if current_training_scope and current_training_scope["conditional_scope"]:
            scoped_sfm_training = current_training_scope
    try:
        for stage_index, stage in enumerate(STAGES, 1):
            sid = stage["id"]
            if progress_callback and sid in FIRST_VISUAL_STAGE_ORDER:
                current_row = state.stage(sid) or {}
                progress_callback({"event": "pipeline_stage_checked", "stage": sid,
                                   "stage_number": FIRST_VISUAL_STAGE_ORDER.index(sid) + 1,
                                   "stage_total": len(FIRST_VISUAL_STAGE_ORDER),
                                   "status": current_row.get("status", "PENDING")})
            if sid == "first_visual" and stop_after_stage != "first_visual":
                summary["skipped"].append(sid)
                summary["stages"][sid] = "SKIPPED_NOT_REQUESTED"
                continue
            if package_only and sid in {"lidar_inspection", "lidar_registration", "sfm_studio_export", "first_visual"}:
                summary["skipped"].append(sid)
                summary["stages"][sid] = "SKIPPED_PACKAGE_ONLY"
                continue
            if package_only and sid not in {"viewer_package", "archive_package", "verification"}:
                if sid == "sfm_dataset_prep" and not auto_preparation_required(root, config):
                    summary["skipped"].append(sid)
                    summary["stages"][sid] = "SKIPPED_NOT_REQUIRED_FOR_CURRENT_INPUT"
                    continue
                fp = stage_fingerprint(root, config, state, sid, full_hash=force_hash)
                prior = state.stage(sid)
                if prior and prior.get("input_fingerprint") == fp and prior.get("status") in {"COMPLETED", "ADOPTED"} and _stage_artifacts_valid(state, sid, stage_root):
                    summary["skipped"].append(sid); summary["stages"][sid] = "SKIPPED_VALID_CACHE"
                    continue
                message = f"Package-only mode did not run {sid}; its current validated cache is missing or stale. Run the required workflow stages explicitly, with fresh backend authorization where applicable."
                summary["status"] = "BLOCKED_PACKAGE_PREREQUISITE"
                summary["blocked"].append({"stage": sid, "reason": message})
                state.end_run(run_id, summary["status"], summary)
                return summary
            blocked = [dep for dep in stage["deps"] if state.stage(dep) is None or state.stage(dep)["status"] not in {"COMPLETED", "ADOPTED"}]
            if sid == "sfm_studio_export" and "sfm" in blocked and (state.stage("sfm") or {}).get("status") == "NEEDS_REVIEW":
                # A technically valid reconstruction may be exported for inspection
                # while its separate component-use decision remains pending.
                blocked.remove("sfm")
            if sid in {"lidar_registration", "navigation"} and "sfm" in blocked and scoped_sfm_geometry:
                blocked.remove("sfm")
            if sid == "training" and "sfm" in blocked and _integration_mode(config, "training") == "spirula_fresh":
                from .sfm_review import training_acceptance_for_run
                scoped_sfm_training = training_acceptance_for_run(root, config, state)
                if scoped_sfm_training and scoped_sfm_training["conditional_scope"]:
                    blocked.remove("sfm")
            if blocked:
                if sid == "lidar_registration" and "sfm" in blocked and scoped_sfm_training and not scoped_sfm_geometry:
                    summary["skipped"].append(sid)
                    summary["stages"][sid] = "SKIPPED_SFM_COMPONENT_REVIEW_SCOPE"
                    continue
                if sid == "navigation" and "sfm" in blocked and scoped_sfm_training and not scoped_sfm_geometry:
                    summary["skipped"].append(sid)
                    summary["stages"][sid] = "SKIPPED_OUTSIDE_SFM_ACCEPTANCE_SCOPE"
                    continue
                if sid == "training" and "sfm" in blocked and scoped_sfm_geometry:
                    message = "The SfM geometry decision permits registration and an unapproved navigation proposal only; record the separate experimental-training component acceptance before continuing."
                    summary["status"] = "NEEDS_REVIEW"
                    summary["blocked"].append({"stage": sid, "reason": message, "authorization_required": False})
                    summary["stop_reason"] = message
                    state.end_run(run_id, summary["status"], summary)
                    return summary
                if sid == "evaluation" and scoped_sfm_training:
                    message = "The SfM decision permits experimental training only; evaluation and later archive stages require their own current review scope."
                    summary["status"] = "NEEDS_REVIEW"
                    summary["blocked"].append({"stage": sid, "reason": message, "authorization_required": False})
                    summary["stop_reason"] = message
                    state.end_run(run_id, summary["status"], summary)
                    return summary
                if package_only:
                    message = f"Package-only mode is blocked by incomplete prerequisites: {blocked}"
                    summary["status"] = "BLOCKED_PACKAGE_PREREQUISITE"
                    summary["blocked"].append({"stage": sid, "reason": message})
                    state.end_run(run_id, summary["status"], summary)
                    return summary
                raise WorkflowError(f"Stage {sid} is blocked by incomplete dependencies: {blocked}")
            required_group = {"sfm":"sfm", "training":"model", "navigation":"navigation"}.get(sid)
            if required_group and not config.get("integrations", {}).get(required_group):
                message = f"No adopted {required_group} artifacts or executable backend are configured; fresh-run execution is an interface only."
                if sid == "navigation":
                    fp = stage_fingerprint(root, config, state, sid, full_hash=force_hash)
                    state.set_stage(sid, "BLOCKED", fp, None, None, {"reason": message, "publishing_ready": False})
                    summary["stages"][sid] = "BLOCKED"
                    summary["blocked"].append({"stage": sid, "reason": message})
                    continue
                state.set_stage(sid, "BLOCKED", None, None, None, {"reason": message, "backend_execution": "NOT_IMPLEMENTED"})
                summary["stages"][sid] = "BLOCKED"; summary["failed"] = {"stage": sid, "error": message}
                raise WorkflowError(f"Stage {sid} blocked: {message}")
            fresh_backend_stage = sid in {"sfm", "training"} and _integration_mode(config, sid) == "spirula_fresh"
            fp = stage_fingerprint(root, config, state, sid,
                                   full_hash=force_hash or fresh_backend_stage or sid == "sfm_dataset_prep")
            prior = state.stage(sid)
            if sid == "sfm" and prior and prior.get("status") == "NEEDS_REVIEW" and prior.get("input_fingerprint") == fp:
                from .sfm_review import training_acceptance_for_run
                scoped_sfm_training = training_acceptance_for_run(root, config, state)
                if ((scoped_sfm_training and scoped_sfm_training["conditional_scope"]) or
                        scoped_sfm_geometry):
                    summary["stages"][sid] = "RETAINED_NEEDS_REVIEW_WITH_TRAINING_SCOPE"
                    summary["needs_review"].append(sid)
                    continue
                export_stage = state.stage("sfm_studio_export") or {}
                export_fp = stage_fingerprint(root, config, state, "sfm_studio_export", full_hash=force_hash)
                if (export_stage.get("input_fingerprint") == export_fp and
                        export_stage.get("status") in {"COMPLETED", "ADOPTED"} and
                        _stage_artifacts_valid(state, "sfm_studio_export", stage_root)):
                    message = "The current SfM result remains NEEDS_REVIEW. Inspect the validation report and make an explicit component decision before downstream training."
                    summary["status"] = "NEEDS_REVIEW"
                    summary["stages"][sid] = "NEEDS_REVIEW"
                    summary["needs_review"].append(sid)
                    summary["blocked"].append({"stage": sid, "reason": message, "authorization_required": False})
                    summary["stop_reason"] = message
                    state.end_run(run_id, summary["status"], summary)
                    return summary
                # Reuse the accepted SfM bytes and allow only the independent,
                # local dataset export stage to run before returning to review.
                summary["stages"][sid] = "NEEDS_REVIEW"
                summary["needs_review"].append(sid)
                continue
            if (sid == "sfm_dataset_prep" and auto_preparation_required(root, config) and prior and
                    prior.get("status") in {"COMPLETED", "ADOPTED"}):
                if _stage_artifacts_valid(state, sid, stage_root):
                    summary["skipped"].append(sid)
                    summary["stages"][sid] = "SKIPPED_VALID_CACHE"
                    if progress_callback and sid in FIRST_VISUAL_STAGE_ORDER:
                        progress_callback({"event": "pipeline_stage_cached", "stage": sid,
                                           "stage_number": FIRST_VISUAL_STAGE_ORDER.index(sid) + 1,
                                           "stage_total": len(FIRST_VISUAL_STAGE_ORDER),
                                           "status": "SKIPPED_VALID_CACHE"})
                    continue
                raise BackendError(
                    "Previously completed HEIC preparation no longer passes source, derivative, or artifact-hash validation. "
                    "Refusing to replace it implicitly; inspect the stale evidence and explicitly reset the stage after correcting inputs.",
                    failure_kind="STALE_DATASET_PREPARATION",
                )
            if prior and prior.get("input_fingerprint") == fp and prior.get("status") in {"COMPLETED", "ADOPTED"} and _stage_artifacts_valid(state, sid, stage_root):
                summary["skipped"].append(sid); summary["stages"][sid] = "SKIPPED_VALID_CACHE"
                if progress_callback and sid in FIRST_VISUAL_STAGE_ORDER:
                    progress_callback({"event": "pipeline_stage_cached", "stage": sid,
                                       "stage_number": FIRST_VISUAL_STAGE_ORDER.index(sid) + 1,
                                       "stage_total": len(FIRST_VISUAL_STAGE_ORDER),
                                       "status": "SKIPPED_VALID_CACHE"})
                if sid == stop_after_stage:
                    summary["status"] = "AWAITING_VISUAL_REVIEW"
                    summary["stop_reason"] = "The requested first-visual Visitor output is ready for human review."
                    state.end_run(run_id, summary["status"], summary)
                    return summary
                if sid == "sfm_studio_export" and (state.stage("sfm") or {}).get("status") == "NEEDS_REVIEW":
                    message = "SfM passed technical output validation but needs a human component decision before training. The portable Studio dataset is ready for inspection."
                    summary["status"] = "NEEDS_REVIEW"
                    summary["stages"]["sfm"] = "NEEDS_REVIEW"
                    summary["needs_review"].append("sfm")
                    summary["blocked"].append({"stage": "sfm", "reason": message, "authorization_required": False})
                    summary["stop_reason"] = message
                    state.end_run(run_id, summary["status"], summary)
                    return summary
                if sid == "training" and scoped_sfm_training:
                    if stop_after_stage == "first_visual":
                        continue
                    message = "The accepted SfM scope permits experimental training only; downstream model evaluation, navigation, archive packaging, and publication remain outside this run."
                    summary["status"] = "NEEDS_REVIEW"
                    summary["stop_reason"] = message
                    summary["blocked"].append({"stage": "evaluation", "reason": message, "authorization_required": False})
                    state.end_run(run_id, summary["status"], summary)
                    return summary
                continue
            if sid == "navigation" and prior and prior.get("input_fingerprint") == fp and prior.get("status") == "NEEDS_REVIEW":
                summary["stages"][sid] = "NEEDS_REVIEW"
                summary["needs_review"].append(sid)
                if stop_after_stage == "first_visual":
                    continue
                summary["stop_reason"] = "The current navigation proposal is awaiting explicit review; use navigation review or change the inputs before generating a new proposal."
                state.end_run(run_id, "NEEDS_REVIEW", summary)
                return summary
            failed_attempts = state.db.execute("SELECT COUNT(*) FROM stage_executions WHERE stage_id=? AND status IN ('FAILED','CANCELLED','INTERRUPTED')", (sid,)).fetchone()[0]
            max_attempts = int(config.get("retry_policy", {}).get("max_failed_attempts", 2))
            if failed_attempts >= max_attempts:
                raise WorkflowError(f"Stage {sid} reached the configured bound of {max_attempts} failed attempts. Use reset-stage after correcting the cause to start a new bounded retry window.")
            authorization_plan = None
            authorization_id = None
            if fresh_backend_stage:
                try:
                    authorization_plan = expensive_execution_plan(root, config, state, sid)
                except Exception as exc:
                    reason = f"Fresh {sid} cannot start because its exact authorization plan is unavailable: {exc}"
                    state.set_stage(sid, "BLOCKED", fp, None, prior.get("last_execution_id") if prior else None,
                                    {"reason": reason, "authorization_required": False, "plan_available": False,
                                     "blocking_class": "BACKEND_PLAN_UNAVAILABLE"})
                    summary["status"] = "BLOCKED_BACKEND_PLAN_UNAVAILABLE"
                    summary["blocked"].append({"stage": sid, "reason": reason, "authorization_required": False,
                                                "plan_available": False, "blocking_class": "BACKEND_PLAN_UNAVAILABLE"})
                    state.end_run(run_id, summary["status"], summary)
                    return summary
                for existing in state.db.execute("SELECT authorization_id,plan_id,expires_at FROM execution_authorizations WHERE project_id=? AND stage_id=? AND status='ACTIVE'",
                                                 (config["project_id"], sid)).fetchall():
                    if existing["expires_at"] <= utc_now():
                        state.mark_authorization_stale(existing["authorization_id"], "Authorization expired before execution")
                    elif existing["plan_id"] != authorization_plan["plan_id"]:
                        state.mark_authorization_stale(existing["authorization_id"], "Current full-hash execution plan no longer matches the approved plan")
                matching_id = next((value for value in authorization_ids if (state.execution_authorization(value) or {}).get("project_id") == config["project_id"]
                                    and (state.execution_authorization(value) or {}).get("stage_id") == sid), None)
                authorization = state.execution_authorization(matching_id) if matching_id else None
                now = utc_now()
                if not authorization or authorization.get("status") != "ACTIVE" or authorization.get("plan_id") != authorization_plan["plan_id"]:
                    reason = f"Fresh {sid} is waiting for a one-time approval bound to the current plan. Review the plan and authorize it explicitly."
                    state.set_stage(sid, "BLOCKED", fp, None, prior.get("last_execution_id") if prior else None,
                                    {"reason": reason, "authorization_required": True, "plan_available": True,
                                     "authorization_plan_id": authorization_plan["plan_id"],
                                     "backend": authorization_plan["backend"], "input_dataset": authorization_plan["input_dataset"],
                                     "source_image_count": authorization_plan["source_image_count"],
                                     "resource_limits": authorization_plan["resource_limits"]})
                    summary["status"] = "AUTHORIZATION_REQUIRED"
                    summary["blocked"].append({"stage": sid, "reason": reason, "authorization_required": True,
                                                "plan_id": authorization_plan["plan_id"], "execution_plan": authorization_plan})
                    state.end_run(run_id, summary["status"], summary)
                    return summary
                authorization_id = matching_id
            adopted = sid in {"capture_preflight", "artwork_association"} or (sid == "navigation" and _navigation_mode(config) == "adopt_existing") or (sid == "evaluation" and _integration_mode(config, "training") == "adopt_existing") or (sid in {"sfm", "training"} and _integration_mode(config, sid) == "adopt_existing")
            execution_id = state.start_stage(run_id, sid, fp, adopted,
                                             {"stage_contract": stage, "authorization_id": authorization_id,
                                              "authorization_plan_id": authorization_plan.get("plan_id") if authorization_plan else None,
                                              "execution_context": authorization_plan.get("execution_context") if authorization_plan else None})
            if authorization_id and not state.consume_execution_authorization(authorization_id, authorization_plan["plan_id"], execution_id):
                message = "The one-time authorization expired, changed, or was already consumed before the stage began. Review and authorize the current plan again."
                state.finish_stage(execution_id, "BLOCKED_AUTHORIZATION", None, {"authorization_id": authorization_id}, message)
                state.set_stage(sid, "BLOCKED", fp, None, execution_id,
                                {"reason": message, "authorization_required": True, "authorization_plan_id": authorization_plan["plan_id"]})
                summary["status"] = "AUTHORIZATION_REQUIRED"
                summary["blocked"].append({"stage": sid, "reason": message, "authorization_required": True, "plan_id": authorization_plan["plan_id"]})
                state.end_run(run_id, summary["status"], summary)
                return summary
            state.set_stage(sid, "RUNNING", fp, None, execution_id,
                            {"resume": resume, "authorization_id": authorization_id,
                             "authorization_plan_id": authorization_plan.get("plan_id") if authorization_plan else None})
            if progress_callback and sid in FIRST_VISUAL_STAGE_ORDER:
                progress_callback({"event": "pipeline_stage_started", "stage": sid,
                                   "stage_number": FIRST_VISUAL_STAGE_ORDER.index(sid) + 1,
                                   "stage_total": len(FIRST_VISUAL_STAGE_ORDER), "status": "RUNNING"})
            try:
                result = _execute_stage(root, config, state, sid, stage_root, execution_id, fail_at, force_hash,
                                        authorization_plan.get("plan_id") if authorization_plan else None,
                                        progress_format, verbose)
                status = result["status"]
                artifact_paths = [Path(x).resolve() for x in result.get("artifacts", [])]
                if not artifact_paths or not all(p.is_file() for p in artifact_paths):
                    raise WorkflowError(f"Stage {sid} reported missing outputs")
                for path in artifact_paths:
                    artifact_type = result.get("artifact_types", {}).get(str(path), "json" if path.suffix == ".json" else "file")
                    state.add_artifact(sid, path, artifact_type, sha256_file(path), path.stat().st_size, {"adopted": adopted, "project_id": config["project_id"]})
                digest = json_digest([{"path": str(p.relative_to(root) if root in p.parents else p), "sha256": state.cached_hash(p)} for p in artifact_paths])
                state.finish_stage(execution_id, status, digest, {**result, "artifacts": [str(x) for x in artifact_paths]})
                state.set_stage(sid, status, fp, digest, execution_id, result.get("details", {}))
                if progress_callback and sid in FIRST_VISUAL_STAGE_ORDER:
                    progress_callback({"event": "pipeline_stage_completed", "stage": sid,
                                       "stage_number": FIRST_VISUAL_STAGE_ORDER.index(sid) + 1,
                                       "stage_total": len(FIRST_VISUAL_STAGE_ORDER), "status": status,
                                       "warning": result.get("details", {}).get("error") if status == "WARNING" else None})
                summary["stages"][sid] = status
                if status == "ADOPTED": summary["adopted"].append(sid)
                elif status == "NEEDS_REVIEW": summary["needs_review"].append(sid)
                else: summary["completed"].append(sid)
                if sid == stop_after_stage:
                    summary["status"] = "AWAITING_VISUAL_REVIEW"
                    summary["stop_reason"] = "The requested first-visual Reviewer and Visitor outputs are ready for human review."
                    state.end_run(run_id, summary["status"], summary)
                    return summary
                if status == "NEEDS_REVIEW":
                    if progress_callback and sid in FIRST_VISUAL_STAGE_ORDER:
                        progress_callback({"event": "pipeline_stage_needs_review", "stage": sid,
                                           "stage_number": FIRST_VISUAL_STAGE_ORDER.index(sid) + 1,
                                           "stage_total": len(FIRST_VISUAL_STAGE_ORDER),
                                           "status": status})
                    # Navigation is independent of Gaussian training. Preserve the
                    # review gate for publication while allowing configured model
                    # work and evaluation to finish from the already validated SfM.
                    if sid == "navigation" and config.get("integrations", {}).get("model"):
                        continue
                    if sid == "sfm":
                        # Allow the non-training portable export to complete, then
                        # stop for the existing component review gate.
                        continue
                    summary["stop_reason"] = f"Stopped before dependent work because {sid} requires human review."
                    state.end_run(run_id, "NEEDS_REVIEW", summary)
                    return summary
                if sid == "training" and scoped_sfm_training:
                    if stop_after_stage == "first_visual":
                        continue
                    message = "The accepted SfM scope permits experimental training only; downstream model evaluation, navigation, archive packaging, and publication remain outside this run."
                    summary["status"] = "NEEDS_REVIEW"
                    summary["stop_reason"] = message
                    summary["blocked"].append({"stage": "evaluation", "reason": message, "authorization_required": False})
                    state.end_run(run_id, summary["status"], summary)
                    return summary
            except KeyboardInterrupt:
                _mark_active_run_interrupted(state, run_id, summary)
                raise
            except AuthorizationRequired as exc:
                execution_row = state.execution(execution_id) or {}
                details = json.loads(execution_row.get("details_json") or "{}")
                details.update({"authorization_blocked": True, "reason": str(exc), "current_plan_id": (exc.plan or {}).get("plan_id")})
                state.finish_stage(execution_id, "BLOCKED_AUTHORIZATION", None, details, str(exc))
                state.set_stage(sid, "BLOCKED", fp, None, execution_id, details)
                summary["status"] = "AUTHORIZATION_REQUIRED"
                summary["blocked"].append({"stage": sid, "reason": str(exc), "authorization_required": True,
                                            "plan_id": (exc.plan or {}).get("plan_id"), "execution_plan": exc.plan})
                state.end_run(run_id, summary["status"], summary)
                return summary
            except Exception as exc:
                try:
                    setattr(exc, "stage_id", sid)
                except (AttributeError, TypeError):
                    pass
                execution_row = state.execution(execution_id) or {}
                execution_details = json.loads(execution_row.get("details_json") or "{}")
                failure_kind = getattr(exc, "failure_kind", "EXECUTION_FAILED")
                execution_details.update({"stage_contract": stage, "failure_kind": failure_kind, "failure_details": getattr(exc, "details", {})})
                finalize_stage_attempt(output_path(root, config), sid, execution_id,
                    failure_kind=failure_kind, error=str(exc), details=getattr(exc, "details", {}))
                execution_status = "CANCELLED" if failure_kind == "CANCELLED" else "FAILED"
                state.finish_stage(execution_id, execution_status, None, execution_details, str(exc))
                state.set_stage(sid, "FAILED", fp, None, execution_id, {"error": str(exc), "failure_kind": failure_kind, "failure_details": getattr(exc, "details", {}), "retry_policy": "bounded stage-local retry via explicit resume; successful upstream stages remain cached"})
                summary["stages"][sid] = "FAILED"; summary["failed"] = {"stage": sid, "error": str(exc)}
                raise
        summary["status"] = "COMPLETED"
        state.end_run(run_id, "COMPLETED", summary)
        return summary
    except KeyboardInterrupt:
        _mark_active_run_interrupted(state, run_id, summary)
        raise
    except Exception:
        state.end_run(run_id, "FAILED", summary)
        raise


def build_archive(root: Path, config: dict, state: ProjectState, output_dir: Path) -> dict:
    archive = output_path(root, config) / "archive"
    temp = archive.with_name("archive.staging")
    if temp.exists():
        shutil.rmtree(temp)
    temp.mkdir(parents=True)
    work = output_path(root, config) / "work"
    model_selection_source = work / "model-selection.json"
    if not model_selection_source.is_file():
        model_selection_source = work / "adopted-model.json"
    copy_map = {
        "source-index.json": work / "source-index.json",
        "capture-quality.json": work / "capture-quality-summary.json",
        "capture-risk-register.json": work / "capture-risk-register.json",
        "recapture-recommendations.json": work / "recapture-recommendations.json",
        "artwork-association-summary.json": work / "artwork-association-summary.json",
        "artwork-inventory-private.json": work / "phase2ev-state.private.json",
        "association-graph-private.json": work / "association-graph.private.json",
        "confirmed-artworks.json": work / "confirmed-artworks.json",
        "sfm-summary.json": work / "sfm-summary.json",
        "model-selection.json": model_selection_source,
        "fidelity-summary.json": work / "model-evaluation.json",
        "navigation-summary.json": work / "navigation-summary.json",
        "known-limitations.json": Path(config.get("archive_policy", {}).get("known_limitations_path", "")),
    }
    for target_name, source in copy_map.items():
        if not source or not Path(source).is_file():
            if target_name in {"known-limitations.json"}:
                continue
            raise WorkflowError(f"Archive input missing: {source}")
        shutil.copy2(source, temp / target_name)
    prep_manifest = preparation_manifest_path(root)
    if prep_manifest.is_file():
        shutil.copy2(prep_manifest, temp / "sfm-dataset-preparation.json")
    if _navigation_mode(config) == "generate_from_sfm":
        navigation_details = json.loads((state.stage("navigation") or {}).get("details_json") or "{}")
        for target_name, key in (("navigation/proposal.json", "proposal_path"), ("navigation/approved.json", "approved_artifact")):
            source = Path(navigation_details.get(key, ""))
            if not source.is_file():
                raise WorkflowError(f"Archive navigation evidence missing: {source}")
            target = temp / target_name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    known = config.get("archive_policy", {}).get("known_limitations", [])
    write_json(temp / "known-limitations.json", {"classification": "ADOPTED_AND_DOCUMENTED", "items": known, "note": "Descriptive evidence, not causal attribution."})
    provenance = {
        "schema_version": "exhibition-archive.provenance.v1", "project_id": config["project_id"],
        "source": {"photo_directory": config["source"]["photo_directory"], "originals_modified": False},
        "sfm": config.get("integrations", {}).get("sfm", {}),
        "image_dataset_preparation": ({"manifest": "sfm-dataset-preparation.json",
                                        "sha256": sha256_file(temp / "sfm-dataset-preparation.json")}
                                       if (temp / "sfm-dataset-preparation.json").is_file() else None),
        "model": read_json(temp / "model-selection.json"),
        "navigation": {"envelope_id": read_json(temp / "navigation-summary.json").get("envelope_id"), "source_files": read_json(temp / "navigation-summary.json").get("source_files")},
        "viewer_runtime": {"name": "PlayCanvas GSplat Viewer", "version": "2.23.0", "license_file_in_package": "../website/vendor/LICENSE.txt"},
        "generated_by": "exhibition-archive 0.1.0",
    }
    write_json(temp / "provenance.json", provenance)
    manifest = {
        "schema_version": "exhibition-archive.manifest.v1", "project_id": config["project_id"],
        "title": config.get("viewer", {}).get("title", config["project_id"]), "created_at_utc": config.get("created_at_utc"),
        "package_policy": config.get("archive_policy", {}), "publication_policy": config.get("publication", {}),
        "contents": [], "source_assets": {"included": False, "reference": config["source"]["photo_directory"], "source_index": "source-index.json", "checksummed": True},
        "completeness": "METADATA_PLUS_DEPLOYABLE_VIEWER; source photographs and full SfM intermediates remain at managed source locations",
    }
    for path in sorted(temp.rglob("*")):
        if path.is_file():
            manifest["contents"].append({"path": path.relative_to(temp).as_posix(), "size_bytes": path.stat().st_size, "sha256": sha256_file(path)})
    write_json(temp / "manifest.json", manifest)
    if archive.exists():
        shutil.rmtree(archive)
    temp.replace(archive)
    return {"status": "COMPLETED", "artifacts": [archive / "manifest.json", archive / "provenance.json"], "details": {"file_count": len(manifest["contents"]) + 1, "source_photographs_bundled": False, "metadata_only_source_index": True}}


def verify_website_package_index(website: Path, index: dict) -> tuple[list[dict], list[str]]:
    """Verify a complete, path-safe v2 static website file index."""
    checks: list[dict] = []
    failures: list[str] = []
    if not isinstance(index, dict):
        return checks, ["Website package index must be a JSON object"]
    if index.get("schema_version") != "exhibition-archive.website-index.v2":
        failures.append("Website package index schema is missing or unsupported")
    entries = index.get("files")
    if not isinstance(entries, list):
        return checks, failures + ["Website package index files must be a list"]

    listed: set[str] = set()
    total_bytes = 0
    root = website.resolve()
    for entry in entries:
        if not isinstance(entry, dict):
            failures.append("Website package index contains a non-object file entry")
            continue
        raw_path = entry.get("path")
        safe = isinstance(raw_path, str) and bool(raw_path) and "\\" not in raw_path
        relative = PurePosixPath(raw_path) if safe else PurePosixPath("invalid")
        safe = safe and not relative.is_absolute() and all(part not in {"", ".", ".."} for part in relative.parts)
        if raw_path == "config/package-index.json":
            safe = False
        candidate = website.joinpath(*relative.parts) if safe else root / "__invalid_manifest_entry__"
        target = candidate.resolve()
        try:
            target.relative_to(root)
        except ValueError:
            safe = False
        if isinstance(raw_path, str) and raw_path in listed:
            safe = False
            failures.append(f"Website package index contains a duplicate path: {raw_path}")
        if safe and isinstance(raw_path, str):
            listed.add(raw_path)
        exists = safe and not candidate.is_symlink() and target.is_file()
        actual_size = target.stat().st_size if exists else None
        size_ok = isinstance(entry.get("size_bytes"), int) and not isinstance(entry.get("size_bytes"), bool) and entry["size_bytes"] >= 0 and actual_size == entry.get("size_bytes")
        digest_ok = exists and isinstance(entry.get("sha256"), str) and bool(re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])) and sha256_file(target) == entry.get("sha256")
        ok = bool(exists and size_ok and digest_ok)
        checks.append({"website_path": raw_path, "ok": ok})
        if not ok:
            failures.append(f"Website package checksum/path validation failed: {raw_path}")
        if isinstance(entry.get("size_bytes"), int) and not isinstance(entry.get("size_bytes"), bool) and entry["size_bytes"] >= 0:
            total_bytes += entry["size_bytes"]

    package_index_path = website / "config" / "package-index.json"
    actual_paths = {p.relative_to(website).as_posix() for p in website.rglob("*") if p.is_file() and p != package_index_path}
    if listed != actual_paths:
        missing = sorted(actual_paths - listed)
        extra = sorted(listed - actual_paths)
        if missing:
            failures.append(f"Website package index omits payload files: {', '.join(missing[:8])}")
        if extra:
            failures.append(f"Website package index lists files that are not in the package: {', '.join(extra[:8])}")
    if not isinstance(index.get("file_count"), int) or isinstance(index.get("file_count"), bool) or index["file_count"] != len(entries):
        failures.append("Website package index file_count does not match its file list")
    if not isinstance(index.get("total_bytes"), int) or isinstance(index.get("total_bytes"), bool) or index["total_bytes"] != total_bytes:
        failures.append("Website package index total_bytes does not match its file list")
    return checks, failures


def _website_reference_failures(website: Path) -> list[str]:
    """Resolve static HTML, CSS, and JavaScript references within the package."""
    failures: list[str] = []
    root = website.resolve()
    attribute = re.compile(r"(?:href|src)\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
    script_ref = re.compile(r"(?:from\s+|import(?:\s+|\s*\()|fetch\s*\()\s*['\"]([^'\"]+)['\"]")
    css_ref = re.compile(r"url\(\s*['\"]?([^'\")]+)", re.IGNORECASE)
    for source in website.rglob("*"):
        if not source.is_file() or source.suffix.casefold() not in {".html", ".js", ".css"}:
            continue
        content = source.read_text(encoding="utf-8", errors="replace")
        if source.suffix.casefold() == ".js":
            content = re.sub(r"/\*.*?\*/", "", content, flags=re.DOTALL)
            content = re.sub(r"(?m)^\s*//.*$", "", content)
        refs: list[str] = []
        if source.suffix.casefold() == ".html":
            refs.extend(attribute.findall(content))
        if source.suffix.casefold() == ".js":
            refs.extend(script_ref.findall(content))
        if source.suffix.casefold() == ".css":
            refs.extend(css_ref.findall(content))
        for raw in refs:
            value = raw.strip()
            if not value or value.startswith(("#", "data:", "mailto:", "tel:", "javascript:")):
                continue
            if value.startswith(("http://", "https://", "//")):
                failures.append(f"Public asset has an external dependency: {source.relative_to(website)} -> {value}")
                continue
            path_part = value.split("?", 1)[0].split("#", 1)[0]
            if path_part.startswith("/"):
                failures.append(f"Public asset uses a root-absolute reference: {source.relative_to(website)} -> {value}")
                continue
            target = (source.parent / path_part).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                failures.append(f"Public asset reference escapes package: {source.relative_to(website)} -> {value}")
                continue
            if target.is_dir():
                target = target / "index.html"
            if not target.is_file():
                failures.append(f"Public asset reference is missing: {source.relative_to(website)} -> {value}")
    return failures


def verify_outputs(root: Path, config: dict, output_dir: Path, state: ProjectState | None = None, force_hash: bool = False) -> dict:
    output_root = output_path(root, config)
    website, archive = output_root / "website", output_root / "archive"
    visitor_website = output_root / "website-visitor"
    manifest = read_json(archive / "manifest.json")
    failures = []
    checks = []
    for entry in manifest["contents"]:
        path = archive / entry["path"]
        ok = path.is_file() and path.stat().st_size == entry["size_bytes"] and sha256_file(path) == entry["sha256"]
        checks.append({"path": entry["path"], "ok": ok})
        if not ok: failures.append(f"Archive checksum failed: {entry['path']}")
    from .model_registry import selected_model_record
    selected_model = selected_model_record(root, config, state) if state else None
    if selected_model:
        model = Path(selected_model["path"])
        expected_hash = selected_model["sha256"]
    else:
        model = project_path(root, config, config["integrations"]["model"]["path"])
        expected_hash = config["integrations"]["model"]["expected_sha256"]
    packaged_model = website / "assets" / "exhibition.ply"
    source_hash = state.cached_hash(model, force_hash) if state else sha256_file(model)
    model_ok = source_hash == expected_hash and packaged_model.is_file() and packaged_model.stat().st_size == model.stat().st_size and sha256_file(packaged_model) == expected_hash
    if not model_ok: failures.append("Packaged model identity does not match selected baseline")
    source_index = read_json(archive / "source-index.json")
    source_root = project_path(root, config, config["source"]["photo_directory"])
    source_errors = []
    for image in source_index.get("images", []):
        path = (source_root / image["relative_path"]).resolve()
        if source_root not in path.parents or not path.is_file():
            source_errors.append({"image_id": image.get("image_id"), "issue": "MISSING_OR_ESCAPED"})
            continue
        digest = state.cached_hash(path, force_hash) if state else sha256_file(path)
        if digest != image.get("sha256"):
            source_errors.append({"image_id": image.get("image_id"), "issue": "SHA256_MISMATCH"})
    if source_errors: failures.append(f"Source photo integrity gate failed for {len(source_errors)} files")
    public_files = [p for site in (website, visitor_website) for p in site.rglob("*") if p.is_file()]
    public_text = []
    for p in public_files:
        if p.suffix.casefold() in {".json", ".html", ".js", ".css", ".md", ".txt"}:
            public_text.append((p, p.read_text(encoding="utf-8", errors="replace")))
    forbidden = ["/Users/", "/private/", "localhost", "127.0.0.1", str(project_path(root, config, config["source"]["photo_directory"]))]
    for path, content in public_text:
        for marker in forbidden:
            if marker and marker in content:
                failures.append(f"Public package contains local/private path marker {marker!r}: {path.relative_to(website)}")
        if re.search(r"IMG_\d{3,}", content):
            failures.append(f"Public package contains an internal source-image identifier: {path.relative_to(website)}")
        if re.search(r"attention-\d{3}", content):
            failures.append(f"Public package contains an internal geometry candidate ID: {path.relative_to(website)}")
    package_index_path = website / "config" / "package-index.json"
    if not package_index_path.is_file():
        failures.append("Website package index is missing")
    else:
        package_index = read_json(package_index_path)
        package_checks, package_failures = verify_website_package_index(website, package_index)
        checks.extend(package_checks)
        failures.extend(package_failures)
        if package_index.get("project_id") != config.get("project_id"):
            failures.append("Website package index project_id does not match the configured project")
        if package_index.get("model_sha256") != expected_hash:
            failures.append("Website package index model_sha256 does not match the selected baseline")
    visitor_index_path = visitor_website / "config" / "package-index.json"
    visitor_index = None
    if not visitor_index_path.is_file():
        failures.append("Visitor website package index is missing")
    else:
        visitor_index = read_json(visitor_index_path)
        visitor_checks, visitor_failures = verify_website_package_index(visitor_website, visitor_index)
        checks.extend(visitor_checks)
        failures.extend(visitor_failures)
        if visitor_index.get("profile") != "visitor":
            failures.append("Visitor package index does not identify the visitor publication profile")
        if visitor_index.get("project_id") != config.get("project_id") or visitor_index.get("model_sha256") != expected_hash:
            failures.append("Visitor package index project or master PLY identity does not match the selected baseline")
    app_path, html_path = website / "viewer" / "app.js", website / "viewer" / "index.html"
    if app_path.is_file() and html_path.is_file():
        app_text = app_path.read_text(encoding="utf-8", errors="replace")
        html_text = html_path.read_text(encoding="utf-8", errors="replace")
        absolute_asset = re.compile(r"(?:href|src)\s*=\s*['\"]\/|from\s+['\"]\/|fetch\(['\"]\/(?:api|assets)\/|['\"]\/assets/")
        if absolute_asset.search(app_text) or absolute_asset.search(html_text):
            failures.append("Static viewer contains a root-absolute asset/API path and may fail under a nested deployment path")
    failures.extend(_website_reference_failures(website))
    failures.extend(_website_reference_failures(visitor_website))
    health_path = website / "api" / "health.json"
    if health_path.is_file():
        health = read_json(health_path)
        content_url = health.get("contentUrl")
        content_target = (website / "viewer" / content_url).resolve() if isinstance(content_url, str) and content_url and "\\" not in content_url else None
        try:
            if content_target is None:
                raise ValueError("missing or unsafe relative model URL")
            content_target.relative_to(website.resolve())
            content_ok = not content_url.startswith("/") and "://" not in content_url and content_target.is_file()
        except ValueError:
            content_ok = False
        if not content_ok:
            failures.append("Website health metadata does not resolve its model through a portable relative URL")
    navigation_doc = website / "config" / "navigation.json"
    if navigation_doc.is_file():
        navigation = read_json(navigation_doc)
        if config.get("integrations", {}).get("navigation", {}).get("mode") == "generate_from_sfm":
            if navigation.get("status") != "NAVIGATION_APPROVED_BY_REVIEW" or not navigation.get("viewer_compatibility", {}).get("envelope") or not navigation.get("viewer_compatibility", {}).get("runtime_policy") or not navigation.get("validation", {}).get("public_ready"):
                failures.append("Website navigation package is missing an explicitly approved, validated generic navigation artifact")
        elif not navigation.get("envelope") or not navigation.get("runtime_policy"):
            failures.append("Website navigation package is missing its accepted envelope or runtime policy")
    if (website / "viewer" / "index.html").is_file() and 'value="debug"' in (website / "viewer" / "index.html").read_text(encoding="utf-8", errors="replace"):
        failures.append("Public viewer exposes the unrestricted Debug Fly control")
    if any(p.is_relative_to(website / "source-images") for p in public_files): failures.append("Public website contains source-image directory")
    expected_website = [website / "index.html", website / "viewer" / "app.js", website / "viewer" / "vendor" / "index.js", website / "config" / "navigation.json", packaged_model, package_index_path]
    for p in expected_website:
        if not p.is_file(): failures.append(f"Required website file missing: {p.relative_to(website)}")
    visitor_required = [
        visitor_website / "index.html", visitor_website / "js" / "viewer.js", visitor_website / "css" / "viewer.css",
        visitor_website / "vendor" / "supersplat-viewer.js", visitor_website / "vendor" / "supersplat-viewer.css",
        visitor_website / "config" / "settings.json", visitor_website / "config" / "exhibition.json",
        visitor_website / "config" / "locales.json",
        visitor_website / "assets" / "enclosure-collision.glb", visitor_index_path,
    ]
    for path in visitor_required:
        if not path.is_file(): failures.append(f"Required visitor website file missing: {path.relative_to(visitor_website)}")
    visitor_metadata_path = visitor_website / "config" / "exhibition.json"
    visitor_metadata = read_json(visitor_metadata_path) if visitor_metadata_path.is_file() else {}
    master_record = visitor_metadata.get("master", {})
    if master_record.get("sha256") != expected_hash or master_record.get("size_bytes") != model.stat().st_size:
        failures.append("Visitor master metadata does not match the selected accepted PLY identity")
    conversion_record = visitor_metadata.get("conversion", {})
    if conversion_record.get("input_sha256") != expected_hash:
        failures.append("Visitor conversion input hash does not match the selected accepted PLY master")
    collision_path = visitor_website / "assets" / "enclosure-collision.glb"
    collision_record = visitor_metadata.get("collision", {})
    if collision_record.get("source_model_sha256") != expected_hash:
        failures.append("Visitor enclosure collision provenance does not match the selected accepted PLY")
    if collision_path.is_file() and (sha256_file(collision_path) != collision_record.get("sha256") or collision_path.stat().st_size != collision_record.get("size_bytes")):
        failures.append("Visitor enclosure collision mesh does not match its recorded integrity metadata")
    if visitor_metadata.get("profile") != "visitor" or visitor_metadata.get("release_authorization") != "NEEDS_REVIEW":
        failures.append("Visitor package profile or independent release authorization state is invalid")
    if visitor_metadata.get("primary_format") == "sog":
        sog_path = visitor_website / "assets" / "exhibition.sog"
        sog_version_ok = bool(re.search(r"\bv3\.10\.3\b", str(conversion_record.get("tool_version", ""))))
        if (not sog_path.is_file() or sha256_file(sog_path) != conversion_record.get("output_sha256")
                or sog_path.stat().st_size != conversion_record.get("output_size_bytes")
                or conversion_record.get("status") not in {"COMPLETED", "VALIDATED_EXTERNAL"} or not sog_version_ok):
            failures.append("Visitor SOG primary asset is missing, invalid, or does not match its conversion integrity metadata")
        try:
            from .publisher import _sog_archive_metadata
            sog_meta = _sog_archive_metadata(sog_path, int(master_record.get("gaussian_count") or 0))
            if (sog_meta.get("format") != "sog"
                    or conversion_record.get("input_gaussian_count") != master_record.get("gaussian_count")
                    or not isinstance(conversion_record.get("input_sh_bands"), int)
                    or conversion_record.get("input_sh_bands", -1) < 0):
                failures.append("Visitor SOG format metadata is incomplete")
        except (OSError, ValueError) as error:
            failures.append(f"Visitor SOG archive failed structural validation: {error}")
        if (visitor_website / "assets" / "exhibition.ply").exists():
            failures.append("SOG-primary Visitor package unexpectedly duplicates the full PLY master")
        models = visitor_metadata.get("models", {})
        if not models.get("sog", {}).get("available") or models.get("ply", {}).get("available"):
            failures.append("SOG-primary Visitor model availability flags are inconsistent")
    elif visitor_metadata.get("primary_format") != "ply":
        failures.append("Visitor package has an unsupported primary model format")
    else:
        visitor_ply = visitor_website / "assets" / "exhibition.ply"
        if not visitor_ply.is_file() or sha256_file(visitor_ply) != expected_hash or visitor_ply.stat().st_size != model.stat().st_size:
            failures.append("Visitor PLY publication does not match the accepted master model identity")
        if (visitor_website / "assets" / "exhibition.sog").exists():
            failures.append("PLY-primary Visitor package unexpectedly includes an unselected SOG model")
        models = visitor_metadata.get("models", {})
        if not models.get("ply", {}).get("available") or models.get("sog", {}).get("available"):
            failures.append("PLY-primary Visitor model availability flags are inconsistent")
    if collision_record.get("contains_interior_objects") is not False:
        failures.append("Visitor collision data must contain only the accepted enclosure, floor, and ceiling")
    visitor_html = visitor_website / "index.html"
    if visitor_html.is_file():
        html_text = visitor_html.read_text(encoding="utf-8", errors="replace").casefold()
        for forbidden_control in ("guided tour", "route selector", "diagnostic", "geometry overlay", "technical status"):
            if forbidden_control in html_text:
                failures.append(f"Visitor presentation contains a removed technical control: {forbidden_control}")
        if 'id="resetview"' not in html_text or 'id="helptoggle"' not in html_text:
            failures.append("Visitor presentation is missing Reset View or navigation help")
    return {"schema_version": "exhibition-archive.verification.v1", "status": "PASS" if not failures else "FAIL", "checks": checks, "model_identity_match": model_ok, "visitor_primary_format": visitor_metadata.get("primary_format"), "visitor_sog_status": visitor_metadata.get("conversion", {}).get("status"), "visitor_enclosure_collision": collision_record.get("contains_interior_objects") is False, "source_photo_count": source_index.get("image_count"), "source_photo_integrity_failure_count": len(source_errors), "source_photo_integrity_failures": source_errors, "full_hash_requested": force_hash, "public_text_file_count": len(public_text), "failure_count": len(failures), "failures": failures, "static_http_browser_validation": "see separate browser-validation evidence", "verified_at_utc": utc_now()}
