from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from .adapters import sha256_file, write_json


def materialize_image_only_dataset(root: Path, config: dict, source_inventory: dict[str, Any]) -> dict[str, Any] | None:
    """Copy image bytes to an image-only, provenance-bound dataset; originals remain untouched."""
    sfm_cfg = config.get("integrations", {}).get("sfm", {})
    image_directory = sfm_cfg.get("image_directory")
    manifest_value = sfm_cfg.get("derived_image_manifest")
    if not image_directory or not manifest_value:
        return None
    source_root = _project_path(root, config, config["source"]["photo_directory"])
    dataset = _project_path(root, config, image_directory)
    if dataset == source_root or source_root in dataset.parents:
        return None
    manifest_path = _project_path(root, config, manifest_value)
    try:
        dataset.relative_to(root.resolve())
        manifest_path.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("Normalized image dataset and provenance manifest must remain inside the project workspace") from exc
    rows = source_inventory.get("images", [])
    if not rows:
        raise ValueError("Cannot materialize an empty image-only dataset")
    expected_mapping = []
    for row in rows:
        relative = row["relative_path"]
        source = (source_root / relative).resolve()
        if source_root not in source.parents or not source.is_file():
            raise ValueError(f"Source image is missing or escapes the registered source: {relative}")
        actual_hash = sha256_file(source)
        if actual_hash != row["sha256"]:
            raise ValueError(f"Source image changed after inventory: {relative}")
        expected_mapping.append({"source_relative_path": relative, "derived_relative_path": relative,
                                 "source_sha256": actual_hash, "derived_sha256": actual_hash,
                                 "size_bytes": source.stat().st_size,
                                 "normalization": "BYTE_IDENTICAL_COPY; EXIF_ORIENTATION_AND_CAMERA_METADATA_PRESERVED"})
    expected = {item["derived_relative_path"] for item in expected_mapping}
    if dataset.exists():
        if not manifest_path.is_file():
            raise FileExistsError(f"Existing derived image dataset has no provenance manifest; refusing to overwrite it: {dataset}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        actual_files = {path.relative_to(dataset).as_posix() for path in dataset.rglob("*") if path.is_file()}
        actual_mapping = manifest.get("image_mappings", [])
        if (actual_files != expected or
                {item.get("derived_relative_path") for item in actual_mapping} != expected or
                actual_mapping != expected_mapping):
            raise ValueError("Existing derived image dataset or manifest is stale; the Engine will not replace it implicitly")
        for item in expected_mapping:
            derived = dataset / item["derived_relative_path"]
            if sha256_file(derived) != item["derived_sha256"]:
                raise ValueError(f"Derived image hash mismatch: {item['derived_relative_path']}")
        return {"status": "REUSED_VALID_DERIVED_DATASET", "dataset_directory": str(dataset),
                "manifest_path": str(manifest_path), "image_count": len(expected_mapping),
                "source_photographs_modified": False}
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        if previous.get("image_mappings") != expected_mapping:
            raise ValueError("An existing normalized dataset manifest describes different input images; choose a new project output")
    dataset.parent.mkdir(parents=True, exist_ok=True)
    staging = dataset.with_name(dataset.name + ".staging")
    if staging.exists():
        raise FileExistsError(f"An interrupted derived-image staging directory already exists; inspect or move it before resuming: {staging}")
    staging.mkdir(parents=True, exist_ok=False)
    try:
        for item in expected_mapping:
            source = source_root / item["source_relative_path"]
            target = staging / item["derived_relative_path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if sha256_file(target) != item["derived_sha256"]:
                raise ValueError(f"Byte-identical image copy failed validation: {item['derived_relative_path']}")
        manifest = {"schema_version": "exhibition-archive.normalized-image-dataset.v1",
                    "project_id": config["project_id"], "source_directory": str(source_root),
                    "effective_image_directory": str(dataset), "source_photographs_modified": False,
                    "image_mappings": expected_mapping,
                    "normalization": "Image-only directory; source bytes and EXIF orientation/camera metadata are preserved exactly."}
        write_json(manifest_path, manifest)
        dataset.parent.mkdir(parents=True, exist_ok=True)
        staging.replace(dataset)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {"status": "CREATED", "dataset_directory": str(dataset),
            "manifest_path": str(manifest_path), "image_count": len(expected_mapping),
            "source_photographs_modified": False,
            "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest()}


def _project_path(root: Path, config: dict, value: str | Path) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else root / path).resolve()
