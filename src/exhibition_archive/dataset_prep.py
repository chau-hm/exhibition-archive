from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from io import BytesIO
from pathlib import Path
from typing import Any

from .adapters import sha256_file, write_json
from .backends import BackendError, IMAGE_SUFFIXES


HEIF_SUFFIXES = {".heic", ".heif"}
JPEG_SUFFIXES = {".jpg", ".jpeg"}
JPEG_QUALITY = 95
PREP_DIR = Path("derived/spirula-jpeg")
PREP_MANIFEST = Path("derived/sfm-dataset-preparation.json")
DATASET_MANIFEST = ".exhibition-archive-dataset.json"
PREP_SCHEMA = "exhibition-archive.spirula-image-dataset.v2"
CONVERSION_VALIDATION_POLICY = "libheif-pixel-content-exif-icc-v1"
PIXEL_SAMPLE_MAX_SIDE = 512
PREP_SETTINGS = {
    "jpeg_quality": JPEG_QUALITY,
    "orientation": "bake-EXIF-transform-into-pixels-and-write-orientation-1",
    "resize": "none",
    "crop": "none",
    "lens_correction": "none",
    "color": "preserve-libheif-decoded-source-ICC-profile-without-gamut-relabel",
    "validation_policy": CONVERSION_VALIDATION_POLICY,
    "pixel_sample_max_side": PIXEL_SAMPLE_MAX_SIDE,
}


class DatasetPreparationRequired(BackendError):
    def __init__(self, message: str):
        super().__init__(message, failure_kind="DATASET_PREPARATION_REQUIRED")


def _project_path(root: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else root / path).resolve()


def raw_sfm_image_directory(root: Path, config: dict[str, Any]) -> Path:
    sfm = config.get("integrations", {}).get("sfm", {})
    return _project_path(root, sfm.get("image_directory", config["source"]["photo_directory"]))


def preparation_manifest_path(root: Path) -> Path:
    return root / PREP_MANIFEST


def _supported_images(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.rglob("*")
                  if path.is_file() and path.suffix.casefold() in IMAGE_SUFFIXES)


def contains_heif(directory: Path) -> bool:
    return any(path.suffix.casefold() in HEIF_SUFFIXES for path in _supported_images(directory))


def auto_preparation_required(root: Path, config: dict[str, Any]) -> bool:
    sfm = config.get("integrations", {}).get("sfm", {})
    if sfm.get("mode", "adopt_existing") != "spirula_fresh":
        return False
    raw = raw_sfm_image_directory(root, config)
    return contains_heif(raw)


def _pillow_modules():
    try:
        from PIL import Image, ImageChops, ImageCms, ImageStat, __version__
    except ImportError as exc:
        raise BackendError(
            "HEIC preparation requires Pillow for decoded-pixel, EXIF, and ICC validation; install the exhibition-archive package dependencies",
            failure_kind="CONVERTER_UNAVAILABLE",
        ) from exc
    return Image, ImageChops, ImageCms, ImageStat, __version__


def heif_converter_identity() -> dict[str, Any]:
    executable = shutil.which("heif-convert")
    if not executable:
        raise BackendError(
            "HEIC preparation requires heif-convert from libheif; on macOS install it with `brew install libheif`, then confirm `heif-convert --version` works",
            failure_kind="CONVERTER_UNAVAILABLE",
        )
    try:
        version_output = subprocess.run([executable, "--version"], check=True, text=True,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        timeout=15).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise BackendError(f"Could not identify the heif-convert/libheif converter: {exc}",
                           failure_kind="CONVERTER_UNAVAILABLE") from exc
    if not version_output:
        raise BackendError("heif-convert returned no version information", failure_kind="CONVERTER_UNAVAILABLE")
    _, _, _, _, pillow_version = _pillow_modules()
    return {
        "name": "libheif heif-convert",
        "executable": str(Path(executable).resolve()),
        "version": version_output,
        "pixel_validator": {"name": "Pillow", "version": pillow_version},
    }


def _json_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part-" + uuid.uuid4().hex)
    try:
        write_json(temporary, value)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _source_records(root: Path, config: dict[str, Any], input_dir: Path,
                    source_inventory: dict[str, Any]) -> list[dict[str, Any]]:
    source_dir = _project_path(root, config["source"]["photo_directory"])
    selected = input_dir.resolve()
    if selected != source_dir and source_dir not in selected.parents:
        if contains_heif(selected):
            raise BackendError(
                "HEIC files selected for fresh SfM are outside the registered source inventory; register the photo directory or use the existing derived_image_manifest contract",
                failure_kind="INVALID_INPUT",
            )
        return []

    rows = source_inventory.get("images", [])
    records: list[dict[str, Any]] = []
    seen_paths: set[Path] = set()
    for row in rows:
        relative = str(row.get("relative_path", ""))
        if not relative:
            continue
        registered = (source_dir / relative).resolve()
        if source_dir not in registered.parents or not registered.is_file():
            raise BackendError(f"Registered source inventory path is missing or escapes its root: {relative}",
                               failure_kind="INVALID_INPUT")
        if registered != selected and selected not in registered.parents:
            continue
        local_relative = registered.relative_to(selected).as_posix()
        if Path(local_relative).suffix.casefold() not in IMAGE_SUFFIXES:
            continue
        digest = sha256_file(registered)
        recorded = row.get("sha256")
        if recorded and digest != recorded:
            raise BackendError(
                f"Source bytes no longer match the registered photo inventory for {relative}; run the workflow with --full-hash to refresh inventory evidence",
                failure_kind="STALE_SOURCE_INVENTORY",
                details={"source_relative_path": relative, "inventory_sha256": recorded,
                         "current_sha256": digest},
            )
        if registered in seen_paths:
            raise BackendError(f"Duplicate resolved source image in inventory: {relative}", failure_kind="INVALID_INPUT")
        seen_paths.add(registered)
        records.append({
            "source_relative_path": relative,
            "input_relative_path": local_relative,
            "source_path": str(registered),
            "source_sha256": digest,
            "source_size_bytes": registered.stat().st_size,
            "source_format": registered.suffix.casefold().lstrip("."),
        })

    actual = _supported_images(selected)
    actual_paths = {path.resolve() for path in actual}
    if actual_paths != seen_paths:
        raise BackendError(
            "Selected input membership differs from the registered source inventory; refresh source inventory before preparing HEIC",
            failure_kind="STALE_SOURCE_INVENTORY",
            details={"inventory_only": sorted(str(p) for p in seen_paths - actual_paths),
                     "filesystem_only": sorted(str(p) for p in actual_paths - seen_paths)},
        )
    records.sort(key=lambda row: row["input_relative_path"])
    return records


