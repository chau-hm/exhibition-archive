from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import signal
import struct
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .adapters import read_json, sha256_file, write_json


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".heic", ".heif", ".bmp", ".avif"}
RAW_IMAGE_SUFFIXES = {".dng", ".cr2", ".cr3", ".nef", ".arw", ".raf", ".rw2"}
COLMAP_CAMERA_PARAMETER_COUNTS = {0: 3, 1: 4, 2: 4, 3: 5, 4: 8, 5: 8, 6: 12, 7: 5, 8: 4, 9: 5, 10: 12, 11: 16}
PLY_TYPES = {
    "char": ("b", 1), "int8": ("b", 1), "uchar": ("B", 1), "uint8": ("B", 1),
    "short": ("h", 2), "int16": ("h", 2), "ushort": ("H", 2), "uint16": ("H", 2),
    "int": ("i", 4), "int32": ("i", 4), "uint": ("I", 4), "uint32": ("I", 4),
    "float": ("f", 4), "float32": ("f", 4), "double": ("d", 8), "float64": ("d", 8),
}


class BackendError(RuntimeError):
    def __init__(self, message: str, *, failure_kind: str = "EXECUTION_FAILED", details: dict | None = None):
        super().__init__(message)
        self.failure_kind = failure_kind
        self.details = details or {}


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def discover_executable(configured: str | None = None) -> Path:
    """Resolve an explicitly configured CLI, PATH entry, or standard macOS app bundle."""
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.parent != Path(".") or candidate.is_absolute():
            resolved = candidate.resolve()
        else:
            located = shutil.which(configured)
            if not located:
                raise BackendError(f"Configured Spirula executable was not found: {configured}", failure_kind="BACKEND_UNAVAILABLE")
            resolved = Path(located).resolve()
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise BackendError(f"Configured Spirula executable is not an executable file: {resolved}", failure_kind="BACKEND_UNAVAILABLE")
        return resolved
    located = shutil.which("spirula")
    candidates = [Path(located).resolve()] if located else []
    candidates += [Path("/Applications/Spirula Studio.app/Contents/MacOS/spirula"),
                   Path.home() / "Applications/Spirula Studio.app/Contents/MacOS/spirula"]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    raise BackendError("Spirula CLI was not found on PATH or in a standard macOS application location", failure_kind="BACKEND_UNAVAILABLE")


def _capture(argv: list[str], timeout: int = 60) -> dict[str, Any]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackendError(f"Backend discovery command failed: {argv!r}: {exc}", failure_kind="BACKEND_UNAVAILABLE") from exc
    return {"argv": argv, "exit_code": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}


def _flags(help_text: str) -> set[str]:
    return set(re.findall(r"^\s+(--[a-zA-Z0-9][a-zA-Z0-9-]*)\b", help_text, re.MULTILINE))


def discover_environment(configured: str | None = None, evidence_dir: Path | None = None) -> dict[str, Any]:
    executable = discover_executable(configured)
    captures = {
        "version": _capture([str(executable), "--version"]),
        "top_help": _capture([str(executable), "--help"]),
        "sfm_help": _capture([str(executable), "sfm", "--help"]),
        "sfm_auto_help": _capture([str(executable), "sfm", "auto", "--help"]),
        "train_help": _capture([str(executable), "train", "--help"]),
        "train_help_all": _capture([str(executable), "train", "--help-all"]),
    }
    for name, capture in captures.items():
        if capture["exit_code"] != 0:
            raise BackendError(f"Spirula discovery failed for {name}: {capture['stderr'] or capture['stdout']}", failure_kind="BACKEND_UNAVAILABLE")
    version = (captures["version"]["stdout"] or captures["version"]["stderr"]).strip()
    sfm_text = captures["sfm_auto_help"]["stdout"]
    train_text = captures["train_help"]["stdout"]
    train_all_text = captures["train_help_all"]["stdout"]
    checkpoint_flags = _flags(train_all_text)
    top_lines = [line.strip() for line in captures["top_help"]["stdout"].splitlines() if re.match(r"^[a-z][a-z0-9-]+\s{2,}", line.strip())]
    commands = [re.match(r"^([a-z][a-z0-9-]+)", line).group(1) for line in top_lines if re.match(r"^([a-z][a-z0-9-]+)", line)]
    result = {
        "schema_version": "exhibition-archive.spirula-capabilities.v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "tool": {"name": "Spirula Studio CLI", "executable": str(executable), "version": version, "identity_status": "VERIFIED_BY_EXECUTION"},
        "platform": {"system": os.uname().sysname, "machine": os.uname().machine, "training_device_probe": "NOT_RUN; no training process launched during discovery"},
        "commands": {"top_level": commands, "status": "VERIFIED_BY_HELP", "evidence": "spirula-help.txt"},
        "sfm": {
            "operations": ["auto", "extract", "match", "map", "merge", "ba"],
            "status": "VERIFIED_BY_HELP",
            "auto_arguments": sorted(_flags(sfm_text)),
            "accepted_input_layout": "IMAGE_DIR or DATASET_DIR containing images/; masks/ beside images are auto-detected",
            "formats": "UNKNOWN_BY_HELP; all source images are retained and handed to Spirula; execution must report unsupported files",
            "output_layout": ["features/", "matches.bin", "sparse/0", "sparse/1.. for disconnected components"],
            "intermediate_reuse": {"status": "VERIFIED_BY_HELP", "details": ["--reuse-features auto|keep|redo", "--reuse-matches auto|keep|redo", "--no-resume disables resuming an interrupted same-settings SFM run"]},
            "camera_manifest": "--manifest supports YAML or JSON capture descriptions",
            "evidence": ["sfm-help.txt", "sfm-auto-help.txt"],
        },
        "training": {
            "presets": ["3dgs", "360-camera", "in-the-wild", "centered-object", "hdr", "synthetic", "meshing"],
            "status": "VERIFIED_BY_HELP",
            "arguments": sorted(_flags(train_text) | _flags(train_all_text)),
            "dataset_formats": ["colmap", "nerfstudio", "metashape"],
            "output_arguments": ["--output-dir-prefix", "--output-dir-name"],
            "iterations_argument": "--num-iterations",
            "resolution_arguments": ["--train-resolution-divisor", "--progressive-resolution"],
            "device_argument": "--device index|name|auto|uuid:hex; valid device list is printed at training startup",
            "checkpoint": {
                "save_intermediate": "VERIFIED_BY_HELP" if "--steps-per-save" in checkpoint_flags else "UNKNOWN",
                "full_state": "VERIFIED_BY_HELP" if "--save-full-checkpoint" in checkpoint_flags else "UNKNOWN",
                "resume_cli_flag": "VERIFIED_BY_HELP" if "--resume" in checkpoint_flags else "UNKNOWN",
                "resume_execution": "NOT_TESTED",
                "spirula_optimizer_resume_semantics": "UNKNOWN_NOT_TESTED",
                "engine_optimizer_resume": "CHECKPOINT_RESUME_UNSUPPORTED",
                "init_ply": "VERIFIED_BY_HELP" if "--init-ply" in checkpoint_flags else "UNKNOWN",
                "init_ply_is_optimizer_resume": False,
                "evidence": "train-help-all.txt",
            },
            "device_selection": {"argument": "--device", "selector_syntax": "VERIFIED_BY_HELP; index, name substring, auto, uuid", "usable_device": "UNKNOWN_UNTIL_BACKEND_EXECUTION"},
            "model_output_convention": "UNKNOWN_BY_HELP; locate PLY artifacts recursively and reject ambiguous candidates unless configured selector matches one",
            "progress_telemetry": "NO_STRUCTURED_PROGRESS_API_VERIFIED; process status and recent stdout/stderr are available",
            "evidence": ["train-help.txt", "train-help-all.txt"],
        },
        "claims": [
            {"claim": "Executable identity and version", "status": "VERIFIED_BY_EXECUTION", "evidence": "spirula-version.txt"},
            {"claim": "SfM commands, output layout, flags and intermediate reuse", "status": "VERIFIED_BY_HELP", "evidence": "sfm-help.txt; sfm-auto-help.txt"},
            {"claim": "Training arguments, presets, checkpoint save and resume options", "status": "VERIFIED_BY_HELP", "evidence": "train-help.txt; train-help-all.txt"},
            {"claim": "Training device is usable by Spirula on this host", "status": "UNKNOWN", "evidence": "No training process was launched during capability discovery"},
            {"claim": "Actual checkpoint resume restores optimizer state", "status": "UNKNOWN", "evidence": "CLI help describes continuing a previous run but behavior has not been executed"},
        ],
        "unsupported_or_unknown": ["Accepted image format list is not enumerated by help", "Exact PLY naming and checkpoint directory conventions are not enumerated by help", "Training device availability is not yet verified by Spirula execution", "Checkpoint resume semantics are not yet verified by execution"],
        "evidence_commands": {name: capture["argv"] for name, capture in captures.items()},
    }
    if evidence_dir:
        evidence_dir.mkdir(parents=True, exist_ok=True)
        filenames = {"version": "spirula-version.txt", "top_help": "spirula-help.txt", "sfm_help": "sfm-help.txt", "sfm_auto_help": "sfm-auto-help.txt", "train_help": "train-help.txt", "train_help_all": "train-help-all.txt"}
        for name, capture in captures.items():
            # Keep stdout and stderr as run even when an app writes help to stderr.
            text = capture["stdout"] + (("\n[stderr]\n" + capture["stderr"]) if capture["stderr"] else "")
            (evidence_dir / filenames[name]).write_text(text, encoding="utf-8")
        result["evidence_files"] = {name: str(evidence_dir / filename) for name, filename in filenames.items()}
        write_json(evidence_dir / "spirula-capabilities.json", result)
    return result


