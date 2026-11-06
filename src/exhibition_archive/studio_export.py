"""Portable COLMAP-folder export for reopening a completed SfM result."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
from pathlib import Path, PurePath, PurePosixPath
from typing import Any

from .adapters import sha256_file, write_json
from .backends import BackendError, _parse_colmap, image_inventory


EXPORT_SCHEMA = "exhibition-archive.spirula-studio-colmap-export.v1"
SPARSE_FILES = ("cameras.bin", "images.bin", "points3D.bin")
STAGE_EXPORT_SCHEMA = "exhibition-archive.spirula-export-stage.v1"


def _safe_name(raw: str) -> PurePosixPath:
    name = PurePosixPath(raw)
    if (not raw or "\\" in raw or name.is_absolute() or
            any(part in {"", ".", ".."} for part in name.parts)):
        raise BackendError(f"COLMAP image path is not portable: {raw!r}", failure_kind="SFM_INVALID")
    if name.as_posix() != raw:
        raise BackendError(f"COLMAP image path is not canonical: {raw!r}", failure_kind="SFM_INVALID")
    return name


def _dataset_fingerprint(source_images: Path, reconstruction: Path,
                         registered_names: list[str], image_rows: list[dict[str, Any]],
                         sparse_hashes: dict[str, str] | None = None) -> str:
    by_name = {row["relative_path"]: row for row in image_rows}
    document = {
        "schema": EXPORT_SCHEMA,
        "reconstruction": sparse_hashes or {name: sha256_file(reconstruction / name) for name in SPARSE_FILES},
        "images": [{"name": name, "sha256": by_name[name]["sha256"]} for name in sorted(registered_names)],
    }
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:20]


def _ensure_directory(path: Path) -> None:
    if path.is_symlink():
        raise BackendError(f"Refusing symlink inside staged Spirula export: {path}", failure_kind="SFM_INVALID")
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise BackendError(f"Staged Spirula export path is not a directory: {path}", failure_kind="SFM_INVALID")


def _staging_target(staging: Path, prefix: str, relative: PurePosixPath) -> Path:
    current = staging / prefix
    _ensure_directory(current)
    for part in relative.parts[:-1]:
        current = current / part
        _ensure_directory(current)
    target = current / relative.parts[-1]
    if target.is_symlink():
        raise BackendError(f"Refusing symlink inside staged Spirula export: {target}", failure_kind="SFM_INVALID")
    return target


def _unlink_staging_file(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise BackendError(f"Refusing non-file temporary export path: {path}", failure_kind="SFM_INVALID")
    if path.exists():
        path.unlink()


def _copy_verified(source: Path, target: Path, expected_sha256: str) -> None:
    if source.is_symlink() or not source.is_file():
        raise BackendError(f"Export source is missing or unsafe: {source}", failure_kind="SFM_INVALID")
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise BackendError(f"Staged export target is unsafe: {target}", failure_kind="SFM_INVALID")
    temporary = target.with_name(target.name + ".copying")
    if temporary.is_symlink() or (temporary.exists() and not temporary.is_file()):
        raise BackendError(f"Staged export temporary file is unsafe: {temporary}", failure_kind="SFM_INVALID")
    if target.is_file() and sha256_file(target) == expected_sha256:
        _unlink_staging_file(temporary)
        return
    _unlink_staging_file(temporary)
    shutil.copy2(source, temporary)
    if sha256_file(temporary) != expected_sha256:
        raise BackendError(f"Source changed while copying into the Spirula export: {source}",
                           failure_kind="SFM_INVALID")
    os.replace(temporary, target)


def _manifest_metadata(value: Any, *, image_root: Path, sparse_root: Path,
                       field: str = "source_provenance") -> Any:
    """Allow JSON primitives and explicit package-relative source paths only."""
    if isinstance(value, PurePath):
        raw = Path(value)
        if raw.is_absolute():
            resolved = raw.resolve(strict=False)
            for root, prefix in ((image_root, "images"), (sparse_root, "sparse/0")):
                try:
                    relative = resolved.relative_to(root)
                except ValueError:
                    continue
                suffix = relative.as_posix()
                return f"{prefix}/{suffix}" if suffix != "." else prefix
            raise BackendError(f"Absolute path in portable {field} is outside the exported source roots: {raw}",
                               failure_kind="SFM_INVALID")
        raw_value = raw.as_posix()
        portable = PurePosixPath(raw_value)
        if (not raw_value or "\\" in raw_value or portable.is_absolute() or
                any(part in {"", ".", ".."} for part in portable.parts)):
            raise BackendError(f"Path in portable {field} must be normalized and relative: {raw_value!r}",
                               failure_kind="SFM_INVALID")
        return portable.as_posix()
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise BackendError(f"{field} contains a non-string JSON object key", failure_kind="SFM_INVALID")
        return {key: _manifest_metadata(item, image_root=image_root, sparse_root=sparse_root,
                                        field=f"{field}.{key}") for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_manifest_metadata(item, image_root=image_root, sparse_root=sparse_root,
                                   field=f"{field}[{index}]") for index, item in enumerate(value)]
    if value is None or type(value) in {str, int, bool}:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise BackendError(f"Unsupported value in portable {field}: {type(value).__name__}",
                       failure_kind="SFM_INVALID")


def _output_relative_path(value: Any, output_root: Path, field: str) -> str:
    if not isinstance(value, (str, PurePath)):
        raise BackendError(f"{field} must be a filesystem path", failure_kind="SFM_INVALID")
    raw = Path(value)
    if raw.is_absolute():
        resolved = raw.resolve(strict=False)
    else:
        relative = PurePosixPath(raw.as_posix())
        if (not raw.as_posix() or "\\" in raw.as_posix() or
                any(part in {"", ".", ".."} for part in relative.parts)):
            raise BackendError(f"{field} must be a normalized path beneath the output root: {raw}",
                               failure_kind="SFM_INVALID")
        resolved = (output_root / Path(*relative.parts)).resolve(strict=False)
    try:
        return resolved.relative_to(output_root).as_posix()
    except ValueError as exc:
        raise BackendError(f"{field} escapes the output root: {raw}", failure_kind="SFM_INVALID") from exc


def _report_json_value(value: Any, output_root: Path, field: str) -> Any:
    if isinstance(value, PurePath):
        return _output_relative_path(value, output_root, field)
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise BackendError(f"{field} contains a non-string JSON object key", failure_kind="SFM_INVALID")
        return {key: _report_json_value(item, output_root, f"{field}.{key}")
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_report_json_value(item, output_root, f"{field}[{index}]")
                for index, item in enumerate(value)]
    if value is None or type(value) in {str, int, bool}:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise BackendError(f"Unsupported value in export report field {field}: {type(value).__name__}",
                       failure_kind="SFM_INVALID")


def build_studio_export_stage_report(result: dict[str, Any], output_root: str | Path) -> dict[str, Any]:
    """Project the internal result into a strict, portable Engine JSON schema."""
    expected = {"status", "package_directory", "manifest_path", "validation", "artifacts"}
    if not isinstance(result, dict) or set(result) != expected:
        raise BackendError("Unexpected internal result fields for Spirula export report", failure_kind="SFM_INVALID",
                           details={"expected": sorted(expected), "actual": sorted(result) if isinstance(result, dict) else None})
    if result["status"] not in {"EXPORTED_AND_STRUCTURALLY_VALIDATED", "REUSED_VALID_EXPORT"}:
        raise BackendError(f"Unsupported Spirula export status: {result['status']!r}", failure_kind="SFM_INVALID")
    root = Path(output_root).expanduser().resolve()
    validation = result["validation"]
    if not isinstance(validation, dict):
        raise BackendError("Spirula export validation must be a JSON object", failure_kind="SFM_INVALID")
    validation = dict(validation)
    if "manifest_path" in validation:
        validation["manifest_path"] = _output_relative_path(validation["manifest_path"], root,
                                                            "validation.manifest_path")
    artifacts = result["artifacts"]
    if not isinstance(artifacts, (list, tuple)):
        raise BackendError("Spirula export artifacts must be a list of paths", failure_kind="SFM_INVALID")
    return {
        "schema_version": STAGE_EXPORT_SCHEMA,
        "status": result["status"],
        "package_directory": _output_relative_path(result["package_directory"], root, "package_directory"),
        "manifest_path": _output_relative_path(result["manifest_path"], root, "manifest_path"),
        "artifacts": [_output_relative_path(path, root, f"artifacts[{index}]")
                      for index, path in enumerate(artifacts)],
        "validation": _report_json_value(validation, root, "validation"),
        "gui_reopen_status": "NOT_GUI_VERIFIED",
    }


def validate_studio_dataset(package: str | Path) -> dict[str, Any]:
    raw_root = Path(package).expanduser()
    if raw_root.is_symlink():
        raise BackendError(f"Spirula export directory is a symlink: {raw_root}", failure_kind="SFM_INVALID")
    root = raw_root.resolve()
    manifest_path = root / "manifest.json"
    if root.is_symlink() or not root.is_dir() or manifest_path.is_symlink() or not manifest_path.is_file():
        raise BackendError(f"Spirula export directory or manifest is missing/unsafe: {root}", failure_kind="SFM_INVALID")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackendError(f"Spirula export manifest is unreadable: {manifest_path}", failure_kind="SFM_INVALID") from exc
    if manifest.get("schema_version") != EXPORT_SCHEMA:
        raise BackendError("Unsupported Spirula export manifest schema", failure_kind="SFM_INVALID")
    reconstruction = root / "sparse" / "0"
    for name in SPARSE_FILES:
        path = reconstruction / name
        if path.is_symlink() or not path.is_file() or sha256_file(path) != manifest.get("sparse_files", {}).get(name, {}).get("sha256"):
            raise BackendError(f"Spirula export sparse artifact is missing, unsafe, or changed: {path}", failure_kind="SFM_INVALID")
    rows = image_inventory(root / "images")["images"]
    image_hashes = {row["relative_path"]: row["sha256"] for row in rows}
    expected = {row["relative_path"]: row["sha256"] for row in manifest.get("images", [])}
    if image_hashes != expected:
        raise BackendError("Spirula export image membership or content differs from its manifest", failure_kind="SFM_INVALID",
                           details={"missing": sorted(set(expected) - set(image_hashes)),
                                    "unexpected": sorted(set(image_hashes) - set(expected))})
    parsed = _parse_colmap(reconstruction, rows)
    registered = {row["name"] for row in parsed.get("registered_images", [])}
    if registered != set(expected):
        raise BackendError("Spirula export registered image names do not resolve exactly", failure_kind="SFM_INVALID",
                           details={"missing": sorted(registered - set(expected)),
                                    "unexpected": sorted(set(expected) - registered)})
    image_details = {row["relative_path"]: row for row in manifest.get("images", [])}
    dimension_mismatches = []
    for row in parsed.get("registered_images", []):
        exported = image_details[row["name"]]
        if exported.get("camera_dimensions") != [row["width"], row["height"]]:
            dimension_mismatches.append({"name": row["name"], "camera": [row["width"], row["height"]],
                                         "exported": exported.get("camera_dimensions")})
    if dimension_mismatches:
        raise BackendError("Exported image dimensions do not match the COLMAP camera records",
                           failure_kind="SFM_INVALID", details={"mismatches": dimension_mismatches[:20]})
    return {"status": "STRUCTURE_VALIDATED", "gui_reopen_status": "NOT_GUI_VERIFIED",
            "image_count": len(registered), "camera_count": parsed["camera_count"],
            "sparse_point_count": parsed["sparse_point_count"],
            "registered_image_names_resolve": True, "camera_dimensions_match": True,
            "portable_relative_layout": True, "manifest_path": str(manifest_path)}


def export_spirula_colmap_dataset(source_images: str | Path, reconstruction: str | Path,
                                  destination_root: str | Path, *, project_id: str,
                                  source_provenance: dict[str, Any] | None = None) -> dict[str, Any]:
    """Copy only registered input images and binary sparse geometry into a portable package."""
    raw_images_dir = Path(source_images).expanduser()
    raw_sparse_dir = Path(reconstruction).expanduser()
    if raw_images_dir.is_symlink() or raw_sparse_dir.is_symlink():
        raise BackendError("SfM image or sparse directory is a symlink", failure_kind="SFM_INVALID")
    images_dir = raw_images_dir.resolve()
    sparse_dir = raw_sparse_dir.resolve()
    output_root = Path(destination_root).expanduser().resolve()
    if images_dir.is_symlink() or sparse_dir.is_symlink() or not images_dir.is_dir() or not sparse_dir.is_dir():
        raise BackendError("SfM image or sparse directory is missing or is a symlink", failure_kind="SFM_INVALID")
    for name in SPARSE_FILES:
        path = sparse_dir / name
        if path.is_symlink() or not path.is_file():
            raise BackendError(f"Validated COLMAP sparse model is missing {name}", failure_kind="SFM_INVALID")
    inventory = image_inventory(images_dir)
    # Read the reconstruction's own image names first, then compare against
    # the source inventory below so a missing registered image is reported as
    # an export membership error with its actual filename.
    parsed = _parse_colmap(sparse_dir)
    registered = parsed.get("registered_images", [])
    if not registered:
        raise BackendError("COLMAP sparse model contains no registered camera images", failure_kind="SFM_INVALID")
    names = [row["name"] for row in registered]
    if len(names) != len(set(names)):
        raise BackendError("COLMAP sparse model contains duplicate registered image names", failure_kind="SFM_INVALID")
    source_by_name = {row["relative_path"]: row for row in inventory["images"]}
    missing = sorted(set(names) - set(source_by_name))
    if missing:
        raise BackendError("Some registered COLMAP images are absent from the effective SfM dataset",
                           failure_kind="SFM_INVALID", details={"missing_images": missing[:50]})
    sparse_source_hashes = {name: sha256_file(sparse_dir / name) for name in SPARSE_FILES}
    key = _dataset_fingerprint(images_dir, sparse_dir, names, inventory["images"], sparse_source_hashes)
    output_root.mkdir(parents=True, exist_ok=True)
    package = output_root / f"revision-{key}"
    if package.exists():
        validation = validate_studio_dataset(package)
        return {"status": "REUSED_VALID_EXPORT", "package_directory": str(package),
                "manifest_path": str(package / "manifest.json"), "validation": validation,
                "artifacts": _artifact_paths(package)}
    staging = output_root / f".{package.name}.staging"
    if staging.is_symlink():
        raise BackendError(f"Refusing symlink for resumable Spirula export staging: {staging}",
                           failure_kind="SFM_INVALID")
    _ensure_directory(staging)
    _ensure_directory(staging / "images")
    _ensure_directory(staging / "sparse")
    _ensure_directory(staging / "sparse" / "0")
    image_rows = []
    camera_by_name = {row["name"]: row for row in registered}
    from PIL import Image
    for name in sorted(names):
        safe = _safe_name(name)
        source = Path(source_by_name[name]["source_path"])
        if source.is_symlink() or not source.is_file() or images_dir not in source.resolve().parents:
            raise BackendError(f"SfM image is not a regular file beneath the configured input directory: {name}",
                               failure_kind="INVALID_INPUT")
        target = _staging_target(staging, "images", safe)
        _copy_verified(source, target, source_by_name[name]["sha256"])
        try:
            with Image.open(target) as image:
                width, height = image.size
                orientation = image.getexif().get(274, 1)
        except Exception as exc:
            raise BackendError(f"Could not inspect exported image {name}: {exc}", failure_kind="SFM_INVALID") from exc
        camera = camera_by_name[name]
        camera_size = [camera["width"], camera["height"]]
        if [width, height] != camera_size:
            raise BackendError(f"Exported image dimensions differ from COLMAP for {name}: {width}x{height} != {camera_size[0]}x{camera_size[1]}",
                               failure_kind="SFM_INVALID", details={"image": name, "encoded_dimensions": [width, height],
                                                                     "camera_dimensions": camera_size,
                                                                     "exif_orientation": orientation})
        image_rows.append({"relative_path": safe.as_posix(), "sha256": sha256_file(target),
                           "size_bytes": target.stat().st_size, "encoded_dimensions": [width, height],
                           "camera_dimensions": camera_size, "exif_orientation": int(orientation or 1),
                           "camera_id": camera["camera_id"]})
    sparse_export_hashes = {}
    for name in SPARSE_FILES:
        target = _staging_target(staging, "sparse/0", PurePosixPath(name))
        _copy_verified(sparse_dir / name, target, sparse_source_hashes[name])
        sparse_export_hashes[name] = {"relative_path": f"sparse/0/{name}", "sha256": sparse_source_hashes[name],
                                      "size_bytes": target.stat().st_size}
    manifest = {
        "schema_version": EXPORT_SCHEMA, "project_id": project_id,
        "dataset_format": "COLMAP_BINARY", "layout": "images/ + sparse/0/{cameras.bin,images.bin,points3D.bin}",
        "portability": "All image and sparse reconstruction references are relative within this folder.",
        "source_provenance": _manifest_metadata(source_provenance or {"source_photographs_modified": False},
                                                 image_root=images_dir, sparse_root=sparse_dir),
        "coordinate_system": "COLMAP SfM frame; arbitrary non-metric scale unless independently validated.",
        "camera_count": parsed["camera_count"], "registered_image_count": len(registered),
        "sparse_point_count": parsed["sparse_point_count"], "registered_image_names": sorted(names),
        "images": image_rows, "sparse_files": sparse_export_hashes,
        "compatibility_validation": {"structural_colmap_dataset": "PASS",
                                     "gui_reopen": "NOT_TESTED; perform the documented local Spirula Studio check"},
        "open_hint": "Open this dataset folder in Spirula Studio or run `spirula <dataset-folder>`. ",
    }
    manifest_path = staging / "manifest.json"
    if manifest_path.is_symlink():
        raise BackendError(f"Refusing symlink for staged Spirula manifest: {manifest_path}", failure_kind="SFM_INVALID")
    _unlink_staging_file(manifest_path.with_suffix(manifest_path.suffix + ".tmp"))
    write_json(manifest_path, manifest)
    readme_path = staging / "README.md"
    if readme_path.is_symlink():
        raise BackendError(f"Refusing symlink for staged Spirula README: {readme_path}", failure_kind="SFM_INVALID")
    readme_temp = readme_path.with_name(readme_path.name + ".writing")
    _unlink_staging_file(readme_temp)
    with readme_temp.open("w", encoding="utf-8") as stream:
        stream.write(
            "# Spirula Studio COLMAP dataset\n\n"
            "This package contains only registered SfM images and their unchanged binary COLMAP sparse model. "
            "Paths are relative and the package can be moved as a whole. Original source photographs were not modified.\n\n"
            "Open this dataset folder in Spirula Studio, or run `spirula <dataset-folder>`. "
            "The Engine has validated names, hashes, image dimensions, camera references, and sparse files; GUI reopening still requires a local manual check.\n")
    os.replace(readme_temp, readme_path)
    # The stable staging path allows a later attempt to reuse any intact images
    # or sparse files left by interruption, without rewriting them.
    validation = validate_studio_dataset(staging)
    os.replace(staging, package)
    validation["manifest_path"] = str(package / "manifest.json")
    return {"status": "EXPORTED_AND_STRUCTURALLY_VALIDATED", "package_directory": str(package),
            "manifest_path": str(package / "manifest.json"), "validation": validation,
            "artifacts": _artifact_paths(package)}


def _artifact_paths(package: Path) -> list[Path]:
    return sorted(path for path in package.rglob("*")
                  if path.is_file() and not path.is_symlink())