def _mapped_output_names(records: list[dict[str, Any]]) -> dict[str, str]:
    candidates: dict[str, str] = {}
    for row in records:
        source_rel = Path(row["input_relative_path"])
        suffix = source_rel.suffix.casefold()
        if suffix in HEIF_SUFFIXES:
            suffix = ".jpg"
        candidates[row["input_relative_path"]] = source_rel.with_suffix(suffix).as_posix()

    basename_counts: dict[str, int] = {}
    for output_rel in candidates.values():
        key = Path(output_rel).name.casefold()
        basename_counts[key] = basename_counts.get(key, 0) + 1

    resolved: dict[str, str] = {}
    used: set[str] = set()
    for row in records:
        source_rel = row["input_relative_path"]
        output_rel = Path(candidates[source_rel])
        if basename_counts[output_rel.name.casefold()] > 1:
            stem = output_rel.stem
            suffix = output_rel.suffix
            token = hashlib.sha256(source_rel.encode("utf-8")).hexdigest()
            width = 10
            while True:
                name = f"{stem}__{token[:width]}{suffix}"
                candidate = output_rel.with_name(name).as_posix()
                if candidate.casefold() not in used and name.casefold() not in {Path(v).name.casefold() for v in candidates.values() if basename_counts[Path(v).name.casefold()] == 1}:
                    break
                width += 2
            output_rel = Path(candidate)
        final_rel = output_rel.as_posix()
        if final_rel.casefold() in used:
            raise BackendError("Collision-safe image naming did not produce unique output paths", failure_kind="INVALID_INPUT")
        used.add(final_rel.casefold())
        resolved[source_rel] = final_rel
    return resolved


_EXIF_FIELDS = (
    ("camera_make", "root", 271), ("camera_model", "root", 272),
    ("lens_make", "exif", 42035), ("lens_model", "exif", 42036),
    ("focal_length", "exif", 37386), ("focal_length_35mm", "exif", 41989),
    ("f_number", "exif", 33437), ("exposure_time", "exif", 33434),
    ("iso_speed_ratings", "exif", 34855),
    ("focal_plane_x_resolution", "exif", 41486),
    ("focal_plane_y_resolution", "exif", 41487),
    ("focal_plane_resolution_unit", "exif", 41488),
    ("date_time_original", "exif", 36867), ("date_time_digitized", "exif", 36868),
    ("gps_latitude", "gps", 2), ("gps_latitude_ref", "gps", 1),
    ("gps_longitude", "gps", 4), ("gps_longitude_ref", "gps", 3),
    ("gps_altitude", "gps", 6), ("gps_altitude_ref", "gps", 5),
    ("gps_date_stamp", "gps", 29), ("gps_time_stamp", "gps", 7),
)


def _exif_group(exif: Any, group: str) -> dict:
    if group == "root":
        return exif
    return exif.get_ifd(34665 if group == "exif" else 34853)


def _canonical_exif(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"bytes": value.hex()}
    if isinstance(value, dict):
        return {str(key): _canonical_exif(item) for key, item in sorted(value.items(), key=lambda row: str(row[0]))}
    if isinstance(value, (tuple, list)):
        return [_canonical_exif(item) for item in value]
    if hasattr(value, "numerator") and hasattr(value, "denominator"):
        return [int(value.numerator), int(value.denominator)]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _load_exif_sidecar(path: Path):
    Image, _, _, _, _ = _pillow_modules()
    data = path.read_bytes()
    offsets = [0]
    if len(data) >= 4:
        offsets.append(4 + int.from_bytes(data[:4], "big"))
    for offset in dict.fromkeys(offsets):
        if data[offset:offset + 2] not in {b"II", b"MM"}:
            continue
        exif = Image.Exif()
        try:
            exif.load(data[offset:])
            return exif
        except (OSError, SyntaxError, ValueError):
            continue
    raise BackendError("heif-convert wrote an EXIF sidecar that Pillow could not parse", failure_kind="CONVERSION_FAILED")


def _profile_description(icc_profile: bytes | None) -> str | None:
    if not icc_profile:
        return None
    _, _, ImageCms, _, _ = _pillow_modules()
    try:
        description = ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(BytesIO(icc_profile))).strip()
    except Exception as exc:
        raise BackendError(f"Decoded JPEG contains an unreadable ICC profile: {exc}", failure_kind="CONVERSION_FAILED") from exc
    return description or "ICC profile with no description"


def _luminance_percentile(image: Any, percentile: float = 0.99) -> int:
    histogram = image.convert("L").histogram()
    threshold = max(1, int(image.width * image.height * percentile))
    cumulative = 0
    for value, count in enumerate(histogram):
        cumulative += count
        if cumulative >= threshold:
            return value
    return 255


def _decoded_pixel_evidence(reference_path: Path, jpeg_path: Path) -> dict[str, Any]:
    Image, ImageChops, _, ImageStat, _ = _pillow_modules()
    try:
        with Image.open(reference_path) as reference:
            reference.load()
            reference_size = reference.size
            reference_sample = reference.convert("RGB")
            reference_sample.thumbnail((PIXEL_SAMPLE_MAX_SIDE, PIXEL_SAMPLE_MAX_SIDE), Image.Resampling.BOX)
        with Image.open(jpeg_path) as converted:
            converted.load()
            converted_size = converted.size
            converted_sample = converted.convert("RGB")
            converted_sample.thumbnail((PIXEL_SAMPLE_MAX_SIDE, PIXEL_SAMPLE_MAX_SIDE), Image.Resampling.BOX)
    except Exception as exc:
        raise BackendError(f"Could not decode the libheif pixel reference or converted JPEG: {exc}",
                           failure_kind="CONVERSION_FAILED") from exc
    if reference_size != converted_size or reference_sample.size != converted_sample.size:
        raise BackendError("Converted JPEG dimensions differ from the normalized libheif reference",
                           failure_kind="CONVERSION_FAILED",
                           details={"reference_size": reference_size, "output_size": converted_size})

    difference = ImageChops.difference(reference_sample, converted_sample)
    mean_error = sum(ImageStat.Stat(difference).mean) / 3.0
    source_luminance_p99 = _luminance_percentile(reference_sample)
    output_luminance_p99 = _luminance_percentile(converted_sample)
    # Compare with the source instead of rejecting images by their absolute
    # brightness. A genuinely dark source remains valid when the JPEG matches it.
    looks_black = source_luminance_p99 >= 12 and output_luminance_p99 <= max(2, source_luminance_p99 * 0.2)
    if looks_black or mean_error > 24:
        raise BackendError(
            "Converted JPEG pixel content does not match the decoded HEIF source; a black or otherwise corrupted derivative was discarded",
            failure_kind="CONVERSION_FAILED",
            details={"mean_absolute_channel_error": round(mean_error, 3),
                     "source_luminance_p99": source_luminance_p99,
                     "output_luminance_p99": output_luminance_p99,
                     "near_black_output": looks_black},
        )
    return {
        "status": "PASS",
        "method": "libheif PNG decode compared with decoded JPEG pixels",
        "sample_max_side": PIXEL_SAMPLE_MAX_SIDE,
        "mean_absolute_channel_error": round(mean_error, 3),
        "source_luminance_p99": source_luminance_p99,
        "output_luminance_p99": output_luminance_p99,
    }