def image_inventory(image_dir: Path) -> dict[str, Any]:
    if not image_dir.is_dir():
        raise BackendError(f"Image directory does not exist: {image_dir}", failure_kind="INVALID_INPUT")
    records, unsupported, invalid, duplicate_names = [], [], [], {}
    for path in sorted(image_dir.rglob("*")):
        if not path.is_file():
            continue
        suffix = path.suffix.casefold()
        if suffix in RAW_IMAGE_SUFFIXES:
            unsupported.append({"relative_path": path.relative_to(image_dir).as_posix(), "reason": "RAW_CAMERA_FORMAT_NOT_VERIFIED_BY_INSTALLED_SPIRULA_HELP"})
            continue
        if suffix not in IMAGE_SUFFIXES:
            continue
        relative = path.relative_to(image_dir).as_posix()
        stat = path.stat()
        if stat.st_size <= 0:
            invalid.append({"relative_path": relative, "reason": "EMPTY_FILE"})
        if path.name in duplicate_names:
            duplicate_names[path.name].append(relative)
        else:
            duplicate_names[path.name] = [relative]
        records.append({"image_id": relative, "relative_path": relative, "source_path": str(path.resolve()), "size_bytes": stat.st_size, "sha256": sha256_file(path), "extension": suffix, "metadata_status": "PRESERVED_IN_ORIGINAL; EXIF_EXTRACTION_NOT_CONFIGURED"})
    duplicates = {name: paths for name, paths in duplicate_names.items() if len(paths) > 1}
    if unsupported:
        raise BackendError(f"Unsupported raw-format images are present and would be omitted: {unsupported[:5]}", failure_kind="INVALID_INPUT", details={"unsupported_images": unsupported})
    if invalid:
        raise BackendError(f"Invalid source images are present: {invalid[:5]}", failure_kind="INVALID_INPUT", details={"invalid_images": invalid})
    if duplicates:
        raise BackendError(f"Image basenames are not unique; COLMAP name mapping would be ambiguous: {list(duplicates)[:5]}", failure_kind="INVALID_INPUT", details={"duplicate_basenames": duplicates})
    if len(records) < 2:
        raise BackendError(f"At least two supported images are required; found {len(records)}", failure_kind="INVALID_INPUT")
    return {"image_count": len(records), "images": records, "formats": dict(sorted({ext: sum(1 for x in records if x["extension"] == ext) for ext in {x["extension"] for x in records}}.items())), "known_unsupported_images": unsupported, "duplicates": duplicates}


def _safe_child(base: Path, path: Path) -> bool:
    base_resolved, path_resolved = base.resolve(), path.resolve()
    return path_resolved == base_resolved or base_resolved in path_resolved.parents


def create_attempt_dir(project_root: Path, output_root: Path, stage: str, execution_id: int, retained_attempt_limit: int | None = None) -> Path:
    parent = output_root / "backend-runs" / stage
    if not _safe_child(project_root, parent):
        raise BackendError("Backend output path escapes the project workspace", failure_kind="UNSAFE_OUTPUT")
    parent.mkdir(parents=True, exist_ok=True)
    existing = sorted(p for p in parent.iterdir() if p.is_dir())
    if retained_attempt_limit is not None:
        if not isinstance(retained_attempt_limit, int) or isinstance(retained_attempt_limit, bool) or retained_attempt_limit < 1:
            raise BackendError("resource_policy.retained_attempt_limit must be a positive integer", failure_kind="INVALID_CONFIGURATION")
        if len(existing) >= retained_attempt_limit:
            raise BackendError(f"Retained {stage} attempts reached the configured limit ({retained_attempt_limit}); no attempts were deleted. Archive or remove engine-owned attempts manually before launching another.", failure_kind="RESOURCE_LIMIT", details={"retained_attempt_limit": retained_attempt_limit, "existing_attempts": [str(x) for x in existing]})
    path = parent / f"{utc_stamp()}-execution-{execution_id:04d}"
    path.mkdir(mode=0o700, parents=False, exist_ok=False)
    if not _safe_child(project_root, path):
        raise BackendError("Resolved attempt path escapes the project workspace", failure_kind="UNSAFE_OUTPUT")
    return path


def check_resources(path: Path, policy: dict[str, Any], stage: str) -> dict[str, Any]:
    usage = shutil.disk_usage(path)
    default_minimum = int(policy.get("minimum_free_disk_bytes", 2 * 1024**3))
    stage_key = "minimum_free_disk_bytes_sfm" if stage == "sfm" else "minimum_free_disk_bytes_training"
    minimum = int(policy.get(stage_key, default_minimum))
    if usage.free < minimum:
        raise BackendError(f"Insufficient free disk space for {stage}: {usage.free} bytes free, {minimum} required", failure_kind="RESOURCE_LIMIT", details={"free_bytes": usage.free, "minimum_required_bytes": minimum})
    return {"free_bytes_before": usage.free, "minimum_required_bytes": minimum, "filesystem_total_bytes": usage.total, "checked_path": str(path.resolve())}


