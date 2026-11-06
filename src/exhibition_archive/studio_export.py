"""Portable COLMAP-folder export for reopening a completed SfM result."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from .adapters import sha256_file, write_json
from .backends import BackendError, _parse_colmap, image_inventory


EXPORT_SCHEMA = "exhibition-archive.spirula-studio-colmap-export.v1"
SPARSE_FILES = ("cameras.bin", "images.bin", "points3D.bin")


def _safe_name(raw: str) -> PurePosixPath:
    name = PurePosixPath(raw)
    if (not raw or "\\" in raw or name.is_absolute() or
            any(part in {"", ".", ".."} for part in name.parts)):
        raise BackendError(f"COLMAP image path is not portable: {raw!r}", failure_kind="SFM_INVALID")
    if name.as_posix() != raw:
        raise BackendError(f"COLMAP image path is not canonical: {raw!r}", failure_kind="SFM_INVALID")
    return name


def _dataset_fingerprint(source_images: Path, reconstruction: Path,
                         registered_names: list[str], image_rows: list[dict[str, Any]]) -> str:
    by_name = {row["relative_path"]: row for row in image_rows}
    document = {
        "schema": EXPORT_SCHEMA,
        "reconstruction": {name: sha256_file(reconstruction / name) for name in SPARSE_FILES},
        "images": [{"name": name, "sha256": by_name[name]["sha256"]} for name in sorted(registered_names)],
    }
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:20]


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
    key = _dataset_fingerprint(images_dir, sparse_dir, names, inventory["images"])
    output_root.mkdir(parents=True, exist_ok=True)
    package = output_root / f"revision-{key}"
    if package.exists():
        validation = validate_studio_dataset(package)
        return {"status": "REUSED_VALID_EXPORT", "package_directory": str(package),
                "manifest_path": str(package / "manifest.json"), "validation": validation,
                "artifacts": _artifact_paths(package)}
    staging = Path(tempfile.mkdtemp(prefix=f".{key}-", dir=output_root))
    try:
        (staging / "images").mkdir()
        (staging / "sparse" / "0").mkdir(parents=True)
        image_rows = []
        camera_by_name = {row["name"]: row for row in registered}
        from PIL import Image
        for name in sorted(names):
            safe = _safe_name(name)
            source = Path(source_by_name[name]["source_path"])
            if source.is_symlink() or not source.is_file() or images_dir not in source.resolve().parents:
                raise BackendError(f"SfM image is not a regular file beneath the configured input directory: {name}",
                                   failure_kind="INVALID_INPUT")
            target = staging / "images" / Path(*safe.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
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
        sparse_hashes = {}
        for name in SPARSE_FILES:
            target = staging / "sparse" / "0" / name
            shutil.copy2(sparse_dir / name, target)
            sparse_hashes[name] = {"relative_path": f"sparse/0/{name}", "sha256": sha256_file(target),
                                   "size_bytes": target.stat().st_size}
        manifest = {
            "schema_version": EXPORT_SCHEMA, "project_id": project_id,
            "dataset_format": "COLMAP_BINARY", "layout": "images/ + sparse/0/{cameras.bin,images.bin,points3D.bin}",
            "portability": "All image and sparse reconstruction references are relative within this folder.",
            "source_provenance": source_provenance or {"source_photographs_modified": False},
            "coordinate_system": "COLMAP SfM frame; arbitrary non-metric scale unless independently validated.",
            "camera_count": parsed["camera_count"], "registered_image_count": len(registered),
            "sparse_point_count": parsed["sparse_point_count"], "registered_image_names": sorted(names),
            "images": image_rows, "sparse_files": sparse_hashes,
            "compatibility_validation": {"structural_colmap_dataset": "PASS",
                                         "gui_reopen": "NOT_TESTED; perform the documented local Spirula Studio check"},
            "open_hint": "Run `spirula /absolute/path/to/this/folder` or open the folder through Spirula Studio's dataset picker.",
        }
        write_json(staging / "manifest.json", manifest)
        (staging / "README.md").write_text(
            "# Spirula Studio COLMAP dataset\n\n"
            "This package contains only registered SfM images and their unchanged binary COLMAP sparse model. "
            "Paths are relative and the package can be moved as a whole. Original source photographs were not modified.\n\n"
            "Open it locally with `spirula /absolute/path/to/this/folder`, or use the Spirula Studio GUI to open/import this dataset folder. "
            "The Engine has validated names, hashes, image dimensions, camera references, and sparse files; GUI reopening still requires a local manual check.\n",
            encoding="utf-8")
        os.replace(staging, package)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    validation = validate_studio_dataset(package)
    return {"status": "EXPORTED_AND_STRUCTURALLY_VALIDATED", "package_directory": str(package),
            "manifest_path": str(package / "manifest.json"), "validation": validation,
            "artifacts": _artifact_paths(package)}


def _artifact_paths(package: Path) -> list[Path]:
    return sorted(path for path in package.rglob("*")
                  if path.is_file() and not path.is_symlink())