def _validate_heif_outputs(reference_path: Path, jpeg_path: Path, exif_path: Path) -> dict[str, Any]:
    Image, _, _, _, _ = _pillow_modules()
    source_exif = _load_exif_sidecar(exif_path)
    try:
        with Image.open(reference_path) as reference, Image.open(jpeg_path) as converted:
            if reference.format != "PNG" or converted.format != "JPEG":
                raise BackendError("libheif reference or conversion output has an unexpected image format",
                                   failure_kind="CONVERSION_FAILED")
            reference_size = reference.size
            output_size = converted.size
            if reference_size != output_size:
                raise BackendError("JPEG dimensions do not match the orientation-normalized HEIF source",
                                   failure_kind="CONVERSION_FAILED",
                                   details={"source_size": reference_size, "output_size": output_size})
            output_exif = converted.getexif()
            source_icc = reference.info.get("icc_profile")
            output_icc = converted.info.get("icc_profile")
            if source_icc is not None and not isinstance(source_icc, bytes):
                raise BackendError("libheif reference contains invalid ICC profile data", failure_kind="CONVERSION_FAILED")
            if output_icc is not None and not isinstance(output_icc, bytes):
                raise BackendError("Converted JPEG contains invalid ICC profile data", failure_kind="CONVERSION_FAILED")
            source_icc = source_icc or None
            output_icc = output_icc or None
            if source_icc and output_icc != source_icc:
                raise BackendError("Converted JPEG ICC profile bytes differ from the libheif-decoded HEIF source",
                                   failure_kind="CONVERSION_FAILED",
                                   details={"source_icc_profile_byte_count": len(source_icc),
                                            "output_icc_profile_byte_count": len(output_icc or b"")})

            source_orientation = int(source_exif.get(274, 1))
            output_orientation = int(output_exif.get(274, 1))
            output_tiff_orientation = int(output_exif.get(274, 1))
            if source_orientation not in range(1, 9) or output_orientation != 1:
                raise BackendError("JPEG orientation was not normalized to EXIF orientation 1",
                                   failure_kind="CONVERSION_FAILED",
                                   details={"source_orientation": source_orientation,
                                            "output_orientation": output_orientation})

            source_fields: list[str] = []
            retained_fields: list[str] = []
            missing_fields: list[str] = []
            for name, group, tag in _EXIF_FIELDS:
                source_group = _exif_group(source_exif, group)
                if tag not in source_group:
                    continue
                source_fields.append(name)
                output_group = _exif_group(output_exif, group)
                if tag in output_group and _canonical_exif(output_group[tag]) == _canonical_exif(source_group[tag]):
                    retained_fields.append(name)
                else:
                    missing_fields.append(name)
            missing_essential = []
            if not source_exif.get(271):
                missing_essential.append("camera_make")
            if not source_exif.get(272):
                missing_essential.append("camera_model")
            source_exif_ifd = _exif_group(source_exif, "exif")
            if source_exif_ifd.get(37386) is None and source_exif_ifd.get(41989) is None:
                missing_essential.append("focal_length")
            if missing_essential:
                raise BackendError("HEIF source lacks essential camera make/model/focal metadata for SfM",
                                   failure_kind="CONVERSION_FAILED",
                                   details={"missing_source_essential_fields": missing_essential})
            if missing_fields:
                raise BackendError("JPEG did not retain source EXIF fields: " + ", ".join(missing_fields),
                                   failure_kind="CONVERSION_FAILED",
                                   details={"missing_or_changed_metadata_fields": missing_fields})

            source_profile = _profile_description(source_icc)
            output_profile = _profile_description(output_icc)
            pixel_evidence = _decoded_pixel_evidence(reference_path, jpeg_path)
    except BackendError:
        raise
    except Exception as exc:
        raise BackendError(f"Could not validate converted JPEG metadata or ICC profile: {exc}",
                           failure_kind="CONVERSION_FAILED") from exc

    return {
        "source_width": reference_size[0], "source_height": reference_size[1],
        "width": output_size[0], "height": output_size[1],
        "source_orientation": source_orientation,
        "output_orientation": output_orientation,
        "output_tiff_orientation": output_tiff_orientation,
        "jpeg_quality": JPEG_QUALITY,
        "validation_policy": CONVERSION_VALIDATION_POLICY,
        "pixel_content_validation": pixel_evidence,
        "source_metadata_fields": source_fields,
        "retained_metadata_fields": retained_fields,
        "missing_source_essential_fields": missing_essential,
        "metadata_validation_status": "PRESERVED_SOURCE_FIELDS" if source_fields else "NO_RELEVANT_SOURCE_METADATA",
        "source_color_profile": source_profile,
        "output_color_profile": output_profile,
        "source_icc_profile_byte_count": len(source_icc) if source_icc else 0,
        "output_icc_profile_byte_count": len(output_icc) if output_icc else 0,
        "source_icc_profile_sha256": hashlib.sha256(source_icc).hexdigest() if source_icc else None,
        "output_icc_profile_sha256": hashlib.sha256(output_icc).hexdigest() if output_icc else None,
        "icc_profile_validation_status": "ICC_PROFILE_BYTES_PRESERVED" if source_icc else "SOURCE_ICC_PROFILE_ABSENT",
        "color_profile_validation_status": "ICC_PROFILE_BYTES_MATCHED" if source_icc else "OUTPUT_ICC_PROFILE_INSPECTED",
        "color_handling": "heif-convert/libheif output ICC bytes inspected with Pillow; source ICC bytes compared when present; no gamut relabel or color conversion",
    }