def _parse_colmap(directory: Path, image_inventory_rows: list[dict] | None = None) -> dict[str, Any]:
    def exact(stream, size, what):
        data = stream.read(size)
        if len(data) != size:
            raise BackendError(f"Truncated COLMAP {what} in {directory}", failure_kind="SFM_INVALID")
        return data

    cameras_path, images_path, points_path = (directory / n for n in ("cameras.bin", "images.bin", "points3D.bin"))
    missing = [str(p) for p in (cameras_path, images_path, points_path) if not p.is_file()]
    if missing:
        raise BackendError(f"COLMAP reconstruction is incomplete; missing {missing}", failure_kind="SFM_INVALID")
    cameras: dict[int, dict] = {}
    with cameras_path.open("rb") as stream:
        count = struct.unpack("<Q", exact(stream, 8, "camera count"))[0]
        for _ in range(count):
            camera_id, model_id, width, height = struct.unpack("<iiQQ", exact(stream, 24, "camera record"))
            n_params = COLMAP_CAMERA_PARAMETER_COUNTS.get(model_id)
            if n_params is None:
                raise BackendError(f"Unsupported COLMAP camera model id {model_id}", failure_kind="SFM_INVALID")
            params = struct.unpack(f"<{n_params}d", exact(stream, 8*n_params, "camera parameters"))
            if camera_id in cameras or not width or not height or not all(math.isfinite(x) for x in params):
                raise BackendError("Invalid or duplicate COLMAP camera record", failure_kind="SFM_INVALID")
            cameras[camera_id] = {"model_id": model_id, "width": width, "height": height, "parameters": params}
        if stream.read(1):
            raise BackendError("Unexpected trailing bytes in cameras.bin", failure_kind="SFM_INVALID")
    images: dict[int, dict] = {}
    names: set[str] = set()
    image_point_ids: dict[int, list[int]] = {}
    camera_centers: list[dict[str, Any]] = []
    quaternion_norm_warnings: list[str] = []
    with images_path.open("rb") as stream:
        count = struct.unpack("<Q", exact(stream, 8, "image count"))[0]
        for _ in range(count):
            image_id = struct.unpack("<I", exact(stream, 4, "image id"))[0]
            pose = struct.unpack("<7d", exact(stream, 56, "image pose"))
            camera_id = struct.unpack("<I", exact(stream, 4, "image camera id"))[0]
            name_bytes = bytearray()
            while True:
                char = exact(stream, 1, "image name")[0]
                if char == 0: break
                name_bytes.append(char)
                if len(name_bytes) > 1024*1024: raise BackendError("COLMAP image name exceeds 1 MiB", failure_kind="SFM_INVALID")
            name = name_bytes.decode("utf-8", errors="strict")
            n_points2d = struct.unpack("<Q", exact(stream, 8, "2D point count"))[0]
            ids = []
            for _p in range(n_points2d):
                x, y, point_id = struct.unpack("<2dQ", exact(stream, 24, "2D point"))
                if not math.isfinite(x) or not math.isfinite(y): raise BackendError(f"Non-finite 2D point in image {name}", failure_kind="SFM_INVALID")
                ids.append(point_id)
            if image_id in images or name in names or camera_id not in cameras or not all(math.isfinite(x) for x in pose):
                raise BackendError(f"Invalid, duplicate, or unresolved COLMAP image record: {name}", failure_kind="SFM_INVALID")
            qw, qx, qy, qz, tx, ty, tz = pose
            qnorm = math.sqrt(qw*qw + qx*qx + qy*qy + qz*qz)
            if qnorm < 1e-9:
                raise BackendError(f"COLMAP image {name} has a zero-length rotation quaternion", failure_kind="SFM_INVALID")
            if abs(qnorm - 1.0) > 0.02:
                quaternion_norm_warnings.append(name)
            qw, qx, qy, qz = (x / qnorm for x in (qw, qx, qy, qz))
            rotation = (
                (1-2*(qy*qy+qz*qz), 2*(qx*qy-qz*qw), 2*(qx*qz+qy*qw)),
                (2*(qx*qy+qz*qw), 1-2*(qx*qx+qz*qz), 2*(qy*qz-qx*qw)),
                (2*(qx*qz-qy*qw), 2*(qy*qz+qx*qw), 1-2*(qx*qx+qy*qy)),
            )
            center = tuple(-(rotation[0][axis]*tx + rotation[1][axis]*ty + rotation[2][axis]*tz) for axis in range(3))
            camera_centers.append({"image_name": name, "xyz_sfm_units": center})
            images[image_id] = {"name": name, "camera_id": camera_id, "points2d": n_points2d, "pose": pose, "quaternion_norm": qnorm}
            image_point_ids[image_id] = ids
            names.add(name)
        if stream.read(1): raise BackendError("Unexpected trailing bytes in images.bin", failure_kind="SFM_INVALID")
    points, track_lengths, errors, reciprocal_failures = set(), [], [], 0
    with points_path.open("rb") as stream:
        count = struct.unpack("<Q", exact(stream, 8, "3D point count"))[0]
        for _ in range(count):
            point_id = struct.unpack("<Q", exact(stream, 8, "3D point id"))[0]
            xyz = struct.unpack("<3d", exact(stream, 24, "3D point position"))
            exact(stream, 3, "3D point colour")
            error = struct.unpack("<d", exact(stream, 8, "3D point error"))[0]
            n_track = struct.unpack("<Q", exact(stream, 8, "3D point track length"))[0]
            if point_id in points or not all(math.isfinite(x) for x in xyz) or not math.isfinite(error):
                raise BackendError("Invalid or duplicate COLMAP 3D point record", failure_kind="SFM_INVALID")
            points.add(point_id); track_lengths.append(n_track); errors.append(error)
            for _track in range(n_track):
                image_id, point2d_idx = struct.unpack("<ii", exact(stream, 8, "track item"))
                image_row = images.get(image_id)
                if image_row is None or point2d_idx < 0 or point2d_idx >= image_row["points2d"]:
                    raise BackendError("COLMAP track refers to an unknown image or 2D feature", failure_kind="SFM_INVALID")
                if image_point_ids[image_id][point2d_idx] != point_id: reciprocal_failures += 1
        if stream.read(1): raise BackendError("Unexpected trailing bytes in points3D.bin", failure_kind="SFM_INVALID")
    for image_id, point_ids in image_point_ids.items():
        for point_id in point_ids:
            if point_id != 0xFFFFFFFFFFFFFFFF and point_id not in points:
                raise BackendError(f"COLMAP image {images[image_id]['name']} references a missing 3D point", failure_kind="SFM_INVALID")
    source_names = {row["relative_path"]: row["image_id"] for row in (image_inventory_rows or [])}
    mapped, unmatched = [], []
    for row in images.values():
        normalized = Path(row["name"]).as_posix().removeprefix("./")
        image_id = source_names.get(normalized)
        if image_id is None:
            unmatched.append(row["name"])
        else:
            mapped.append(image_id)
    if image_inventory_rows and unmatched:
        raise BackendError(f"COLMAP registered-image mapping failed for {len(unmatched)} source names; sample={unmatched[:5]}", failure_kind="SFM_INVALID", details={"unmatched_names": unmatched})
    sorted_errors = sorted(errors)
    p50 = sorted_errors[len(sorted_errors)//2] if sorted_errors else None
    p90 = sorted_errors[min(len(sorted_errors)-1, int(len(sorted_errors)*.9))] if sorted_errors else None
    if image_inventory_rows:
        source_id_by_name = {row["relative_path"]: row["image_id"] for row in image_inventory_rows}
        for center in camera_centers:
            center["image_id"] = source_id_by_name.get(Path(center["image_name"]).as_posix().removeprefix("./"))
    if camera_centers:
        positions = [row["xyz_sfm_units"] for row in camera_centers]
        bounds = {axis: {"minimum": min(p[index] for p in positions), "maximum": max(p[index] for p in positions), "span": max(p[index] for p in positions)-min(p[index] for p in positions)} for index, axis in enumerate(("x", "y", "z"))}
        centroid = tuple(sum(p[index] for p in positions)/len(positions) for index in range(3))
        spread_rms = math.sqrt(sum(sum((p[index]-centroid[index])**2 for index in range(3)) for p in positions)/len(positions))
        bbox_diagonal = math.sqrt(sum(v["span"]**2 for v in bounds.values()))
        camera_distribution = {"pose_count": len(positions), "coordinate_frame": "COLMAP reconstruction; arbitrary non-metric SfM units", "axis_bounds": bounds, "centroid_xyz_sfm_units": centroid, "rms_distance_from_centroid_sfm_units": spread_rms, "bounding_box_diagonal_sfm_units": bbox_diagonal, "interpretation": "Pose-center distribution only; does not establish real-world scale, coverage of artwork surfaces, or visitor-safe navigation."}
    else:
        camera_distribution = None
    return {
        "format": "COLMAP_BINARY", "directory": str(directory.resolve()), "camera_count": len(cameras),
        "registered_image_count": len(images), "sparse_point_count": len(points),
        "camera_model_ids": sorted({x["model_id"] for x in cameras.values()}),
        "all_camera_parameters_finite": True, "all_camera_references_resolve": True,
        "all_image_poses_finite": True, "all_3d_points_finite": True,
        "registered_image_names": [x["name"] for x in images.values()], "camera_centers": camera_centers,
        "camera_quaternion_norm_warning_image_names": quaternion_norm_warnings, "camera_distribution": camera_distribution,
        "mapped_source_image_ids": mapped, "unmatched_source_image_names": unmatched,
        "mean_track_length": (sum(track_lengths)/len(track_lengths)) if track_lengths else None,
        "median_track_length": sorted(track_lengths)[len(track_lengths)//2] if track_lengths else None,
        "mean_point_reprojection_error_px": (sum(errors)/len(errors)) if errors else None,
        "p50_point_reprojection_error_px": p50, "p90_point_reprojection_error_px": p90,
        "track_reciprocal_reference_mismatches": reciprocal_failures,
    }


def validate_colmap_directory(sparse_root: Path, image_rows: list[dict], thresholds: dict[str, Any] | None = None) -> dict[str, Any]:
    thresholds = thresholds or {}
    has_direct_model = all((sparse_root / name).is_file() for name in ("cameras.bin", "images.bin", "points3D.bin"))
    model_dirs = ([sparse_root] if has_direct_model else
                  sorted((p for p in sparse_root.iterdir() if p.is_dir() and p.name.isdigit()), key=lambda p: int(p.name))
                  if sparse_root.is_dir() else [])
    if not model_dirs:
        raise BackendError(f"No numeric COLMAP model directories exist at {sparse_root}", failure_kind="SFM_INVALID")
    components = []
    for directory in model_dirs:
        components.append(_parse_colmap(directory, image_rows))
    components.sort(key=lambda x: x["registered_image_count"], reverse=True)
    primary = components[0]
    ratio = primary["registered_image_count"] / len(image_rows) if image_rows else 0.0
    min_ratio = float(thresholds.get("minimum_registered_ratio", 0.65))
    min_images = int(thresholds.get("minimum_registered_images", 8))
    max_mean_error = float(thresholds.get("maximum_mean_point_reprojection_error_px", 2.0))
    warnings = []
    if ratio < 0.95: warnings.append("NOT_ALL_SOURCE_IMAGES_REGISTERED")
    if len(components) > 1: warnings.append("MULTIPLE_RECONSTRUCTION_COMPONENTS")
    if primary["mean_track_length"] is None: warnings.append("TRACK_LENGTH_UNKNOWN")
    if primary["mean_point_reprojection_error_px"] is None: warnings.append("REPROJECTION_ERROR_UNKNOWN")
    elif primary["mean_point_reprojection_error_px"] > max_mean_error: warnings.append("MEAN_POINT_REPROJECTION_ERROR_ABOVE_CONFIGURED_LIMIT")
    elif primary["mean_point_reprojection_error_px"] <= 1e-9: warnings.append("REPROJECTION_ERROR_ZERO_OR_UNINFORMATIVE")
    if primary["camera_quaternion_norm_warning_image_names"]: warnings.append("CAMERA_ROTATION_QUATERNION_NORM_OUTSIDE_TOLERANCE")
    if primary["track_reciprocal_reference_mismatches"]: warnings.append("TRACK_RECIPROCITY_MISMATCH")
    if primary["camera_count"] < 1 or primary["registered_image_count"] < 2 or primary["sparse_point_count"] < 1:
        quality = "SFM_INVALID"
    elif ratio < min_ratio or primary["registered_image_count"] < min_images or len(components) > 1:
        quality = "SFM_NEEDS_REVIEW"
    elif warnings:
        quality = "SFM_VALID_WITH_WARNINGS"
    else:
        quality = "SFM_VALID"
    return {
        "schema_version": "exhibition-archive.sfm-validation.v1", "quality_gate": quality,
        "source_image_count": len(image_rows), "registered_image_ratio": ratio,
        "thresholds": {"minimum_registered_ratio": min_ratio, "minimum_registered_images": min_images, "maximum_mean_point_reprojection_error_px": max_mean_error},
        "component_count": len(components), "components": components,
        "primary_component": primary["directory"], "warnings": warnings,
        "interpretation": "Technical COLMAP integrity and coarse capture coverage only; does not guarantee 3DGS suitability or artwork fidelity.",
    }


def validate_gaussian_ply(path: Path) -> dict[str, Any]:
    file = Path(path)
    with file.open("rb") as stream:
        header = bytearray()
        while len(header) <= 1024*1024:
            line = stream.readline()
            if not line: raise BackendError("PLY header ended before end_header", failure_kind="MODEL_INVALID")
            header.extend(line)
            if line.strip() == b"end_header": break
        else: raise BackendError("PLY header exceeds 1 MiB", failure_kind="MODEL_INVALID")
        body_offset = stream.tell()
    try: lines = header.decode("ascii").splitlines()
    except UnicodeDecodeError as exc: raise BackendError("PLY header is not ASCII", failure_kind="MODEL_INVALID") from exc
    if not lines or lines[0] != "ply": raise BackendError("PLY magic header is missing", failure_kind="MODEL_INVALID")
    format_match = next((x.split() for x in lines if x.startswith("format ")), None)
    if not format_match or len(format_match) < 3: raise BackendError("PLY format declaration is missing", failure_kind="MODEL_INVALID")
    fmt = format_match[1]
    elements, current = [], None
    for line in lines:
        tokens = line.split()
        if not tokens: continue
        if tokens[0] == "element" and len(tokens) == 3:
            try:
                count = int(tokens[2])
            except ValueError as exc:
                raise BackendError(f"Invalid PLY element count: {line}", failure_kind="MODEL_INVALID") from exc
            if count < 0:
                raise BackendError(f"Negative PLY element count: {line}", failure_kind="MODEL_INVALID")
            current = {"name": tokens[1], "count": count, "properties": []}; elements.append(current)
        elif tokens[0] == "property" and current:
            if len(tokens) != 3 or tokens[1] not in PLY_TYPES:
                raise BackendError(f"Unsupported/non-scalar PLY property: {line}", failure_kind="MODEL_INVALID")
            current["properties"].append({"name": tokens[2], "type": tokens[1]})
    vertex = next((x for x in elements if x["name"] == "vertex"), None)
    if not vertex or vertex["count"] <= 0: raise BackendError("PLY has no non-empty vertex element", failure_kind="MODEL_INVALID")
    if any(e["name"] != "vertex" for e in elements):
        raise BackendError("PLY contains unsupported non-vertex elements; renderer compatibility cannot be verified", failure_kind="MODEL_UNSUPPORTED_SCHEMA")
    prop_names = [x["name"] for x in vertex["properties"]]
    required = {"x", "y", "z", "opacity", "f_dc_0", "f_dc_1", "f_dc_2", "scale_0", "scale_1", "scale_2", "rot_0", "rot_1", "rot_2", "rot_3"}
    missing = sorted(required - set(prop_names))
    if missing: raise BackendError(f"PLY does not contain the expected Gaussian properties: {missing}", failure_kind="MODEL_UNSUPPORTED_SCHEMA", details={"properties": prop_names})
    if len(set(prop_names)) != len(prop_names): raise BackendError("PLY vertex property names are duplicated", failure_kind="MODEL_INVALID")
    body_bytes = file.stat().st_size - body_offset
    sample_count, numeric_check = 0, "NOT_CHECKED"
    if fmt in {"binary_little_endian", "binary_big_endian"}:
        endian = "<" if fmt == "binary_little_endian" else ">"
        properties = vertex["properties"]
        record_bytes = sum(PLY_TYPES[p["type"]][1] for p in properties)
        expected_bytes = sum(e["count"] * sum(PLY_TYPES[p["type"]][1] for p in e["properties"]) for e in elements)
        if body_bytes < expected_bytes: raise BackendError(f"PLY body is truncated: {body_bytes} bytes present, {expected_bytes} required", failure_kind="MODEL_INVALID")
        if body_bytes != expected_bytes: raise BackendError(f"PLY has {body_bytes-expected_bytes} unexplained trailing body bytes", failure_kind="MODEL_INVALID")
        offsets, offset = {}, 0
        for prop in properties:
            offsets[prop["name"]] = offset; offset += PLY_TYPES[prop["type"]][1]
        target_sample_count = min(vertex["count"], 10000)
        indices = sorted({0, vertex["count"]-1, *(round(x*(vertex["count"]-1)/(target_sample_count-1)) for x in range(target_sample_count))} if target_sample_count > 1 else {0})
        sample_count = len(indices)
        with file.open("rb") as stream:
            for index in indices:
                for name in prop_names:
                    prop = next(p for p in properties if p["name"] == name)
                    code, size = PLY_TYPES[prop["type"]]
                    stream.seek(body_offset + index*record_bytes + offsets[name])
                    value = struct.unpack(endian + code, stream.read(size))[0]
                    if not math.isfinite(float(value)): raise BackendError(f"Non-finite {name} value in PLY vertex {index}", failure_kind="MODEL_INVALID")
        numeric_check = "ALL_VERTEX_PROPERTIES_FINITE" if sample_count == vertex["count"] else "SAMPLED_VERTEX_PROPERTIES_FINITE"
    elif fmt == "ascii":
        if body_bytes <= 0: raise BackendError("ASCII PLY body is empty", failure_kind="MODEL_INVALID")
        indices = {0, vertex["count"]-1}
        stride = max(1, vertex["count"] // 10000)
        indices.update(range(0, vertex["count"], stride))
        with file.open("rb") as stream:
            stream.seek(body_offset)
            for index in range(vertex["count"]):
                raw = stream.readline()
                if not raw:
                    raise BackendError("ASCII PLY body is truncated", failure_kind="MODEL_INVALID")
                if index in indices:
                    tokens = raw.split()
                    if len(tokens) != len(prop_names): raise BackendError(f"Malformed ASCII PLY row {index}", failure_kind="MODEL_INVALID")
                    values = dict(zip(prop_names, tokens))
                    for name in prop_names:
                        try:
                            finite = math.isfinite(float(values[name]))
                        except ValueError as exc:
                            raise BackendError(f"Non-numeric {name} value in PLY vertex {index}", failure_kind="MODEL_INVALID") from exc
                        if not finite:
                            raise BackendError(f"Non-finite {name} value in PLY vertex {index}", failure_kind="MODEL_INVALID")
                    sample_count += 1
            if stream.read(1):
                raise BackendError("ASCII PLY contains data beyond the declared vertex count", failure_kind="MODEL_INVALID")
        numeric_check = "ALL_VERTEX_PROPERTIES_FINITE" if sample_count == vertex["count"] else "SAMPLED_VERTEX_PROPERTIES_FINITE"
    else:
        raise BackendError(f"Unsupported PLY format {fmt}; no conversion was attempted", failure_kind="MODEL_UNSUPPORTED_SCHEMA")
    return {"path": str(file.resolve()), "format": fmt, "gaussian_count": vertex["count"], "file_bytes": file.stat().st_size,
            "body_bytes": body_bytes, "vertex_record_bytes": sum(PLY_TYPES[p["type"]][1] for p in vertex["properties"]),
            "property_names": prop_names, "schema": "3DGS_GAUSSIAN_PLY", "numeric_validation": numeric_check,
            "numeric_sample_count": sample_count, "sha256": sha256_file(file), "validation": "PLY completeness, Gaussian field schema, numeric sample, and full-file hash passed."}


def _process_matches(pid: int, expected_executable: str | None = None) -> bool:
    try:
        os.kill(pid, 0)
        if os.name == "posix" and os.getpgid(pid) != pid:
            return False
        if expected_executable:
            try:
                result = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=3)
            except PermissionError:
                # Some managed macOS sandboxes forbid spawning ps. The PID must
                # still exist and be the leader of the isolated process group
                # created by execute_process; process identity is then based on
                # durable PID/PGID provenance rather than a command-line probe.
                return True
            command = result.stdout.strip()
            if result.returncode != 0 or not command:
                return False
            if Path(expected_executable).name not in command:
                return False
        return True
    except (ProcessLookupError, PermissionError, OSError, subprocess.TimeoutExpired):
        return False


def process_alive(pid: int, expected_executable: str | None = None) -> bool:
    return _process_matches(pid, expected_executable)


def discover_checkpoints(root: Path) -> list[dict[str, Any]]:
    """Report checkpoint-looking files without claiming they can be resumed."""
    rows = []
    if not root.exists(): return rows
    for path in sorted(root.rglob("*")):
        if path.is_symlink(): continue
        name = path.name.casefold()
        if (path.is_file() and path.suffix.casefold() in {".ckpt", ".pth", ".pt", ".safetensors"}) or (path.is_dir() and (path.suffix.casefold() == ".ckpt" or "checkpoint" in name)):
            try: stat = path.stat()
            except OSError: continue
            rows.append({"path": str(path.resolve()), "is_directory": path.is_dir(), "size_bytes": stat.st_size if path.is_file() else None, "mtime_ns": stat.st_mtime_ns, "validation": "DISCOVERED_BY_NAME_ONLY"})
    return rows


def finalize_attempt_record(
    attempt_dir: Path,
    terminal_state: str,
    *,
    exit_code: int | None = None,
    exit_code_unavailable_reason: str | None = None,
    error_classification: str | None = None,
    error: str | None = None,
    pid: int | None = None,
    log_path: Path | str | None = None,
    elapsed_seconds: float | None = None,
    artifact_discovery_status: str | None = None,
    validation_result: dict[str, Any] | None = None,
    recovery_guidance: str | None = None,
    details: dict[str, Any] | None = None,
) -> bool:
    """Atomically finalize an attempt record without masking the original failure."""
    attempt_dir = Path(attempt_dir)
    record_path = attempt_dir / "command.json"
    if not record_path.is_file():
        return False
    try:
        record = read_json(record_path)
        if not isinstance(record, dict):
            raise ValueError("command.json root is not an object")
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        record["terminal_state"] = terminal_state
        record.setdefault("ended_at_utc", now)
        record.setdefault("process_terminal_state", terminal_state)
        prior_process = record.get("process") if isinstance(record.get("process"), dict) else {}
        record["process"] = {**prior_process, **({"pid": pid} if pid is not None else {}),
            **({"process_group_id": pid} if pid is not None and os.name == "posix" else {}),
            "state": record.get("process_terminal_state", terminal_state), "ended_at_utc": prior_process.get("ended_at_utc", now)}
        if pid is not None:
            record["pid"] = pid
        if exit_code is not None:
            record["exit_code"] = exit_code
            record.pop("exit_code_unavailable_reason", None)
        elif exit_code_unavailable_reason:
            record["exit_code_unavailable_reason"] = exit_code_unavailable_reason
        if log_path is not None:
            record["log_path"] = str(Path(log_path).resolve())
        if elapsed_seconds is not None:
            record["elapsed_seconds"] = elapsed_seconds
        if error_classification:
            record["error_classification"] = error_classification
        if error:
            record["error"] = error
        if artifact_discovery_status:
            record["artifact_discovery_status"] = artifact_discovery_status
        else:
            record.setdefault("artifact_discovery_status", "NOT_RUN")
        if validation_result is not None:
            record["validation_result"] = validation_result
        else:
            record.setdefault("validation_result", {"status": "NOT_RUN"})
        if recovery_guidance:
            record["recovery_guidance"] = recovery_guidance
        elif terminal_state not in {"SUCCEEDED", "VALIDATED"}:
            record.setdefault("recovery_guidance", "Review the attempt log and failure details; correct the recorded cause, then launch a new bounded attempt. Do not adopt partial artifacts.")
        if details:
            record["terminal_details"] = {**(record.get("terminal_details") or {}), **details}
        record["finalized_at_utc"] = now
        write_json(record_path, record)
        return True
    except Exception as exc:
        # SQLite and the backend log remain the durable evidence if this write fails.
        try:
            error_path = attempt_dir / "command.json.finalization-error.txt"
            temporary = error_path.with_suffix(error_path.suffix + ".tmp")
            temporary.write_text(f"Could not finalize {record_path}: {type(exc).__name__}: {exc}\n", encoding="utf-8")
            temporary.replace(error_path)
        except Exception:
            pass
        return False


def finalize_stage_attempt(
    output_root: Path,
    stage_id: str,
    execution_id: int,
    *,
    failure_kind: str,
    error: str,
    details: dict[str, Any] | None = None,
) -> bool:
    """Finalize the current engine attempt after validation or stage-level failure."""
    parent = Path(output_root) / "backend-runs" / stage_id
    suffix = f"-execution-{execution_id:04d}"
    if not parent.is_dir():
        return False
    attempt = next((p for p in sorted(parent.iterdir(), reverse=True) if p.is_dir() and p.name.endswith(suffix)), None)
    if attempt is None or not (attempt / "command.json").is_file():
        return False
    try:
        record = read_json(attempt / "command.json")
    except Exception:
        return False
    existing = record.get("terminal_state")
    process_states = {"NONZERO_EXIT", "CANCELLED", "TIMED_OUT", "PROCESS_LAUNCH_FAILED", "INTERRUPTED", "UNEXPECTED_EXCEPTION"}
    if existing in process_states:
        terminal = existing
    elif failure_kind == "TIMEOUT":
        terminal = "TIMED_OUT"
    elif failure_kind == "CANCELLED":
        terminal = "CANCELLED"
    elif failure_kind == "PROCESS_LAUNCH_FAILED":
        terminal = "PROCESS_LAUNCH_FAILED"
    elif failure_kind in {"MODEL_OUTPUT_MISSING", "MODEL_INVALID", "MODEL_UNSUPPORTED_SCHEMA", "MODEL_OUTPUT_AMBIGUOUS", "SFM_INVALID", "QUALITY_GATE_BLOCKED"}:
        terminal = "VALIDATION_FAILED"
    else:
        terminal = "STAGE_FAILED"
    artifact_status = "MISSING" if failure_kind == "MODEL_OUTPUT_MISSING" else (
        "FOUND_INVALID_OR_AMBIGUOUS" if failure_kind in {"MODEL_INVALID", "MODEL_UNSUPPORTED_SCHEMA", "MODEL_OUTPUT_AMBIGUOUS", "SFM_INVALID"} else "NOT_VALIDATED")
    return finalize_attempt_record(attempt, terminal,
        exit_code=record.get("exit_code"), exit_code_unavailable_reason=record.get("exit_code_unavailable_reason"),
        error_classification=failure_kind, error=error,
        pid=(record.get("process") or {}).get("pid") or record.get("pid"),
        log_path=record.get("log_path") or (attempt / "spirula.log"), elapsed_seconds=record.get("elapsed_seconds"),
        artifact_discovery_status=artifact_status,
        validation_result={"status": "FAILED", "failure_kind": failure_kind, "details": details or {}},
        details={"engine_execution_id": execution_id})


def execute_process(
    state,
    execution_id: int,
    stage_id: str,
    command: list[str],
    cwd: Path,
    log_path: Path,
    env_overrides: dict[str, str] | None = None,
    timeout_seconds: int | None = None,
    poll_interval: float = 1.0,
    checkpoint_root: Path | None = None,
) -> dict[str, Any]:
    if not command or not all(isinstance(x, str) for x in command):
        raise BackendError("Subprocess command must be a non-empty string argument array", failure_kind="INVALID_COMMAND")
    if poll_interval < 0.1 or poll_interval > 5:
        raise BackendError("poll_interval_seconds must be between 0.1 and 5", failure_kind="INVALID_CONFIGURATION")
    if not cwd.is_dir(): raise BackendError(f"Subprocess working directory is missing: {cwd}", failure_kind="UNSAFE_OUTPUT")
    if log_path.exists(): raise BackendError(f"Refusing to overwrite an existing process log: {log_path}", failure_kind="UNSAFE_OUTPUT")
    env = os.environ.copy()
    overrides = env_overrides or {}
    if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or not isinstance(value, str) for key, value in overrides.items()):
        raise BackendError("Environment overrides must map valid variable names to strings", failure_kind="INVALID_CONFIGURATION")
    env.update(overrides)
    started = time.monotonic()
    process = None
    returncode = None
    try:
        with log_path.open("xb") as log:
            process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                       shell=False, start_new_session=(os.name == "posix"), close_fds=True)
            launched_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            attempt_record_path = log_path.parent / "command.json"
            if attempt_record_path.is_file():
                attempt_record = read_json(attempt_record_path)
                attempt_record["process"] = {"pid": process.pid, "process_group_id": process.pid if os.name == "posix" else None,
                    "started_at_utc": launched_at, "state": "RUNNING"}
                attempt_record["pid"] = process.pid
                attempt_record["log_path"] = str(log_path.resolve())
                write_json(attempt_record_path, attempt_record)
            state.update_stage_execution(execution_id, command=command, workdir=str(cwd.resolve()), pid=process.pid,
                                         log_path=str(log_path.resolve()), details={"process_state": "RUNNING", "launched_at_utc": launched_at, "environment_override_keys": sorted(overrides)})
            current = state.stage(stage_id) or {}
            state.set_stage(stage_id, "RUNNING", current.get("input_fingerprint"), None, execution_id, {"process_state": "RUNNING", "pid": process.pid, "command": command, "workdir": str(cwd.resolve()), "log_path": str(log_path.resolve()), "started_monotonic": started})
            last_update = started
            while process.poll() is None:
                now = time.monotonic()
                if timeout_seconds and now-started > timeout_seconds:
                    try: os.killpg(process.pid, signal.SIGTERM)
                    except (ProcessLookupError, OSError): pass
                    try: process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        try: os.killpg(process.pid, signal.SIGKILL)
                        except (ProcessLookupError, OSError): pass
                        process.wait()
                    returncode = process.returncode
                    raise BackendError(f"{stage_id} process timed out after {timeout_seconds} seconds", failure_kind="TIMEOUT", details={"pid": process.pid, "exit_code": returncode, "log_path": str(log_path)})
                if now - last_update >= max(1.0, poll_interval):
                    elapsed = round(now-started, 2)
                    checkpoints = discover_checkpoints(checkpoint_root) if checkpoint_root else []
                    recent_lines = []
                    try:
                        from collections import deque
                        with log_path.open("r", encoding="utf-8", errors="replace") as log_reader:
                            recent_lines = [line.rstrip("\r\n") for line in deque(log_reader, maxlen=8)]
                    except OSError:
                        pass
                    current = state.stage(stage_id) or {}
                    state.set_stage(stage_id, "RUNNING", current.get("input_fingerprint"), None, execution_id, {"process_state": "RUNNING", "pid": process.pid, "command": command, "workdir": str(cwd.resolve()), "log_path": str(log_path.resolve()), "elapsed_seconds": elapsed, "progress": "UNKNOWN_NOT_REPORTED_BY_BACKEND", "recent_log_lines": recent_lines, "checkpoint_candidates": checkpoints})
                    state.update_stage_execution(execution_id, details={"elapsed_seconds": elapsed, "checkpoint_candidates": checkpoints, "recent_log_lines": recent_lines})
                    last_update = now
                time.sleep(poll_interval)
            returncode = process.wait()
            log.flush()
            os.fsync(log.fileno())
    except KeyboardInterrupt as exc:
        if process is not None and process.poll() is None:
            try: os.killpg(process.pid, signal.SIGTERM)
            except (ProcessLookupError, OSError): pass
            try: process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                try: os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, OSError): pass
                process.wait()
        returncode = process.returncode if process is not None else None
        elapsed = round(time.monotonic()-started, 2)
        finalize_attempt_record(log_path.parent, "CANCELLED", exit_code=returncode,
            exit_code_unavailable_reason=None if returncode is not None else "PROCESS_NOT_LAUNCHED",
            error_classification="CANCELLED", error="Execution interrupted by the operator.",
            pid=process.pid if process is not None else None, log_path=log_path, elapsed_seconds=elapsed)
        raise BackendError("Execution cancelled by the operator", failure_kind="CANCELLED",
                           details={"pid": process.pid if process is not None else None, "exit_code": returncode, "log_path": str(log_path)}) from exc
    except BackendError as exc:
        if process is not None and process.poll() is None:
            try: os.killpg(process.pid, signal.SIGTERM)
            except (ProcessLookupError, OSError): pass
            try: process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                try: os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, OSError): pass
                process.wait()
        returncode = process.returncode if process is not None else returncode
        elapsed = round(time.monotonic()-started, 2)
        terminal = {"TIMEOUT": "TIMED_OUT", "CANCELLED": "CANCELLED", "PROCESS_LAUNCH_FAILED": "PROCESS_LAUNCH_FAILED"}.get(exc.failure_kind, "EXECUTION_FAILED")
        finalize_attempt_record(log_path.parent, terminal, exit_code=returncode,
            exit_code_unavailable_reason=None if returncode is not None else ("PROCESS_NOT_LAUNCHED" if process is None else "EXIT_STATUS_UNAVAILABLE"),
            error_classification=exc.failure_kind, error=str(exc), pid=process.pid if process is not None else None,
            log_path=log_path, elapsed_seconds=elapsed, details=exc.details)
        raise
    except OSError as exc:
        if process is not None and process.poll() is None:
            try: os.killpg(process.pid, signal.SIGTERM)
            except (ProcessLookupError, OSError): pass
            try: process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                try: os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, OSError): pass
                process.wait()
        returncode = process.returncode if process is not None else None
        elapsed = round(time.monotonic()-started, 2)
        kind = "PROCESS_LAUNCH_FAILED" if process is None else "UNEXPECTED_EXCEPTION"
        message = f"Could not launch subprocess {command[0]}: {exc}" if process is None else f"Subprocess orchestration failed: {exc}"
        finalize_attempt_record(log_path.parent, "PROCESS_LAUNCH_FAILED" if process is None else "UNEXPECTED_EXCEPTION",
            exit_code=returncode, exit_code_unavailable_reason=None if returncode is not None else "PROCESS_NOT_LAUNCHED",
            error_classification=kind, error=message, pid=process.pid if process is not None else None,
            log_path=log_path, elapsed_seconds=elapsed)
        raise BackendError(message, failure_kind=kind, details={"command": command, "cwd": str(cwd), "exit_code": returncode, "log_path": str(log_path)}) from exc
    except Exception as exc:
        if process is not None and process.poll() is None:
            try: os.killpg(process.pid, signal.SIGTERM)
            except (ProcessLookupError, OSError): pass
            try: process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                try: os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, OSError): pass
                process.wait()
        returncode = process.returncode if process is not None else None
        elapsed = round(time.monotonic()-started, 2)
        message = f"Unexpected exception while executing {stage_id}: {type(exc).__name__}: {exc}"
        finalize_attempt_record(log_path.parent, "UNEXPECTED_EXCEPTION", exit_code=returncode,
            exit_code_unavailable_reason=None if returncode is not None else ("PROCESS_NOT_LAUNCHED" if process is None else "EXIT_STATUS_UNAVAILABLE"),
            error_classification="UNEXPECTED_EXCEPTION", error=message, pid=process.pid if process is not None else None,
            log_path=log_path, elapsed_seconds=elapsed)
        raise BackendError(message, failure_kind="UNEXPECTED_EXCEPTION", details={"exit_code": returncode, "log_path": str(log_path)}) from exc
    elapsed = round(time.monotonic()-started, 2)
    partial_sfm_output = stage_id == "sfm" and returncode == 3
    if returncode != 0 and not partial_sfm_output:
        cancelled = returncode in {-signal.SIGTERM, -signal.SIGKILL, 128+signal.SIGTERM, 128+signal.SIGINT}
        kind = "CANCELLED" if cancelled else "NONZERO_EXIT"
        terminal = "CANCELLED" if cancelled else "NONZERO_EXIT"
        message = f"{stage_id} subprocess exited with code {returncode}; see {log_path}"
        checkpoints = discover_checkpoints(checkpoint_root) if checkpoint_root else []
        finalize_attempt_record(log_path.parent, terminal, exit_code=returncode, error_classification=kind,
            error=message, pid=process.pid if process is not None else None, log_path=log_path,
            elapsed_seconds=elapsed, artifact_discovery_status="NOT_RUN", details={"checkpoint_candidates": checkpoints})
        state.update_stage_execution(execution_id, details={"process_state": terminal, "ended_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "exit_code": returncode, "elapsed_seconds": elapsed, "checkpoint_candidates": checkpoints})
        raise BackendError(message, failure_kind=kind, details={"exit_code": returncode, "elapsed_seconds": elapsed, "pid": process.pid, "log_path": str(log_path)})
    checkpoints = discover_checkpoints(checkpoint_root) if checkpoint_root else []
    terminal_state = "PARTIAL_OUTPUT" if partial_sfm_output else "SUCCEEDED"
    validation_status = "PENDING_PARTIAL_OUTPUT_VALIDATION" if partial_sfm_output else "PENDING"
    finalize_attempt_record(log_path.parent, terminal_state, exit_code=returncode, pid=process.pid, log_path=log_path,
        elapsed_seconds=elapsed, artifact_discovery_status="PENDING", validation_result={"status": validation_status},
        details={"checkpoint_candidates": checkpoints, "backend_partial_output": partial_sfm_output})
    state.update_stage_execution(execution_id, details={"process_state": terminal_state, "ended_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "exit_code": returncode, "elapsed_seconds": elapsed, "checkpoint_candidates": checkpoints, "backend_partial_output": partial_sfm_output})
    return {"pid": process.pid, "exit_code": returncode, "elapsed_seconds": elapsed, "log_path": str(log_path.resolve()), "command": command, "workdir": str(cwd.resolve()), "progress": "UNKNOWN_NOT_REPORTED_BY_BACKEND", "partial_output": partial_sfm_output}


def cancel_execution(execution: dict[str, Any], grace_seconds: float = 8.0) -> dict[str, Any]:
    pid = execution.get("pid")
    command = json.loads(execution.get("command_json") or "[]")
    expected = command[0] if command else None
    if not pid:
        raise BackendError("No launched process is recorded for this execution", failure_kind="NO_ACTIVE_PROCESS")
    pid = int(pid)
    if not process_alive(pid, expected):
        return {"status": "PROCESS_NOT_RUNNING", "pid": pid}
    if os.name != "posix":
        raise BackendError("Process-group cancellation is only implemented on POSIX hosts", failure_kind="CANCEL_UNSUPPORTED")
    os.killpg(pid, signal.SIGTERM)
    deadline = time.monotonic() + max(0.0, grace_seconds)
    while time.monotonic() < deadline:
        if not process_alive(pid, expected):
            return {"status": "TERMINATED_GRACEFULLY", "pid": pid}
        time.sleep(0.2)
    if process_alive(pid, expected):
        os.killpg(pid, signal.SIGKILL)
        return {"status": "TERMINATED_FORCEFULLY", "pid": pid}
    return {"status": "TERMINATED_GRACEFULLY", "pid": pid}


def latest_stage_execution(state, stage_id: str) -> dict[str, Any] | None:
    row = state.db.execute("SELECT * FROM stage_executions WHERE stage_id=? ORDER BY execution_id DESC LIMIT 1", (stage_id,)).fetchone()
    return dict(row) if row else None


def reconcile_running_stage(state, stage_id: str) -> dict[str, Any] | None:
    stage = state.stage(stage_id)
    if not stage or stage["status"] != "RUNNING": return None
    execution = state.db.execute("SELECT * FROM stage_executions WHERE execution_id=?", (stage.get("last_execution_id"),)).fetchone()
    if not execution or execution["status"] != "RUNNING": return None
    execution = dict(execution)
    command = json.loads(execution.get("command_json") or "[]")
    pid = execution.get("pid")
    try:
        updated = datetime.fromisoformat(str(stage.get("updated_at", "")).replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - updated).total_seconds()
    except (ValueError, TypeError):
        age = 0
    # Source hashing and backend discovery happen before Popen. Do not call a
    # slow prepare phase interrupted while it is still producing its first PID.
    if not pid and age < 120:
        return {"status": "STARTING", "execution_id": execution.get("execution_id")}
    if pid and command and age < 3 and not process_alive(int(pid), command[0]):
        return {"status": "EXITED_AWAITING_FINALIZATION", "pid": int(pid), "execution_id": execution.get("execution_id")}
    if pid and command and process_alive(int(pid), command[0]):
        return {"status": "RUNNING", "pid": int(pid), "log_path": execution.get("log_path"), "execution_id": execution.get("execution_id")}
    details = json.loads(execution.get("details_json") or "{}")
    details.update({"process_state": "INTERRUPTED", "reconciled_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "error": "Backend process no longer exists; no automatic process reattachment or checkpoint resume was attempted."})
    raw_log_path = execution.get("log_path")
    log_path = Path(raw_log_path) if raw_log_path else None
    if log_path is not None and log_path.parent.is_dir():
        finalize_attempt_record(log_path.parent, "INTERRUPTED", exit_code_unavailable_reason="PROCESS_EXIT_STATUS_LOST_WITH_ORCHESTRATOR",
            error_classification="INTERRUPTED", error=details["error"], pid=int(pid) if pid else None,
            log_path=log_path, artifact_discovery_status="NOT_RUN",
            validation_result={"status": "NOT_RUN"}, details={"execution_id": execution["execution_id"]})
    state.finish_stage(execution["execution_id"], "INTERRUPTED", None, details, details["error"])
    state.set_stage(stage_id, "FAILED", execution.get("input_fingerprint"), None, execution["execution_id"], details)
    return {"status": "INTERRUPTED", "execution_id": execution["execution_id"], "details": details}


def build_sfm_command(executable: Path, image_dir: Path, output_dir: Path, cfg: dict[str, Any], capabilities: dict[str, Any]) -> list[str]:
    quality = cfg.get("quality", "low")
    data_type = cfg.get("data_type", "individual")
    if quality not in {"low", "medium", "high", "extreme"}: raise BackendError(f"Unsupported Spirula SfM quality: {quality}", failure_kind="INVALID_CONFIGURATION")
    if data_type not in {"individual", "video", "internet"}: raise BackendError(f"Unsupported Spirula SfM data_type: {data_type}", failure_kind="INVALID_CONFIGURATION")
    supported = set(capabilities["sfm"]["auto_arguments"])
    command = [str(executable), "sfm", "auto", str(image_dir.resolve()), "-o", str(output_dir.resolve()), "--quality", quality, "--data-type", data_type]
    device = cfg.get("device")
    if device is not None:
        if "--device" not in supported or not isinstance(device, str) or not device.strip():
            raise BackendError("sfm.device must be a non-empty device name/index supported by the installed Spirula version", failure_kind="INVALID_CONFIGURATION")
        command += ["--device", device]
    size = cfg.get("max_image_size")
    if size is not None:
        if "--max-image-size" not in supported or not isinstance(size, int) or size < 320: raise BackendError("max_image_size must be an integer >=320 supported by this Spirula version", failure_kind="INVALID_CONFIGURATION")
        command += ["--max-image-size", str(size)]
    extras = cfg.get("additional_args", [])
    if not isinstance(extras, list) or not all(isinstance(x, str) for x in extras): raise BackendError("sfm.additional_args must be a string array", failure_kind="INVALID_CONFIGURATION")
    forbidden = {"-o", "--output", "--progress-dir", "--manifest", "--masks", "--quality", "--data-type", "--max-image-size", "--device"}
    extra_flags = [arg for arg in extras if arg.startswith("-")]
    unknown = sorted(set(extra_flags)-supported-{"-o"})
    if unknown: raise BackendError(f"Unsupported SfM extra flags: {unknown}", failure_kind="INVALID_CONFIGURATION")
    if any(flag in forbidden for flag in extra_flags): raise BackendError("SfM extra args may not override resolved quality, input, masks, or output settings", failure_kind="INVALID_CONFIGURATION")
    command.extend(extras)
    return command


def build_training_command(executable: Path, dataset_dir: Path, attempt_dir: Path, cfg: dict[str, Any], capabilities: dict[str, Any], attempt_name: str) -> list[str]:
    supported = set(capabilities["training"]["arguments"])
    required = {"preset", "iterations"}
    missing = [key for key in required if key not in cfg]
    if missing: raise BackendError(f"Fresh Spirula training requires explicit configuration values: {missing}", failure_kind="INVALID_CONFIGURATION")
    preset = cfg["preset"]
    if preset not in capabilities["training"]["presets"]: raise BackendError(f"Unsupported Spirula training preset: {preset}", failure_kind="INVALID_CONFIGURATION")
    iterations = cfg["iterations"]
    if not isinstance(iterations, int) or iterations < 1: raise BackendError("training.iterations must be a positive integer", failure_kind="INVALID_CONFIGURATION")
    data_format = cfg.get("data_format", "colmap")
    if data_format not in {"colmap", "nerfstudio", "metashape"}: raise BackendError(f"Unsupported training data format: {data_format}", failure_kind="INVALID_CONFIGURATION")
    command = [str(executable), "train", preset, "--data", str(dataset_dir.resolve()), "--data-format", data_format,
               "--image-dir", cfg.get("image_dir", "images"), "--colmap-recon-dir", cfg.get("colmap_recon_dir", "sparse/0"),
               "--output-dir-prefix", str((attempt_dir / "outputs").resolve()), "--output-dir-name", attempt_name,
               "--num-iterations", str(iterations), "--disable-viewer", "1", "--keep-viewer-alive", "0"]
    divisor = cfg.get("resolution_divisor", 4)
    if "--train-resolution-divisor" not in supported or not isinstance(divisor, int) or divisor < 1:
        raise BackendError("training.resolution_divisor must be a positive integer supported by Spirula", failure_kind="INVALID_CONFIGURATION")
    command += ["--train-resolution-divisor", str(divisor)]
    device = cfg.get("device", "auto")
    if not isinstance(device, str) or not device.strip(): raise BackendError("training.device must be a non-empty string", failure_kind="INVALID_CONFIGURATION")
    if "--device" in supported: command += ["--device", device]
    if "--steps-per-save" in supported: command += ["--steps-per-save", str(cfg.get("steps_per_save", 100))]
    if "--save-only-latest-checkpoint" in supported: command += ["--save-only-latest-checkpoint", "0"]
    if "--save-full-checkpoint" in supported: command += ["--save-full-checkpoint", "1"]
    seed = cfg.get("seed")
    if seed is not None:
        if "--seed" not in supported: raise BackendError("Configured seed is not supported by the installed Spirula version", failure_kind="INVALID_CONFIGURATION")
        command += ["--seed", str(seed)]
    extras = cfg.get("additional_args", [])
    if not isinstance(extras, list) or not all(isinstance(x, str) for x in extras): raise BackendError("training.additional_args must be a string array", failure_kind="INVALID_CONFIGURATION")
    protected = {"--data", "--data-format", "--image-dir", "--colmap-recon-dir", "--output-dir-prefix", "--output-dir-name", "--num-iterations", "--device", "--train-resolution-divisor", "--resume", "--init-ply", "--save-full-checkpoint", "--save-only-latest-checkpoint", "--steps-per-save", "--disable-viewer", "--keep-viewer-alive"}
    flags = [arg for arg in extras if arg.startswith("-")]
    unknown, blocked = sorted(set(flags)-supported), sorted(set(flags)&protected)
    if unknown: raise BackendError(f"Unsupported training extra flags: {unknown}", failure_kind="INVALID_CONFIGURATION")
    if blocked: raise BackendError(f"Training extra flags cannot override protected configuration: {blocked}", failure_kind="INVALID_CONFIGURATION")
    command.extend(extras)
    return command


def prepare_training_dataset(attempt_dir: Path, image_dir: Path, reconstruction_dir: Path, *, copy_inputs: bool = False,
                            registered_image_names: list[str] | None = None) -> Path:
    """Prepare a COLMAP training workspace, optionally materializing only registered views.

    Production fresh-training callers pass the exact image names parsed from the
    selected component's images.bin. Each image remains at the same relative path
    that COLMAP records; unrelated source images are never exposed to the backend.
    """
    dataset = attempt_dir / "dataset"
    if dataset.exists(): raise BackendError(f"Refusing to overwrite training dataset workspace: {dataset}", failure_kind="UNSAFE_OUTPUT")
    dataset.mkdir(mode=0o700)
    (dataset / "sparse").mkdir()
    if copy_inputs:
        if any(p.is_symlink() for root in (image_dir, reconstruction_dir) for p in [root, *root.rglob("*")]):
            raise BackendError("Smoke-test inputs may not contain symlinks", failure_kind="INVALID_INPUT")
        if registered_image_names is None:
            shutil.copytree(image_dir, dataset / "images", copy_function=shutil.copy2)
        shutil.copytree(reconstruction_dir, dataset / "sparse" / "0", copy_function=shutil.copy2)
    else:
        if registered_image_names is None:
            (dataset / "images").symlink_to(image_dir.resolve(), target_is_directory=True)
        (dataset / "sparse" / "0").symlink_to(reconstruction_dir.resolve(), target_is_directory=True)
    if not (dataset / "sparse" / "0" / "images.bin").is_file(): raise BackendError("Training dataset does not resolve to a COLMAP images.bin", failure_kind="SFM_INVALID")
    if registered_image_names is not None:
        inventory = image_inventory(image_dir)
        source_by_name = {row["relative_path"]: row for row in inventory["images"]}
        normalized_names = []
        casefold_names = set()
        for raw_name in registered_image_names:
            name = str(raw_name)
            parsed = PurePosixPath(name)
            if (not name or "\\" in name or parsed.is_absolute() or
                    any(part in {"", ".", ".."} for part in parsed.parts)):
                raise BackendError(f"Unsafe COLMAP registered image path: {name!r}", failure_kind="SFM_INVALID")
            normalized = parsed.as_posix()
            if normalized != name:
                raise BackendError(f"COLMAP registered image path is not canonical: {name!r}", failure_kind="SFM_INVALID")
            folded = normalized.casefold()
            if folded in casefold_names:
                raise BackendError(f"Registered image paths collide on a case-insensitive filesystem: {normalized}", failure_kind="SFM_INVALID")
            casefold_names.add(folded)
            normalized_names.append(normalized)
        if len(set(normalized_names)) != len(normalized_names):
            raise BackendError("COLMAP registered image list contains duplicate paths", failure_kind="SFM_INVALID")
        missing = sorted(set(normalized_names) - set(source_by_name))
        if missing:
            raise BackendError("Selected COLMAP component references images missing from the source dataset",
                               failure_kind="SFM_INVALID", details={"missing_images": missing[:20]})
        selected_root = dataset / "images"
        selected_root.mkdir()
        root_resolved = selected_root.resolve()
        for name in normalized_names:
            source = Path(source_by_name[name]["source_path"]).resolve()
            if image_dir.resolve() not in source.parents or source.is_symlink() or not source.is_file():
                raise BackendError(f"Registered image source is not a regular file beneath the configured image directory: {name}",
                                   failure_kind="INVALID_INPUT")
            destination = selected_root.joinpath(*PurePosixPath(name).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if root_resolved not in destination.parent.resolve().parents and destination.parent.resolve() != root_resolved:
                raise BackendError(f"Registered image destination escapes the training dataset: {name}", failure_kind="UNSAFE_OUTPUT")
            if copy_inputs:
                shutil.copy2(source, destination)
            else:
                destination.symlink_to(source)
        actual_names = sorted(path.relative_to(selected_root).as_posix() for path in selected_root.rglob("*") if path.is_file())
        if actual_names != sorted(normalized_names):
            raise BackendError("Materialized training image membership differs from the accepted COLMAP component",
                               failure_kind="INVALID_INPUT", details={"expected": sorted(normalized_names), "actual": actual_names})
        for name in normalized_names:
            source = Path(source_by_name[name]["source_path"]).resolve()
            target = selected_root.joinpath(*PurePosixPath(name).parts)
            if not target.is_file() or sha256_file(target) != source_by_name[name]["sha256"]:
                raise BackendError(f"Materialized registered image failed hash verification: {name}", failure_kind="INVALID_INPUT")
            if not copy_inputs and (not target.is_symlink() or target.resolve() != source):
                raise BackendError(f"Registered training image is not a symlink to its verified source: {name}", failure_kind="INVALID_INPUT")
    return dataset


def discover_model_files(attempt_dir: Path, selector: str | None = None, *, allow_multiple: bool = False) -> list[Path]:
    files = sorted(p for p in attempt_dir.rglob("*") if p.is_file() and not p.is_symlink() and p.suffix.casefold() == ".ply")
    if selector:
        import fnmatch
        files = [p for p in files if fnmatch.fnmatch(p.relative_to(attempt_dir).as_posix(), selector)]
    if not files: raise BackendError(f"No PLY model artifacts found beneath {attempt_dir}", failure_kind="MODEL_OUTPUT_MISSING")
    if len(files) > 1 and not allow_multiple: raise BackendError(f"Multiple PLY outputs found; set training.artifact_selector to choose one: {[str(x.relative_to(attempt_dir)) for x in files]}", failure_kind="MODEL_OUTPUT_AMBIGUOUS", details={"candidates": [str(x) for x in files]})
    return files