def _run_heif_command(executable: str, arguments: list[str], source: str,
                      deadline: float, timeout_seconds: int) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise BackendError(
            f"heif-convert exceeded the configured {timeout_seconds}-second dataset-preparation limit; incomplete outputs were discarded",
            failure_kind="RESOURCE_LIMIT",
        )
    try:
        completed = subprocess.run([executable, *arguments], check=False, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   timeout=remaining)
    except subprocess.TimeoutExpired as exc:
        raise BackendError(
            f"heif-convert exceeded the configured {timeout_seconds}-second dataset-preparation limit; incomplete outputs were discarded",
            failure_kind="RESOURCE_LIMIT",
        ) from exc
    except OSError as exc:
        raise BackendError(f"Could not launch heif-convert: {exc}", failure_kind="CONVERTER_UNAVAILABLE") from exc
    if completed.returncode != 0:
        raise BackendError(
            f"heif-convert failed while processing {Path(source).name}; incomplete outputs were discarded",
            failure_kind="CONVERSION_FAILED",
            details={"exit_code": completed.returncode,
                     "stdout": completed.stdout[-2000:], "stderr": completed.stderr[-4000:]},
        )


def _run_heif_convert_batch(requests: list[dict[str, str]], converter: dict[str, Any],
                            timeout_seconds: int) -> dict[str, dict[str, Any]]:
    if not requests:
        return {}
    deadline = time.monotonic() + timeout_seconds
    results: dict[str, dict[str, Any]] = {}
    # The JPEG and libheif pixel reference, including heif-convert's .exif
    # sidecar, are all kept in this OS temporary directory, outside the final
    # SfM image directory.
    with tempfile.TemporaryDirectory(prefix="exhibition-archive-heif-") as temp_name:
        temp = Path(temp_name)
        for index, request in enumerate(requests):
            source = str(request["source"])
            output = Path(request["output"])
            converted = temp / f"converted-{index:05d}.jpg"
            reference = temp / f"reference-{index:05d}.png"
            exif_sidecar = temp / f"converted-{index:05d}.exif"
            _run_heif_command(
                converter["executable"],
                ["--with-exif", "-q", str(JPEG_QUALITY), source, str(converted)],
                source, deadline, timeout_seconds,
            )
            if not converted.is_file() or not exif_sidecar.is_file():
                raise BackendError(
                    f"heif-convert did not produce a JPEG and EXIF sidecar for {Path(source).name}",
                    failure_kind="CONVERSION_FAILED",
                )
            _run_heif_command(converter["executable"], [source, str(reference)],
                              source, deadline, timeout_seconds)
            if not reference.is_file():
                raise BackendError(f"heif-convert did not produce a decoded pixel reference for {Path(source).name}",
                                   failure_kind="CONVERSION_FAILED")
            details = _validate_heif_outputs(reference, converted, exif_sidecar)
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(converted, output)
            results[source] = details
    return results


def _jpeg_dimensions(path: Path) -> tuple[int | None, int | None]:
    try:
        with path.open("rb") as stream:
            if stream.read(2) != b"\xff\xd8":
                return None, None
            while True:
                marker_start = stream.read(1)
                if not marker_start:
                    return None, None
                if marker_start != b"\xff":
                    continue
                marker = stream.read(1)
                while marker == b"\xff":
                    marker = stream.read(1)
                if not marker or marker in (b"\xd8", b"\xd9"):
                    continue
                size_data = stream.read(2)
                if len(size_data) != 2:
                    return None, None
                size = int.from_bytes(size_data, "big")
                if marker[0] in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                    data = stream.read(5)
                    if len(data) != 5:
                        return None, None
                    return int.from_bytes(data[3:5], "big"), int.from_bytes(data[1:3], "big")
                stream.seek(size - 2, 1)
    except (OSError, ValueError):
        return None, None


def _dataset_file_membership(dataset: Path) -> set[str] | None:
    files: set[str] = set()
    for path in dataset.rglob("*"):
        if path.is_symlink():
            return None
        if path.is_file():
            relative = path.relative_to(dataset).as_posix().casefold()
            if relative != DATASET_MANIFEST.casefold():
                files.add(relative)
    return files


def _conversion_validation_current(details: Any) -> bool:
    if not isinstance(details, dict):
        return False
    pixel = details.get("pixel_content_validation")
    icc_status = details.get("icc_profile_validation_status")
    return (
        details.get("validation_policy") == CONVERSION_VALIDATION_POLICY and
        isinstance(pixel, dict) and pixel.get("status") == "PASS" and
        str(pixel.get("method", "")).startswith("libheif PNG decode") and
        details.get("output_orientation") == 1 and
        details.get("jpeg_quality") == JPEG_QUALITY and
        details.get("metadata_validation_status") == "PRESERVED_SOURCE_FIELDS" and
        icc_status in {"ICC_PROFILE_BYTES_PRESERVED", "SOURCE_ICC_PROFILE_ABSENT"} and
        (not details.get("output_icc_profile_byte_count") or
         isinstance(details.get("output_icc_profile_sha256"), str))
    )


def _prepared_dataset_valid(dataset: Path, preparation_id: str,
                            source_records: list[dict[str, Any]],
                            preparation_basis: dict[str, Any]) -> dict[str, Any] | None:
    manifest_path = dataset / DATASET_MANIFEST
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (manifest.get("schema_version") != PREP_SCHEMA or manifest.get("status") != "PREPARED" or
            manifest.get("preparation_fingerprint") != preparation_id or
            manifest.get("preparation_basis") != preparation_basis or
            _json_digest(preparation_basis) != preparation_id or
            manifest.get("preparation_settings") != PREP_SETTINGS or
            manifest.get("source_photographs_modified") is not False):
        return None
    mappings = manifest.get("image_mappings")
    if not isinstance(mappings, list) or len(mappings) != len(source_records):
        return None
    expected_sources = {row["source_relative_path"]: row for row in source_records}
    expected_outputs = _mapped_output_names(source_records)
    records_by_input = {row["input_relative_path"]: row for row in source_records}
    seen_source: set[str] = set()
    seen_output: set[str] = set()
    for mapping in mappings:
        source_rel = mapping.get("source_relative_path")
        output_rel = mapping.get("derived_relative_path")
        if source_rel not in expected_sources or not isinstance(output_rel, str):
            return None
        if source_rel in seen_source or output_rel.casefold() in seen_output:
            return None
        seen_source.add(source_rel)
        seen_output.add(output_rel.casefold())
        src = expected_sources[source_rel]
        expected = records_by_input.get(str(mapping.get("input_relative_path")))
        if (not expected or expected["source_relative_path"] != source_rel or
                expected_outputs.get(expected["input_relative_path"]) != output_rel):
            return None
        target = (dataset / output_rel).resolve()
        if dataset.resolve() not in target.parents or not target.is_file() or target.is_symlink():
            return None
        if (src["source_sha256"] != mapping.get("source_sha256") or
                src["source_format"] != mapping.get("source_format") or
                sha256_file(target) != mapping.get("derived_sha256")):
            return None
        if src["source_size_bytes"] != mapping.get("source_size_bytes"):
            return None
        if src["source_format"] in {x.lstrip(".") for x in HEIF_SUFFIXES}:
            details = mapping.get("conversion_validation", {})
            if (target.suffix.casefold() not in JPEG_SUFFIXES or target.stat().st_size <= 0 or
                    _jpeg_dimensions(target) != (mapping.get("width"), mapping.get("height")) or
                    not _conversion_validation_current(details)):
                return None
        elif mapping.get("derived_sha256") != mapping.get("source_sha256"):
            return None
    if seen_source != set(expected_sources):
        return None
    actual = {p.relative_to(dataset).as_posix().casefold() for p in _supported_images(dataset)}
    if actual != seen_output or _dataset_file_membership(dataset) != seen_output:
        return None
    return manifest


def _manifest_is_current(root: Path, config: dict[str, Any], manifest: dict[str, Any],
                          *, validate_files: bool = True) -> tuple[Path, dict[str, Any]]:
    source_dir = _project_path(root, config["source"]["photo_directory"])
    input_dir = raw_sfm_image_directory(root, config)
    if (manifest.get("schema_version") != PREP_SCHEMA or manifest.get("status") != "PREPARED" or
            manifest.get("project_id") != config.get("project_id") or
            manifest.get("source_photographs_modified") is not False):
        raise DatasetPreparationRequired("The current registered HEIC input has no completed Engine-managed JPEG preparation; run the safe intake workflow before requesting an SfM plan")
    dataset_raw = manifest.get("effective_image_directory")
    if not isinstance(dataset_raw, str):
        raise BackendError("Prepared dataset manifest has no effective_image_directory", failure_kind="INVALID_INPUT")
    dataset = Path(dataset_raw).resolve()
    managed_root = (root / PREP_DIR).resolve()
    if dataset.parent != managed_root or dataset.name != manifest.get("preparation_fingerprint") or not dataset.is_dir():
        raise BackendError("Prepared image directory is missing or outside the Engine-managed workspace", failure_kind="UNSAFE_OUTPUT")
    try:
        local_manifest = json.loads((dataset / DATASET_MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackendError("Prepared dataset-local manifest is missing or unreadable", failure_kind="STALE_DATASET_PREPARATION") from exc
    if local_manifest != manifest:
        raise BackendError("Workspace and dataset-local preparation manifests do not match", failure_kind="STALE_DATASET_PREPARATION")
    if Path(manifest.get("source_directory", "")).resolve() != input_dir:
        raise BackendError("Prepared dataset refers to a different configured SfM source directory", failure_kind="INVALID_INPUT")

    source_inventory_path = _project_path(root, config.get("output_directory", "output")) / "work" / "source-index.json"
    try:
        source_inventory = json.loads(source_inventory_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DatasetPreparationRequired("Source inventory is missing; run the safe intake workflow before planning SfM") from exc
    inventory = {str(row.get("relative_path")): row for row in source_inventory.get("images", [])
                 if row.get("relative_path")}
    expected_records = _source_records(root, config, input_dir, source_inventory)
    expected_by_source = {row["source_relative_path"]: row for row in expected_records}
    expected_outputs = _mapped_output_names(expected_records)
    mappings = manifest.get("image_mappings", [])
    if not isinstance(mappings, list) or not mappings or len(mappings) != len(expected_records):
        raise BackendError("Prepared dataset manifest contains no source-to-derivative mappings", failure_kind="INVALID_INPUT")
    source_root = source_dir.resolve()
    seen_source: set[str] = set()
    seen_output: set[str] = set()
    for mapping in mappings:
        source_rel = mapping.get("source_relative_path")
        output_rel = mapping.get("derived_relative_path")
        if not isinstance(source_rel, str) or not isinstance(output_rel, str) or source_rel in seen_source or output_rel.casefold() in seen_output:
            raise BackendError("Prepared dataset mapping has missing or duplicate paths", failure_kind="INVALID_INPUT")
        seen_source.add(source_rel)
        seen_output.add(output_rel.casefold())
        source = (source_root / source_rel).resolve()
        target = (dataset / output_rel).resolve()
        if source_root not in source.parents or dataset not in target.parents or not source.is_file() or not target.is_file() or target.is_symlink():
            raise BackendError("Prepared dataset mapping escapes its source or managed output directory", failure_kind="UNSAFE_OUTPUT")
        current_source_hash = sha256_file(source)
        current_row = inventory.get(source_rel)
        if (current_source_hash != mapping.get("source_sha256") or not current_row or
                current_row.get("sha256") != current_source_hash):
            raise BackendError(f"Registered source image changed after dataset preparation: {source_rel}", failure_kind="STALE_DATASET_PREPARATION")
        expected_source = expected_by_source.get(source_rel)
        if (not expected_source or mapping.get("input_relative_path") != expected_source["input_relative_path"] or
                output_rel != expected_outputs.get(expected_source["input_relative_path"]) or
                mapping.get("source_size_bytes") != expected_source["source_size_bytes"] or
                mapping.get("source_format") != expected_source["source_format"]):
            raise BackendError(f"Prepared mapping no longer matches the registered source inventory: {source_rel}", failure_kind="STALE_DATASET_PREPARATION")
        if validate_files and sha256_file(target) != mapping.get("derived_sha256"):
            raise BackendError(f"Prepared JPEG/image changed after verification: {output_rel}", failure_kind="STALE_DATASET_PREPARATION")
        if Path(source_rel).suffix.casefold() in HEIF_SUFFIXES:
            details = mapping.get("conversion_validation", {})
            if Path(output_rel).suffix.casefold() not in JPEG_SUFFIXES or not _conversion_validation_current(details):
                raise BackendError(f"Prepared HEIC mapping lacks current pixel/EXIF/ICC verification evidence: {source_rel}", failure_kind="INVALID_INPUT")
    if seen_source != set(expected_by_source):
        raise BackendError("Prepared source mapping does not cover the current registered image dataset", failure_kind="STALE_DATASET_PREPARATION")
    if manifest.get("preparation_settings") != PREP_SETTINGS:
        raise BackendError("Prepared dataset conversion settings differ from the current Engine requirements", failure_kind="STALE_DATASET_PREPARATION")
    basis = manifest.get("preparation_basis")
    if not isinstance(basis, dict) or _json_digest(basis) != manifest.get("preparation_fingerprint"):
        raise BackendError("Prepared dataset fingerprint does not match its recorded conversion inputs", failure_kind="STALE_DATASET_PREPARATION")
    current_converter = heif_converter_identity()
    expected_basis = {
        "schema_version": PREP_SCHEMA,
        "source_directory": str(input_dir.resolve()),
        "registered_source_directory": str(source_dir.resolve()),
        "sources": [{"source_relative_path": row["source_relative_path"],
                     "source_sha256": row["source_sha256"],
                     "input_relative_path": row["input_relative_path"],
                     "derived_relative_path": expected_outputs[row["input_relative_path"]]}
                    for row in expected_records],
        "settings": PREP_SETTINGS,
        "converter": current_converter,
    }
    if basis != expected_basis or basis.get("converter") != current_converter or basis.get("settings") != PREP_SETTINGS:
        raise BackendError("heif-convert/libheif identity or JPEG preparation settings changed; rerun safe dataset preparation", failure_kind="STALE_DATASET_PREPARATION")
    conversion = manifest.get("conversion", {})
    if conversion.get("backend") != current_converter or conversion.get("settings") != PREP_SETTINGS or manifest.get("image_count") != len(expected_records):
        raise BackendError("Prepared manifest conversion provenance is incomplete or stale", failure_kind="STALE_DATASET_PREPARATION")
    actual_outputs = {path.relative_to(dataset).as_posix().casefold() for path in _supported_images(dataset)}
    if actual_outputs != seen_output or _dataset_file_membership(dataset) != seen_output:
        raise BackendError("Prepared dataset contains unexpected or missing files relative to its verified image mapping", failure_kind="STALE_DATASET_PREPARATION")
    if not contains_heif(input_dir):
        raise BackendError("Prepared dataset no longer corresponds to a HEIC input set", failure_kind="STALE_DATASET_PREPARATION")
    return dataset, manifest


def effective_sfm_dataset(root: Path, config: dict[str, Any], *,
                          validate_files: bool = True) -> tuple[Path, dict[str, Any] | None]:
    """Return the effective fresh-SfM input and its verified Engine provenance."""
    raw = raw_sfm_image_directory(root, config)
    sfm = config.get("integrations", {}).get("sfm", {})
    if sfm.get("mode", "adopt_existing") != "spirula_fresh" or not contains_heif(raw):
        return raw, None
    source = _project_path(root, config["source"]["photo_directory"])
    if raw != source and source not in raw.parents:
        raise BackendError("Fresh SfM HEIC inputs must come from the registered source inventory", failure_kind="INVALID_INPUT")
    path = preparation_manifest_path(root)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise DatasetPreparationRequired("HEIC sources require a verified JPEG dataset. Run the safe intake workflow before planning or authorizing SfM.")
    dataset, manifest = _manifest_is_current(root, config, manifest, validate_files=validate_files)
    return dataset, manifest


def effective_training_dataset(root: Path, config: dict[str, Any]) -> tuple[Path, dict[str, Any] | None]:
    """Keep fresh training on the same pixels and filenames used by fresh SfM."""
    sfm = config.get("integrations", {}).get("sfm", {})
    model = config.get("integrations", {}).get("model", {})
    sfm_dir = raw_sfm_image_directory(root, config)
    model_dir = _project_path(root, model.get("image_directory", sfm.get("image_directory", config["source"]["photo_directory"])))
    sfm_effective, manifest = effective_sfm_dataset(root, config)
    if manifest:
        if model_dir not in {sfm_dir, sfm_effective}:
            raise BackendError(
                "Fresh training image_directory must resolve to the same registered source dataset used by SfM so prepared filenames and orientation remain identical",
                failure_kind="INVALID_CONFIGURATION",
                details={"sfm_source_directory": str(sfm_dir), "training_image_directory": str(model_dir),
                         "effective_sfm_directory": str(sfm_effective)},
            )
        return sfm_effective, manifest
    return model_dir, None


def validate_preparation_stage_outputs(root: Path, config: dict[str, Any]) -> bool:
    """Validate cached preparation artifacts before run_workflow skips the stage."""
    path = preparation_manifest_path(root)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    sfm = config.get("integrations", {}).get("sfm", {})
    raw = raw_sfm_image_directory(root, config)
    if sfm.get("mode", "adopt_existing") == "spirula_fresh" and contains_heif(raw):
        try:
            _manifest_is_current(root, config, manifest, validate_files=True)
            return True
        except BackendError:
            return False
    if manifest.get("schema_version") != PREP_SCHEMA or manifest.get("source_photographs_modified") is not False:
        return False
    if Path(manifest.get("source_directory", "")).resolve() != raw:
        return False
    return manifest.get("status") in {
        "NO_CONVERSION_REQUIRED", "BACKEND_DOES_NOT_REQUIRE_PREPARATION"
    }


def _write_not_required(root: Path, config: dict[str, Any], status: str,
                        input_dir: Path, source_inventory: dict[str, Any]) -> dict[str, Any]:
    rows = source_inventory.get("images", [])
    selected_hash = _json_digest(sorted((str(row.get("relative_path")), str(row.get("sha256"))) for row in rows))
    manifest = {
        "schema_version": PREP_SCHEMA,
        "status": status,
        "project_id": config["project_id"],
        "source_directory": str(input_dir.resolve()),
        "effective_image_directory": str(input_dir.resolve()),
        "conversion_required": False,
        "reason": "The selected stage/backend does not require Engine HEIC conversion" if status != "NO_CONVERSION_REQUIRED" else "No HEIC/HEIF source files were present",
        "source_inventory_fingerprint_sha256": selected_hash,
        "preparation_settings": PREP_SETTINGS,
        "jpeg_quality": JPEG_QUALITY,
        "source_photographs_modified": False,
        "image_mappings": [],
    }
    path = preparation_manifest_path(root)
    _atomic_write_json(path, manifest)
    return {"status": status, "manifest_path": path, "effective_image_directory": input_dir.resolve(),
            "details": {"conversion_required": False, "source_photographs_modified": False,
                        "source_image_count": len(rows)}}


def prepare_spirula_dataset(root: Path, config: dict[str, Any],
                            source_inventory: dict[str, Any]) -> dict[str, Any]:
    """Prepare a verified, reusable dataset only when fresh Spirula SfM needs it."""
    sfm = config.get("integrations", {}).get("sfm", {})
    input_dir = raw_sfm_image_directory(root, config)
    if sfm.get("mode", "adopt_existing") != "spirula_fresh":
        return _write_not_required(root, config, "BACKEND_DOES_NOT_REQUIRE_PREPARATION", input_dir, source_inventory)

    if not input_dir.is_dir():
        raise BackendError(f"Configured fresh SfM image directory does not exist: {input_dir}", failure_kind="INVALID_INPUT")
    if not contains_heif(input_dir):
        return _write_not_required(root, config, "NO_CONVERSION_REQUIRED", input_dir, source_inventory)

    source_root = _project_path(root, config["source"]["photo_directory"])
    if input_dir != source_root and source_root not in input_dir.parents:
        raise BackendError("HEIC sources selected for fresh SfM are outside the registered source inventory", failure_kind="INVALID_INPUT")
    records = _source_records(root, config, input_dir, source_inventory)
    if not records or not any(row["source_format"] in {x.lstrip(".") for x in HEIF_SUFFIXES} for row in records):
        raise BackendError("HEIC files were detected but none are represented in the registered source inventory", failure_kind="STALE_SOURCE_INVENTORY")

    converter = heif_converter_identity()
    output_names = _mapped_output_names(records)
    source_fingerprint_rows = [{"source_relative_path": row["source_relative_path"],
                                "source_sha256": row["source_sha256"],
                                "input_relative_path": row["input_relative_path"],
                                "derived_relative_path": output_names[row["input_relative_path"]]}
                               for row in records]
    preparation_basis = {"schema_version": PREP_SCHEMA, "source_directory": str(input_dir.resolve()),
                         "registered_source_directory": str(source_root.resolve()),
                         "sources": source_fingerprint_rows, "settings": PREP_SETTINGS,
                         "converter": converter}
    preparation_id = _json_digest(preparation_basis)
    managed_root = root / PREP_DIR
    managed_root.mkdir(parents=True, exist_ok=True)
    dataset = managed_root / preparation_id
    cached = _prepared_dataset_valid(dataset, preparation_id, records, preparation_basis)
    manifest_path = preparation_manifest_path(root)
    if cached:
        current_manifest = dict(cached)
        current_manifest["effective_image_directory"] = str(dataset.resolve())
        current_manifest["source_directory"] = str(input_dir.resolve())
        _atomic_write_json(manifest_path, current_manifest)
        return {"status": "COMPLETED", "manifest_path": manifest_path,
                "effective_image_directory": dataset.resolve(),
                "details": {"conversion_required": True, "cache_reused": True,
                            "source_image_count": len(records),
                            "converted_heif_count": sum(row["source_format"] in {x.lstrip(".") for x in HEIF_SUFFIXES} for row in records),
                            "manifest_path": str(manifest_path.resolve()),
                            "preparation_fingerprint": preparation_id,
                            "converter": converter}}

    estimate_copy_bytes = sum(row["source_size_bytes"] for row in records
                              if row["source_format"] not in {x.lstrip(".") for x in HEIF_SUFFIXES})
    estimate_heif_bytes = sum(row["source_size_bytes"] for row in records
                              if row["source_format"] in {x.lstrip(".") for x in HEIF_SUFFIXES})
    heif_count = sum(row["source_format"] in {x.lstrip(".") for x in HEIF_SUFFIXES} for row in records)
    estimated_outputs = estimate_copy_bytes + max(estimate_heif_bytes * 5, heif_count * 1024**2)
    policy = config.get("resource_policy", {})
    reserve = int(policy.get("minimum_free_disk_bytes_dataset_prep",
                             policy.get("minimum_free_disk_bytes_sfm", policy.get("minimum_free_disk_bytes", 0))))
    free = shutil.disk_usage(managed_root).free
    required = reserve + estimated_outputs
    if free < required:
        raise BackendError(
            f"Insufficient free disk space for HEIC dataset preparation: {free} bytes free, {required} estimated required (including a {reserve}-byte reserve)",
            failure_kind="RESOURCE_LIMIT",
            details={"free_bytes": free, "estimated_output_bytes": estimated_outputs,
                     "reserve_bytes": reserve, "minimum_required_bytes": required},
        )

    staging = managed_root / (".staging-" + uuid.uuid4().hex)
    staging.mkdir(mode=0o700)
    requests: list[dict[str, str]] = []
    mapping_by_input = {row["input_relative_path"]: row for row in records}
    try:
        for input_rel, output_rel in output_names.items():
            row = mapping_by_input[input_rel]
            output = staging / output_rel
            output.parent.mkdir(parents=True, exist_ok=True)
            if row["source_format"] in {x.lstrip(".") for x in HEIF_SUFFIXES}:
                temporary_output = output.with_name(output.name + ".part")
                requests.append({"source": row["source_path"], "output": str(temporary_output)})
            else:
                shutil.copy2(row["source_path"], output)

        conversion_details = _run_heif_convert_batch(
            requests, converter,
            int(policy.get("dataset_prep_timeout_seconds", 3600)),
        )
        image_mappings: list[dict[str, Any]] = []
        for input_rel, output_rel in output_names.items():
            row = mapping_by_input[input_rel]
            target = staging / output_rel
            if row["source_format"] in {x.lstrip(".") for x in HEIF_SUFFIXES}:
                temporary_output = target.with_name(target.name + ".part")
                details = conversion_details.get(row["source_path"])
                if not details or not temporary_output.is_file():
                    raise BackendError(f"heif-convert did not produce a fully verified JPEG for {row['source_relative_path']}", failure_kind="CONVERSION_FAILED")
                os.replace(temporary_output, target)
                if target.suffix.casefold() not in JPEG_SUFFIXES or _jpeg_dimensions(target) != (details.get("width"), details.get("height")):
                    raise BackendError(f"JPEG header/dimensions failed independent verification for {row['source_relative_path']}", failure_kind="CONVERSION_FAILED")
                conversion_name = "HEIC_TO_JPEG_LIBHEIF"
            else:
                details = None
                if not target.is_file() or sha256_file(target) != row["source_sha256"]:
                    raise BackendError(f"Compatible image copy failed byte verification for {row['source_relative_path']}", failure_kind="CONVERSION_FAILED")
                conversion_name = "COPIED_UNCHANGED"
            mapping = {
                "source_relative_path": row["source_relative_path"],
                "input_relative_path": input_rel,
                "derived_relative_path": output_rel,
                "source_sha256": row["source_sha256"],
                "source_size_bytes": row["source_size_bytes"],
                "derived_sha256": sha256_file(target),
                "derived_size_bytes": target.stat().st_size,
                "source_format": row["source_format"],
                "conversion": conversion_name,
                "width": details.get("width") if details else None,
                "height": details.get("height") if details else None,
                "conversion_validation": details,
            }
            image_mappings.append(mapping)

        manifest = {
            "schema_version": PREP_SCHEMA,
            "status": "PREPARED",
            "project_id": config["project_id"],
            "source_directory": str(input_dir.resolve()),
            "registered_source_directory": str(source_root.resolve()),
            "effective_image_directory": str(dataset.resolve()),
            "conversion_required": True,
            "conversion": {"backend": converter, "settings": PREP_SETTINGS,
                           "jpeg_quality": JPEG_QUALITY,
                           "color_handling": "For converted HEIC, the output JPEG ICC profile is decoded and identified; ICC bytes are compared with the libheif-decoded source when present. Display P3 is never treated as DCI-P3. Byte-copied JPEG inputs remain unchanged and are not profile-inspected."},
            "preparation_settings": PREP_SETTINGS,
            "jpeg_quality": JPEG_QUALITY,
            "preparation_fingerprint": preparation_id,
            "preparation_basis": preparation_basis,
            "source_inventory_fingerprint_sha256": _json_digest(
                [{"source_relative_path": row["source_relative_path"], "source_sha256": row["source_sha256"]}
                 for row in records]),
            "source_photographs_modified": False,
            "resource_check": {"free_bytes_before": free, "estimated_output_bytes": estimated_outputs,
                               "reserve_bytes": reserve, "minimum_required_bytes": required},
            "image_count": len(records),
            "converted_heif_count": sum(row["source_format"] in {x.lstrip(".") for x in HEIF_SUFFIXES} for row in records),
            "copied_unchanged_count": sum(row["source_format"] not in {x.lstrip(".") for x in HEIF_SUFFIXES} for row in records),
            "image_mappings": image_mappings,
        }
        # The dataset-local manifest proves the output tree was created by this
        # Engine version. It is not part of Spirula's image inventory.
        write_json(staging / DATASET_MANIFEST, manifest)
        if dataset.exists():
            try:
                old = json.loads((dataset / DATASET_MANIFEST).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                old = {}
            if old.get("schema_version") != PREP_SCHEMA:
                raise BackendError(f"Refusing to replace an unmanaged dataset directory: {dataset}", failure_kind="UNSAFE_OUTPUT")
            stale = managed_root / (".stale-" + preparation_id + "-" + uuid.uuid4().hex)
            os.replace(dataset, stale)
        os.replace(staging, dataset)
        manifest["effective_image_directory"] = str(dataset.resolve())
        _atomic_write_json(manifest_path, manifest)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    return {"status": "COMPLETED", "manifest_path": manifest_path,
            "effective_image_directory": dataset.resolve(),
            "details": {"conversion_required": True, "cache_reused": False,
                        "source_image_count": len(records), "converted_heif_count": manifest["converted_heif_count"],
                        "copied_unchanged_count": manifest["copied_unchanged_count"],
                        "manifest_path": str(manifest_path.resolve()),
                        "preparation_fingerprint": preparation_id, "converter": converter,
                        "resource_check": manifest["resource_check"]}}


def image_gamut_mismatch(config: dict[str, Any], manifest: dict[str, Any] | None) -> str | None:
    """Reject explicit gamut labels that contradict verified derivative profiles."""
    if not manifest or manifest.get("status") != "PREPARED":
        return None
    values = []
    converted = [mapping for mapping in manifest.get("image_mappings", [])
                 if mapping.get("conversion") == "HEIC_TO_JPEG_LIBHEIF"]
    for mapping in converted:
        details = mapping.get("conversion_validation") or {}
        profile = details.get("output_color_profile")
        if isinstance(profile, str):
            normalized = re.sub(r"[^a-z0-9]", "", profile.casefold())
            if "displayp3" in normalized:
                values.append("display-p3")
            elif "dcip3" in normalized:
                values.append("dci-p3")
            elif "srgb" in normalized:
                values.append("srgb")
            elif profile:
                values.append("unknown:" + profile)
            else:
                values.append("unknown:missing-profile-name")
        else:
            values.append("unknown:missing-profile-name")
    configured = None
    args = config.get("integrations", {}).get("sfm", {}).get("additional_args", [])
    for index, value in enumerate(args if isinstance(args, list) else []):
        if value == "--image-gamut" and index + 1 < len(args):
            configured = str(args[index + 1])
        elif isinstance(value, str) and value.startswith("--image-gamut="):
            configured = value.split("=", 1)[1]
    if configured is None:
        return None
    if not converted:
        return None
    uninspected_jpegs = [mapping for mapping in manifest.get("image_mappings", [])
                         if mapping.get("conversion") == "COPIED_UNCHANGED" and
                         Path(str(mapping.get("derived_relative_path", ""))).suffix.casefold() in JPEG_SUFFIXES and
                         not (mapping.get("conversion_validation") or {}).get("output_color_profile")]
    if uninspected_jpegs:
        return (f"Configured --image-gamut={configured} cannot be verified for the mixed dataset because "
                f"{len(uninspected_jpegs)} byte-copied JPEG input(s) have no recorded profile evidence")
    normalized_config = re.sub(r"[^a-z0-9]", "", configured.casefold())
    normalized_profiles = set(values)
    if "unknown:" in " ".join(values) or len(normalized_profiles) != 1:
        return f"Configured --image-gamut={configured} cannot be verified against mixed or unrecognized derivative ICC profiles: {sorted(normalized_profiles)}"
    profile = next(iter(normalized_profiles))
    expected = re.sub(r"[^a-z0-9]", "", profile.casefold())
    if normalized_config != expected:
        return f"Configured --image-gamut={configured} does not match the verified derivative color profile {profile}; Display P3 must not be labeled DCI-P3"
    return None
