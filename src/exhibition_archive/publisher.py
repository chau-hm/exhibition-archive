from __future__ import annotations

import html
import json
import math
import os
import re
import struct
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile, is_zipfile

from .adapters import public_json, read_json, sha256_file, write_json
from .workflow import output_path, project_path


PRODUCT_ROOT = Path(__file__).resolve().parents[2]
SOG_TOOL_VERSION = "3.10.3"
SOG_SH_ITERATIONS = 10
SOG_MAX_WORKERS = 4
SOG_MAX_SECONDS = 600
SOG_NODE_OLD_SPACE_MB = 8192
SOG_OUTPUT_TAIL_CHARS = 12000
SOG_IMPORT_DIRECTORY = "artifacts/visitor-sog"


def _process_output_tail(value: str | bytes | None) -> str | None:
    if value is None:
        return None
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
    return text[-SOG_OUTPUT_TAIL_CHARS:] or None


def _path(root: Path, config: dict, group: str, key: str) -> Path:
    raw = config["integrations"][group][key]
    p = Path(raw).expanduser()
    return (p if p.is_absolute() else root / p).resolve()


def _subtree(value: Any, keys: set[str]) -> Any:
    if isinstance(value, dict):
        return {k: _subtree(v, keys) for k, v in value.items() if k in keys}
    if isinstance(value, list):
        return [_subtree(x, keys) for x in value]
    return value


def _write_json(out: Path, name: str, value: Any) -> None:
    write_json(out / "api" / f"{name}.json", public_json(value))


def _static_api(root: Path, config: dict, website: Path, model_record: dict, state) -> dict[str, Any]:
    nav_cfg = config["integrations"]["navigation"]
    navigation_artifact = None
    generated_navigation = nav_cfg.get("mode") == "generate_from_sfm"
    if generated_navigation:
        approved_path = output_path(root, config) / "work" / "navigation-approved.json"
        navigation_stage = state.stage("navigation")
        if not approved_path.is_file() or not navigation_stage or navigation_stage.get("status") != "ADOPTED":
            raise ValueError("Public viewer packaging is blocked: generated navigation has no explicitly approved artifact")
        navigation_artifact = read_json(approved_path)
        if navigation_artifact.get("status") != "NAVIGATION_APPROVED_BY_REVIEW" or navigation_artifact.get("provenance", {}).get("navigation_input_fingerprint") != navigation_stage.get("input_fingerprint"):
            raise ValueError("Public viewer packaging is blocked: approved navigation is stale or not review-approved")
        compatibility = navigation_artifact.get("viewer_compatibility") or {}
        envelope = compatibility.get("envelope")
        policy = compatibility.get("runtime_policy")
        if not envelope or not policy or not navigation_artifact.get("validation", {}).get("public_ready"):
            raise ValueError("Public viewer packaging is blocked: approved navigation artifact is incomplete")
    else:
        envelope = read_json(_path(root, config, "navigation", "envelope"))
        policy = read_json(_path(root, config, "navigation", "runtime_policy"))
    baseline = Path(model_record["path"])
    expected = model_record["sha256"]
    health = {"ok": True, "modelBytes": baseline.stat().st_size, "sha256": expected, "expectedSha256": expected,
              "exactBaselineMatch": model_record.get("selection") == "accepted baseline", "modelSelection": model_record.get("selection"),
              "contentUrl": "../assets/exhibition.ply", "modelFilename": "exhibition.ply",
              "boundaryId": envelope.get("id"),
              "boundarySource": envelope.get("horizontalBoundary", {}).get("classification"), "legacyActiveWallCount": 0,
              "indexedSourceImages": 0, "projectId": config["project_id"], "publication": "STATIC; no source photographs or private review endpoints"}
    _write_json(website, "health", health)
    _write_json(website, "boundary", envelope)
    _write_json(website, "boundary-generated", envelope)

    if generated_navigation:
        compatibility = navigation_artifact["viewer_compatibility"]
        stations = compatibility.get("stations", {"stations": []})
        centers = compatibility.get("camera_centers", {"stations": []})
        tours = compatibility.get("guided_tours", {"routes": []})
        candidate_registry = {"candidates": []}
        footprints = {"candidates": [{"id": region["id"], "algorithm": region["algorithm"], "marginUnits": 0.0,
                                      "areaUnits2": abs(sum(region["polygon_uv"][i][0] * region["polygon_uv"][(i+1) % len(region["polygon_uv"])][1] - region["polygon_uv"][(i+1) % len(region["polygon_uv"])][0] * region["polygon_uv"][i][1] for i in range(len(region["polygon_uv"]))) / 2),
                                      "trustedStationInclusion": {"insideOrOn": region["evidence"]["trusted_station_count"], "total": region["evidence"]["trusted_station_count"]},
                                      "polygon2d": region["polygon_uv"]} for region in navigation_artifact.get("regions", [])]}
        distribution = {"threeNearestNeighborEdges": []}
        transform = {"transform": navigation_artifact.get("coordinate_frame", {})}
        hang = {}
    else:
        stations = read_json(_path(root, config, "navigation", "stations"))
        centers = read_json(_path(root, config, "navigation", "camera_centers"))
        tours = read_json(_path(root, config, "navigation", "guided_tours"))
        candidate_registry = read_json(_path(root, config, "navigation", "candidate_registry"))
        footprints = read_json(_path(root, config, "navigation", "footprints"))
        distribution = read_json(_path(root, config, "navigation", "distribution"))
        transform = read_json(_path(root, config, "navigation", "transform"))
        hang = read_json(_path(root, config, "navigation", "hanging_text_geometry"))
    artwork_config = config.get("integrations", {}).get("artwork", {})
    confirmed_path = project_path(root, config, artwork_config["confirmed_artworks"]) if artwork_config.get("confirmed_artworks") else output_path(root, config) / "work" / "confirmed-artworks.json"
    confirmed = read_json(confirmed_path)

    # The accepted Phase 2ER app requires separate station records for safe
    # spawn/view direction and camera-center records for its map. Only those
    # fields are exported; source paths and investigative notes stay private.
    public_station_keys = ("stationId", "centerCommonViewer", "representativeForwardCommonViewer", "normalVisitorPositionCandidate", "regionId")
    public_stations = [{key: station[key] for key in public_station_keys if key in station} for station in stations.get("stations", [])]
    public_camera_stations = [{key: station[key] for key in ("stationId", "planeCoordinates") if key in station} for station in centers.get("stations", [])]
    _write_json(website, "stations", {"schemaVersion": 1, "defaultStationId": nav_cfg.get("default_station_id") or stations.get("defaultStationId"), "stations": public_stations})
    _write_json(website, "cameras", {"schemaVersion": 1, "principalImageKeys": [], "stations": public_camera_stations})
    public_routes = []
    for index, route in enumerate(tours.get("routes", []), start=1):
        if not isinstance(route, dict) or not isinstance(route.get("waypoints"), list):
            continue
        waypoints = []
        for pose in route["waypoints"]:
            if not isinstance(pose, dict):
                continue
            position, target = pose.get("position"), pose.get("target")
            if not all(isinstance(point, list) and len(point) == 3 and
                       all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) for value in point)
                       for point in (position, target)):
                continue
            waypoint = {"position": position, "target": target}
            fov = pose.get("fov")
            if isinstance(fov, (int, float)) and not isinstance(fov, bool) and math.isfinite(fov):
                waypoint["fov"] = fov
            waypoints.append(waypoint)
        if len(waypoints) >= 2:
            public_routes.append({
                "id": f"guided-route-{index:02d}",
                "title": str(route.get("title") or f"Guided route {index}"),
                "waypoints": waypoints,
            })
    _write_json(website, "tours", {"schemaVersion": 1, "routes": public_routes})
    candidate_ids = config.get("viewer", {}).get("public_geometry_view_ids")
    selected_candidates = [x for x in candidate_registry.get("candidates", []) if candidate_ids and x.get("candidateId") in candidate_ids]
    candidate_map = {item["candidateId"]: f"geometry-view-{index + 1:02d}" for index, item in enumerate(selected_candidates)}
    public_candidate_keys = ("commonCenter", "commonHalfExtents", "commonRotationRows", "preferredStationId")
    candidates = []
    for item in selected_candidates:
        candidate = {key: item[key] for key in public_candidate_keys if key in item}
        candidate["candidateId"] = candidate_map[item["candidateId"]]
        candidate["publicLabel"] = f"Geometry view {len(candidates) + 1}"
        candidates.append(candidate)
    default_candidate = config.get("viewer", {}).get("default_geometry_view_id")
    _write_json(website, "candidates", {"schemaVersion": 1, "defaultCandidateId": candidate_map.get(default_candidate, candidates[0]["candidateId"] if candidates else None), "candidates": candidates})
    # Map layers are reduced to values directly consumed by the read-only map.
    footprint_public = {"schemaVersion": 1, "candidates": [{k: x.get(k) for k in ("id", "algorithm", "marginUnits", "areaUnits2", "trustedStationInclusion", "polygon2d")} for x in footprints.get("candidates", [])], "concaveAlphaSweep": []}
    _write_json(website, "footprints", footprint_public)
    _write_json(website, "distribution", {"schemaVersion": 1, "threeNearestNeighborEdges": distribution.get("threeNearestNeighborEdges", [])})
    _write_json(website, "transform", transform.get("transform", {}))
    from .visitor import canonical_initial_camera, viewer_transform_node
    initial_camera = canonical_initial_camera(envelope, stations, viewer_transform_node(transform.get("transform", {})))
    _write_json(website, "initial-camera", initial_camera)
    _write_json(website, "artifact-evidence", {"schemaVersion": 1, "status": "GEOMETRY_VIEWPOINT_ONLY; no artwork identity or semantic label is published", "candidate": candidates[0] if candidates else {}})
    _write_json(website, "ghost-annotations", {"schemaVersion": 1, "annotations": []})

    config_file = {"schema_version": "exhibition-archive.public-viewer.v1", "project_id": config["project_id"],
                   "title": config.get("viewer", {}).get("title", config["project_id"]), "theme": config.get("viewer", {}).get("theme", "dark"),
                   "model": {"url": "../assets/exhibition.ply", "filename": "exhibition.ply",
                             "sha256": expected, "size_bytes": baseline.stat().st_size,
                             "gaussian_count": model_record.get("gaussian_count")},
                   "artwork_catalogue_status": confirmed.get("count", len(confirmed.get("artworks", []))),
                   "public_catalogue": "Only reviewer-confirmed artwork records are listed in config/artworks.json.",
                   "navigation_policy": "Explicitly reviewed camera-pose navigation artifact; scene units are nonmetric unless calibrated." if generated_navigation else "Imported accepted camera-pose enclosure; navigation geometry is nonmetric and not surveyed."}
    write_json(website / "config" / "exhibition.json", config_file)
    if generated_navigation:
        public_policy = {key: value for key, value in policy.items() if key != "review"}
        public_navigation = {"schema_version": navigation_artifact["schema_version"], "artifact_version": navigation_artifact["artifact_version"],
                             "id": navigation_artifact["id"], "status": navigation_artifact["status"],
                             "coordinate_frame": navigation_artifact["coordinate_frame"],
                             "regions": [{"id": region["id"], "polygon_uv": region["polygon_uv"], "algorithm": region["algorithm"],
                                          "classification": region["classification"], "vertical_bounds": region["vertical_bounds"]}
                                         for region in navigation_artifact.get("regions", [])],
                             "transitions": [{"from_region": item["from_region"], "to_region": item["to_region"],
                                              "portal": {"segment_uv": item["portal"]["segment_uv"]},
                                              "approved": bool(item.get("approved")),
                                              "classification": item.get("classification", "REVIEWED_PORTAL")}
                                             for item in navigation_artifact.get("transitions", [])],
                             "camera_clearance": navigation_artifact.get("camera_clearance"),
                             "viewer_compatibility": {"envelope": envelope, "runtime_policy": public_policy},
                             "validation": {"status": navigation_artifact["validation"].get("status"), "public_ready": True}}
        write_json(website / "config" / "navigation.json", public_json(public_navigation))
    else:
        write_json(website / "config" / "navigation.json", public_json({"envelope": envelope, "runtime_policy": policy}))
    write_json(website / "config" / "artworks.json", public_json(confirmed))
    return {"health": health, "public_artwork_count": config_file["artwork_catalogue_status"], "static_api_count": 11}


def _visitor_transform(root: Path, config: dict) -> dict:
    raw = config.get("integrations", {}).get("navigation", {}).get("transform")
    if not raw:
        return {}
    path = _path(root, config, "navigation", "transform")
    return read_json(path)


def _splat_transform_binary() -> str | None:
    configured = os.environ.get("EXHIBITION_ARCHIVE_SPLAT_TRANSFORM")
    if configured:
        path = Path(configured).expanduser()
        return str(path.resolve()) if path.is_file() else None
    local_binary = PRODUCT_ROOT / "node_modules" / ".bin" / "splat-transform"
    if local_binary.is_file():
        return str(local_binary)
    return shutil.which("splat-transform")


def _tool_version(executable: str) -> str:
    result = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=30, check=False)
    text = (result.stdout or result.stderr).strip().splitlines()
    version = text[0] if text else ""
    match = re.search(r"\bv(\d+\.\d+\.\d+)\b", version)
    if result.returncode or not match or match.group(1) != SOG_TOOL_VERSION:
        raise ValueError(f"Expected splat-transform {SOG_TOOL_VERSION}; found {version or 'unknown'}")
    return version


def _tool_info(executable: str, path: Path) -> dict[str, Any]:
    result = subprocess.run([executable, str(path), "--info", "json", "null"], capture_output=True,
                            text=True, timeout=120, check=False)
    if result.returncode:
        raise ValueError(f"splat-transform --info failed for {path.name}: {_process_output_tail(result.stderr or result.stdout)}")
    output = result.stdout or ""
    start = output.find("{")
    if start < 0:
        raise ValueError(f"splat-transform --info returned no JSON for {path.name}")
    try:
        info = json.loads(output[start:output.rfind("}") + 1])
    except json.JSONDecodeError as error:
        raise ValueError(f"splat-transform --info returned invalid JSON for {path.name}: {error}") from error
    if not isinstance(info, dict) or info.get("gaussian") is not True or int(info.get("numGaussians", 0)) <= 0:
        raise ValueError(f"{path.name} is not a non-empty Gaussian model")
    return info


def _sog_archive_metadata(path: Path, expected_count: int | None = None) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size <= 0 or not is_zipfile(path):
        raise ValueError("SOG is missing, empty, unreadable, or not a bundled SOG archive")
    try:
        with ZipFile(path) as archive:
            names = set(archive.namelist())
            if any(name.startswith("/") or ".." in Path(name).parts for name in names):
                raise ValueError("SOG archive contains an unsafe member path")
            bad_member = archive.testzip()
            if bad_member:
                raise ValueError(f"SOG archive CRC check failed for {bad_member}")
            if "meta.json" not in names:
                raise ValueError("SOG archive has no meta.json")
            metadata = json.loads(archive.read("meta.json"))
            if metadata.get("version") not in {1, 2}:
                raise ValueError(f"Unsupported SOG metadata version: {metadata.get('version')}")
            count = int(metadata.get("count", 0))
            if count <= 0 or (expected_count is not None and count != expected_count):
                raise ValueError(f"SOG Gaussian count {count} does not match expected count {expected_count}")
            referenced = []
            for key in ("means", "scales", "quats", "sh0", "shN"):
                record = metadata.get(key)
                if isinstance(record, dict):
                    referenced.extend(record.get("files", []))
            if not referenced or any(name not in names for name in referenced):
                raise ValueError("SOG metadata references missing texture assets")
            return {"format": "sog", "metadata_version": metadata["version"], "gaussian_count": count,
                    "generator": metadata.get("asset", {}).get("generator"),
                    "sh_texture_present": any(name.startswith("shN_") for name in referenced),
                    "member_count": len(names)}
    except (BadZipFile, OSError, json.JSONDecodeError) as error:
        raise ValueError(f"SOG archive validation failed: {error}") from error


def register_external_sog(root: Path, config: dict, source_sog: Path, *, converter_version: str,
                          command: str, device: str | None = None, gpu_selection: str | None = None,
                          sh_iterations: int = SOG_SH_ITERATIONS,
                          webp_effort: int | None = None, execution_metadata: dict[str, Any] | None = None,
                          replace: bool = False) -> dict[str, Any]:
    """Validate and register a SOG produced outside the Engine's execution environment."""
    from .workflow import output_path, project_path

    source_sog = source_sog.expanduser().resolve()
    if source_sog.suffix.casefold() != ".sog":
        raise ValueError("External visitor model must use the .sog extension")
    model_config = config.get("integrations", {}).get("model", {})
    source_ply_value = model_config.get("path")
    expected_source_hash = model_config.get("expected_sha256")
    if not source_ply_value or not expected_source_hash:
        raise ValueError("Project must identify the accepted master PLY path and expected SHA-256")
    source_ply = project_path(root, config, source_ply_value)
    if not source_ply.is_file():
        raise ValueError(f"Accepted source PLY is missing: {source_ply}")
    input_hash = sha256_file(source_ply)
    if input_hash != expected_source_hash:
        raise ValueError("Accepted source PLY hash does not match the project manifest")
    binary = _splat_transform_binary()
    if not binary:
        raise ValueError("splat-transform is required to inspect and validate the SOG import")
    inspector_version = _tool_version(binary)
    source_info = _tool_info(binary, source_ply)
    sog_info = _tool_info(binary, source_sog)
    if source_info.get("format") != "ply" or sog_info.get("format") != "sog":
        raise ValueError("Source must inspect as PLY and imported artifact must inspect as SOG")
    source_count = int(source_info["numGaussians"])
    sog_count = int(sog_info["numGaussians"])
    source_bands = int(source_info.get("shBands", 0))
    sog_bands = int(sog_info.get("shBands", 0))
    if source_count != sog_count or source_bands != sog_bands:
        raise ValueError(f"SOG content metadata differs from its source PLY (count {source_count}/{sog_count}; SH bands {source_bands}/{sog_bands})")
    archive_metadata = _sog_archive_metadata(source_sog, source_count)
    generator_version = re.search(r"v?(\d+\.\d+\.\d+)", str(archive_metadata.get("generator", "")))
    claimed_version = re.search(r"v?(\d+\.\d+\.\d+)", converter_version)
    if not claimed_version or not generator_version or generator_version.group(1) != claimed_version.group(1):
        raise ValueError("Supplied converter version does not match the SOG generator metadata")
    if not command.strip():
        raise ValueError("The conversion command must be recorded for external SOG provenance")
    if sh_iterations < 1 or (webp_effort is not None and not 0 <= webp_effort <= 9):
        raise ValueError("SOG conversion settings are outside supported ranges")
    if execution_metadata is not None and not isinstance(execution_metadata, dict):
        raise ValueError("External SOG execution metadata must be a JSON object")

    output_root = output_path(root, config) / SOG_IMPORT_DIRECTORY
    target = output_root / "exhibition.sog"
    provenance_path = output_root / "provenance.json"
    output_hash = sha256_file(source_sog)
    if target.exists() and not replace:
        if target.is_file() and sha256_file(target) == output_hash:
            existing = read_json(provenance_path) if provenance_path.is_file() else {}
            if existing.get("output_sha256") == output_hash and existing.get("input_sha256") == input_hash:
                return {"status": "ALREADY_REGISTERED", "artifact": str(target), "provenance": str(provenance_path),
                        "output_sha256": output_hash, "output_size_bytes": target.stat().st_size}
        raise FileExistsError(f"A different SOG is already registered at {target}; pass replace=True to replace it explicitly")

    output_root.mkdir(parents=True, exist_ok=True)
    staged = output_root / "exhibition.sog.staging"
    staged.unlink(missing_ok=True)
    shutil.copy2(source_sog, staged)
    if sha256_file(staged) != output_hash or staged.stat().st_size != source_sog.stat().st_size:
        staged.unlink(missing_ok=True)
        raise ValueError("Copied SOG failed byte-integrity verification")
    os.replace(staged, target)
    provenance = {
        "schema_version": "exhibition-archive.visitor-sog-provenance.v1",
        "status": "VALIDATED_EXTERNAL",
        "registered_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source_ply_path": str(source_ply),
        "input_sha256": input_hash,
        "input_size_bytes": source_ply.stat().st_size,
        "input_gaussian_count": source_count,
        "input_sh_bands": source_bands,
        "converter": {"name": "@playcanvas/splat-transform", "version": converter_version,
                      "command": command, "device": device, "gpu_selection": gpu_selection,
                      "parameters": {"sh_iterations": sh_iterations, "webp_effort": webp_effort,
                                     "gpu": gpu_selection or device or "unknown",
                                     "filters": [], "decimation": None, "coordinate_transform": "none",
                                     "workers": SOG_MAX_WORKERS},
                      "execution_metadata": execution_metadata or {}},
        "inspector": {"name": "@playcanvas/splat-transform", "version": inspector_version,
                      "source_info": source_info, "sog_info": sog_info},
        "sog_format": archive_metadata,
        "output_sha256": output_hash,
        "output_size_bytes": source_sog.stat().st_size,
        "compression_ratio_output_to_input": source_sog.stat().st_size / source_ply.stat().st_size,
    }
    write_json(provenance_path, provenance)
    return {"status": provenance["status"], "artifact": str(target), "provenance": str(provenance_path),
            "output_sha256": output_hash, "output_size_bytes": target.stat().st_size,
            "input_sha256": input_hash, "gaussian_count": source_count, "sh_bands": source_bands}


def _registered_sog(model: Path, record: dict, target: Path) -> dict[str, Any] | None:
    registered = Path(record["path"]).resolve()
    provenance_path = registered.with_name("provenance.json")
    if not registered.is_file() and not provenance_path.is_file():
        return None
    if not registered.is_file() or not provenance_path.is_file():
        raise ValueError("Registered external SOG artifact is incomplete")
    provenance = read_json(provenance_path)
    expected_hash = record.get("sha256")
    if (provenance.get("status") != "VALIDATED_EXTERNAL" or provenance.get("input_sha256") != expected_hash
            or sha256_file(model) != expected_hash or provenance.get("output_sha256") != sha256_file(registered)
            or provenance.get("output_size_bytes") != registered.stat().st_size):
        raise ValueError("Registered SOG provenance does not match the current accepted PLY or SOG bytes")
    sog_info = _sog_archive_metadata(registered, int(record.get("gaussian_count") or provenance.get("input_gaussian_count") or 0))
    info = provenance.get("inspector", {}).get("sog_info", {})
    if info.get("format") != "sog" or int(info.get("numGaussians", 0)) != sog_info["gaussian_count"]:
        raise ValueError("Registered SOG metadata does not match the validated archive")
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_name(target.name + ".staging")
    shutil.copy2(registered, staged)
    if sha256_file(staged) != provenance["output_sha256"]:
        staged.unlink(missing_ok=True)
        raise ValueError("Staged SOG copy failed SHA-256 validation")
    os.replace(staged, target)
    converter = provenance["converter"]
    parameters = converter.get("parameters", {})
    public_command = ["splat-transform", "-g", str(converter.get("gpu_selection") or "auto"), "--verbose", "--memory",
                      "accepted-master.ply", "exhibition.sog", "-i", str(parameters.get("sh_iterations", SOG_SH_ITERATIONS))]
    return {
        "status": "VALIDATED_EXTERNAL",
        "method": "external-artifact-import",
        "tool": "@playcanvas/splat-transform CLI",
        "tool_version": converter.get("version"),
        "inspector_version": provenance.get("inspector", {}).get("version"),
        "execution_metadata": converter.get("execution_metadata", {}),
        "command_template": public_command,
        "device": converter.get("device"),
        "settings": parameters,
        "input_sha256": provenance["input_sha256"],
        "input_size_bytes": provenance["input_size_bytes"],
        "input_gaussian_count": provenance["input_gaussian_count"],
        "input_sh_bands": provenance["input_sh_bands"],
        "sog_format": sog_info,
        "output_size_bytes": registered.stat().st_size,
        "compression_ratio_output_to_input": registered.stat().st_size / model.stat().st_size,
        "output_sha256": provenance["output_sha256"],
    }


def _convert_sog(model: Path, target: Path) -> dict[str, Any]:
    from .visitor import VIEWER_VERSION

    command_template = ["splat-transform", "-g", "auto", "--no-tty", "--verbose", "--memory", "accepted-master.ply",
                        "exhibition.sog", "-i", str(SOG_SH_ITERATIONS), "--max-workers", str(SOG_MAX_WORKERS)]
    base_provenance = {
        "tool": "@playcanvas/splat-transform CLI",
        "expected_tool_version": SOG_TOOL_VERSION,
        "elapsed_time_limit_seconds": SOG_MAX_SECONDS,
        "node_old_space_limit_mb": SOG_NODE_OLD_SPACE_MB,
        "settings": {"sh_band_clustering_iterations": SOG_SH_ITERATIONS, "gpu": "auto",
                     "workers": SOG_MAX_WORKERS, "filters": [], "decimation": None,
                     "coordinate_transform": "none; original accepted PLY coordinates retained"},
        "command_template": command_template,
        "viewer_version": VIEWER_VERSION,
        "input_size_bytes": model.stat().st_size,
        "input_sha256": sha256_file(model),
    }
    if os.environ.get("EXHIBITION_ARCHIVE_BUILD_SOG") != "1":
        return {**base_provenance, "status": "NOT_REQUESTED",
                "reason": "SOG conversion is opt-in; set EXHIBITION_ARCHIVE_BUILD_SOG=1 for an explicitly authorized bounded conversion. The visitor package uses its PLY fallback."}
    executable = _splat_transform_binary()
    if not executable:
        return {**base_provenance, "status": "NOT_AVAILABLE",
                "reason": "splat-transform was not found; the visitor package uses its PLY fallback."}
    try:
        version_text = _tool_version(executable)
    except (OSError, subprocess.TimeoutExpired, ValueError) as error:
        return {**base_provenance, "status": "NOT_AVAILABLE",
                "reason": f"Could not validate splat-transform: {error}. The visitor package uses its PLY fallback."}
    provenance = {**base_provenance, "tool_version": version_text}
    try:
        gpu_result = subprocess.run([executable, "--list-gpus"], capture_output=True, text=True, timeout=30, check=False)
        gpu_output = ((gpu_result.stdout or "") + "\n" + (gpu_result.stderr or "")).strip()
    except (OSError, subprocess.TimeoutExpired) as error:
        return {**provenance, "status": "GPU_DETECTION_FAILED",
                "reason": f"Could not enumerate SOG GPU adapters ({error}); refusing an implicit CPU fallback."}
    gpu_rows = []
    for line in gpu_output.splitlines():
        match = re.match(r"\s*(?:\[(\d+)\]|(?:GPU\s*)?(\d+)\s*[:.)-])\s*(\S.*)\s*$", line, re.IGNORECASE)
        if match:
            gpu_rows.append({"index": int(match.group(1) or match.group(2)), "name": match.group(3)})
    device_request = os.environ.get("EXHIBITION_ARCHIVE_SOG_DEVICE", "auto").strip().casefold()
    if (not gpu_rows and re.search(r"Failed to enumerate GPU adapters", gpu_output, re.IGNORECASE)
            and device_request != "cpu"):
        return {**provenance, "status": "GPU_DETECTION_FAILED", "gpu_enumeration": _process_output_tail(gpu_output),
                "reason": "SplatTransform could not enumerate GPU adapters; refusing an implicit CPU fallback."}
    if device_request == "cpu":
        selected_device: str | int = "cpu"
    elif device_request not in {"", "auto"}:
        if not device_request.isdigit() or int(device_request) not in {row["index"] for row in gpu_rows}:
            return {**provenance, "status": "GPU_UNAVAILABLE",
                    "gpu_adapters": gpu_rows, "gpu_enumeration": _process_output_tail(gpu_output),
                    "reason": f"Configured SOG GPU {device_request!r} was not found; no CPU fallback was attempted."}
        selected_device = int(device_request)
    elif gpu_rows:
        selected_device = gpu_rows[0]["index"]
    else:
        return {**provenance, "status": "GPU_UNAVAILABLE", "gpu_adapters": [],
                "gpu_enumeration": _process_output_tail(gpu_output),
                    "reason": "No supported GPU adapter is visible to this Engine process; no CPU fallback was attempted. Run `sog-import` with a SOG generated on a GPU-capable host, or explicitly configure and approve CPU mode."}
    provenance = {**provenance, "gpu_adapters": gpu_rows,
                  "selected_device": selected_device,
                  "settings": {**base_provenance["settings"], "gpu": selected_device},
                  "gpu_enumeration": _process_output_tail(gpu_output)}
    target.parent.mkdir(parents=True, exist_ok=True)
    staged_target = target.with_name(target.stem + ".staging" + target.suffix)
    staged_target.unlink(missing_ok=True)
    command = [executable, "-g", str(selected_device), "--no-tty", "--verbose", "--memory",
               str(model), str(staged_target), "-i", str(SOG_SH_ITERATIONS), "--max-workers", str(SOG_MAX_WORKERS)]
    child_env = os.environ.copy()
    child_env["NODE_OPTIONS"] = (child_env.get("NODE_OPTIONS", "").strip() + f" --max-old-space-size={SOG_NODE_OLD_SPACE_MB}").strip()
    started_at = time.monotonic()
    try:
        converted = subprocess.run(command, capture_output=True, text=True, timeout=SOG_MAX_SECONDS, check=False, env=child_env)
    except subprocess.TimeoutExpired as error:
        elapsed = round(time.monotonic() - started_at, 3)
        staged_target.unlink(missing_ok=True)
        return {**provenance, "status": "TIMEOUT", "elapsed_seconds": elapsed,
                "stdout_tail": _process_output_tail(error.stdout), "stderr_tail": _process_output_tail(error.stderr),
                "reason": f"SOG conversion exceeded {SOG_MAX_SECONDS} seconds; the visitor package uses its PLY fallback."}
    except OSError:
        elapsed = round(time.monotonic() - started_at, 3)
        staged_target.unlink(missing_ok=True)
        return {**provenance, "status": "FAILED", "elapsed_seconds": elapsed,
                "reason": "Could not run splat-transform; the visitor package uses its PLY fallback."}
    elapsed = round(time.monotonic() - started_at, 3)
    if converted.returncode or not staged_target.is_file() or staged_target.stat().st_size <= 0:
        staged_target.unlink(missing_ok=True)
        return {**provenance, "status": "FAILED", "reason": f"splat-transform returned {converted.returncode} or produced no SOG; the visitor package uses its PLY fallback.",
                "returncode": converted.returncode, "elapsed_seconds": elapsed,
                "stdout_tail": _process_output_tail(converted.stdout), "stderr_tail": _process_output_tail(converted.stderr)}
    try:
        info = _tool_info(executable, staged_target)
        sog_meta = _sog_archive_metadata(staged_target, int(info["numGaussians"]))
        source_info = _tool_info(executable, model)
        if int(source_info["numGaussians"]) != int(info["numGaussians"]) or int(source_info.get("shBands", 0)) != int(info.get("shBands", 0)):
            raise ValueError("Converted SOG Gaussian count or SH bands differ from the PLY master")
    except (OSError, subprocess.TimeoutExpired, ValueError) as error:
        staged_target.unlink(missing_ok=True)
        return {**provenance, "status": "FAILED", "elapsed_seconds": elapsed,
                "stdout_tail": _process_output_tail(converted.stdout), "stderr_tail": _process_output_tail(converted.stderr),
                "reason": f"splat-transform output failed SOG validation ({error}); the visitor package uses its PLY fallback."}
    os.replace(staged_target, target)
    output_size = target.stat().st_size
    return {
        **provenance,
        "status": "COMPLETED",
        "elapsed_seconds": elapsed,
        "command": ["splat-transform", "-g", str(selected_device), "--no-tty", "--verbose", "--memory",
                    "accepted-master.ply", "exhibition.sog", "-i", str(SOG_SH_ITERATIONS),
                    "--max-workers", str(SOG_MAX_WORKERS)],
        "output_size_bytes": output_size,
        "compression_ratio_output_to_input": output_size / model.stat().st_size,
        "output_sha256": sha256_file(target),
        "sog_format": sog_meta,
        "sog_info": info,
        "input_gaussian_count": int(source_info["numGaussians"]),
        "input_sh_bands": int(source_info.get("shBands", 0)),
        "stdout_tail": _process_output_tail(converted.stdout),
        "stderr_tail": _process_output_tail(converted.stderr),
    }


def _build_visitor_website(root: Path, config: dict, model_record: dict, review_website: Path) -> dict[str, Any]:
    from .visitor import enclosure_collision_glb, visitor_camera_settings, viewer_transform_node, write_glb

    website = output_path(root, config) / "website-visitor"
    staging = website.with_name("website-visitor.staging")
    if staging.exists():
        shutil.rmtree(staging)
    for folder in ("assets", "config", "vendor", "js", "css"):
        (staging / folder).mkdir(parents=True, exist_ok=True)
    title = str(config.get("viewer", {}).get("title", config["project_id"]))
    source_template = PRODUCT_ROOT / "templates" / "visitor"
    html_template = (source_template / "index.html").read_text(encoding="utf-8")
    public_html = html_template.replace("__EXHIBITION_TITLE__", html.escape(title, quote=True))
    if "__EXHIBITION_TITLE__" in public_html:
        raise ValueError("Visitor viewer title was not rendered")
    (staging / "index.html").write_text(public_html, encoding="utf-8")
    shutil.copy2(source_template / "style.css", staging / "css" / "viewer.css")
    shutil.copy2(source_template / "locales.json", staging / "config" / "locales.json")
    visitor_vendor = source_template / "vendor"
    required_vendor = ("supersplat-viewer.css", "SUPERSPLAT-VIEWER-LICENSE.txt", "PLAYCANVAS-LICENSE.txt", "THIRD_PARTY-NOTICES.md", "runtime.json")
    missing_vendor = [name for name in required_vendor if not (visitor_vendor / name).is_file()]
    if not (visitor_vendor / "visitor-app.js").is_file():
        missing_vendor.append("visitor-app.js")
    if missing_vendor:
        raise ValueError(f"Visitor viewer runtime has not been built; missing: {missing_vendor}. Run npm run build:visitor-viewer.")
    for name in required_vendor:
        shutil.copy2(visitor_vendor / name, staging / "vendor" / name)
    acceptance_build = os.environ.get("EXHIBITION_ARCHIVE_VISITOR_ACCEPTANCE") == "1"
    if acceptance_build:
        visitor_js = (source_template / "app.js").read_text(encoding="utf-8")
        visitor_js = visitor_js.replace("__EXHIBITION_ARCHIVE_VISITOR_ACCEPTANCE__", "true")
        if "__EXHIBITION_ARCHIVE_VISITOR_ACCEPTANCE__" in visitor_js:
            raise ValueError("Visitor acceptance build marker was not rendered")
        (staging / "js" / "viewer.js").write_text(visitor_js, encoding="utf-8")
    else:
        shutil.copy2(visitor_vendor / "visitor-app.js", staging / "js" / "viewer.js")
    shutil.copy2(visitor_vendor / "supersplat-viewer.js", staging / "vendor" / "supersplat-viewer.js")

    model = Path(model_record["path"])
    review_api = review_website / "api"
    envelope = read_json(review_api / "boundary.json")
    stations = read_json(review_api / "stations.json")
    transform_doc = read_json(review_api / "transform.json") if (review_api / "transform.json").is_file() else _visitor_transform(root, config)
    transform_node = viewer_transform_node(transform_doc)
    settings, camera = visitor_camera_settings(envelope, stations, transform_node)
    write_json(staging / "config" / "settings.json", settings)
    collision_data, collision_metadata = enclosure_collision_glb(envelope, transform_node)
    collision_path = staging / "assets" / "enclosure-collision.glb"
    write_glb(collision_path, collision_data)
    collision_metadata.update({"source_model_sha256": model_record["sha256"],
                               "sha256": sha256_file(collision_path), "size_bytes": collision_path.stat().st_size})

    sog_path = staging / "assets" / "exhibition.sog"
    registered_path = output_path(root, config) / SOG_IMPORT_DIRECTORY / "exhibition.sog"
    conversion = None
    if registered_path.exists() or registered_path.with_name("provenance.json").exists():
        conversion = _registered_sog(model, {**model_record, "path": str(registered_path)}, sog_path)
    if conversion is None:
        conversion = _convert_sog(model, sog_path)
    conversion["input_sha256"] = model_record["sha256"]
    primary = "sog" if conversion.get("status") in {"COMPLETED", "VALIDATED_EXTERNAL"} else "ply"
    if primary == "ply":
        sog_path.unlink(missing_ok=True)
        shutil.copy2(model, staging / "assets" / "exhibition.ply")
    viewer_runtime = read_json(visitor_vendor / "runtime.json")
    default_language = str(config.get("viewer", {}).get("default_language", "en"))
    if default_language not in {"en", "zh-Hant", "zh-TW"}:
        default_language = "en"
    metadata = {
        "schema_version": "exhibition-archive.visitor-publication.v1",
        "profile": "visitor",
        "project_id": config["project_id"],
        "title": title,
        "default_language": "zh-Hant" if default_language == "zh-TW" else default_language,
        "release_authorization": "NEEDS_REVIEW",
        "primary_format": primary,
        "models": {
            "sog": {"url": "./assets/exhibition.sog", "filename": "exhibition.sog", "available": primary == "sog"},
            "ply": {"url": "./assets/exhibition.ply" if primary == "ply" else None,
                    "filename": "exhibition.ply", "available": primary == "ply"},
        },
        "master": {"filename": "exhibition.ply", "published_in_visitor_package": primary == "ply",
                   "size_bytes": model.stat().st_size, "sha256": model_record["sha256"],
                   "gaussian_count": model_record.get("gaussian_count"), "selection": model_record.get("selection")},
        "conversion": conversion,
        "collision": {"url": "./assets/enclosure-collision.glb", **collision_metadata},
        "initial_camera": camera,
        "viewer_runtime": viewer_runtime,
        "navigation_policy": "Free XYZ SuperSplat Fly Mode; enclosure-only collision shell derived from the accepted horizontal boundary, floor, and ceiling. No fixed-height Walk Mode or interior-object collisions.",
        "visitor_ui": ["unobtrusive navigation help", "reset view"],
    }
    write_json(staging / "config" / "exhibition.json", metadata)
    model_role = ("The PLY master remains in the project's derived artifact store and is not duplicated into this package."
                  if primary == "sog" else "It is the active visitor model.")
    conversion_note = ""
    if primary != "sog":
        conversion_note = f" SOG conversion status: {conversion.get('status', 'UNKNOWN')}. {conversion.get('reason', 'The package uses its accepted PLY model.')}"
    readme = f"""# {title} — visitor website\n\nThis static package opens directly into the exhibition. It uses the official SuperSplat Viewer {viewer_runtime['viewer']['version']} with an enclosure-only collision mesh. Visitors can fly freely in XYZ, reset to the opening view, and open brief navigation help. The package contains no guided tours, route selectors, diagnostics, geometry overlays, or technical status panels.\n\nThe primary model format is **{primary.upper()}**. {model_role}{conversion_note} `config/exhibition.json` records the accepted master PLY hash, conversion tool and settings, output hashes, Viewer version, and collision dimensions. `config/package-index.json` hashes every payload file except itself.\n\nThe website is modular: replace the selected external model asset, edit `config/settings.json` for the opening view and rendering settings, and edit `config/locales.json` for visitor UI text. See `INTEGRATION.md` for HTTP hosting, nested paths, iframe embedding, and direct use of the model with the official Viewer API. Modular assets require HTTP/HTTPS; double-click `file://` is not supported.\n\nThis website is a static publication candidate, not a Preservation Package. Release authorization remains **NEEDS_REVIEW**. Review artwork identity and rights, navigation, and access policy before any public release.\n"""
    (staging / "README.md").write_text(readme, encoding="utf-8")
    shutil.copy2(PRODUCT_ROOT / "docs" / "visitor-integration.md", staging / "INTEGRATION.md")
    index = [{"path": path.relative_to(staging).as_posix(), "size_bytes": path.stat().st_size, "sha256": sha256_file(path)}
             for path in sorted(staging.rglob("*")) if path.is_file()]
    write_json(staging / "config" / "package-index.json", {
        "schema_version": "exhibition-archive.website-index.v2",
        "project_id": config["project_id"],
        "model_sha256": model_record["sha256"],
        "profile": "visitor",
        "file_count": len(index),
        "total_bytes": sum(row["size_bytes"] for row in index),
        "files": index,
    })
    if website.exists():
        shutil.rmtree(website)
    staging.replace(website)
    published_collision = website / "assets" / "enclosure-collision.glb"
    return {
        "website": str(website),
        "primary_format": primary,
        "sog_status": conversion.get("status"),
        "sog_size_bytes": conversion.get("output_size_bytes"),
        "sog_sha256": conversion.get("output_sha256"),
        "collision_size_bytes": published_collision.stat().st_size,
        "collision_sha256": collision_metadata["sha256"],
        "total_bytes": sum(row["size_bytes"] for row in index),
        "published_model_asset": f"assets/exhibition.{primary}",
        "package_index": str(website / "config" / "package-index.json"),
    }


def build_website(root: Path, config: dict, state, output_dir: Path) -> dict:
    website = output_path(root, config) / "website"
    staging = website.with_name("website.staging")
    if staging.exists():
        shutil.rmtree(staging)
    for folder in ("viewer", "viewer/vendor", "assets", "api", "config"):
        (staging / folder).mkdir(parents=True, exist_ok=True)
    source_viewer = PRODUCT_ROOT / "templates" / "viewer"
    title = str(config.get("viewer", {}).get("title", config["project_id"]))
    html_template = (source_viewer / "index.html").read_text(encoding="utf-8")
    if "__EXHIBITION_TITLE__" not in html_template or "__PROJECT_ID__" not in html_template:
        raise ValueError("Public viewer template is missing its metadata placeholders")
    public_html = html_template.replace("__EXHIBITION_TITLE__", html.escape(title, quote=True))
    public_html = public_html.replace("__PROJECT_ID__", html.escape(str(config["project_id"]), quote=True))
    if "__EXHIBITION_TITLE__" in public_html or "__PROJECT_ID__" in public_html:
        raise ValueError("Public viewer metadata placeholders were not fully rendered")
    (staging / "viewer" / "index.html").write_text(public_html, encoding="utf-8")
    for name in ("app.js", "style.css"):
        shutil.copy2(source_viewer / name, staging / "viewer" / name)
    for name in ("index.js", "index.css"):
        shutil.copy2(source_viewer / "vendor" / name, staging / "viewer" / "vendor" / name)

    from .model_registry import selected_model_record
    model_record = selected_model_record(root, config, state)
    baseline = Path(model_record["path"])
    target_model = staging / "assets" / "exhibition.ply"
    shutil.copy2(baseline, target_model)
    _static_api(root, config, staging, model_record, state)

    root_page = f"""<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><meta name=\"color-scheme\" content=\"dark\"><link rel=\"icon\" href=\"./favicon.ico\" type=\"image/x-icon\"><meta http-equiv=\"refresh\" content=\"0;url=viewer/\"><title>{html.escape(title, quote=True)}</title></head><body><p><a href=\"viewer/\">Open the exhibition archive viewer</a></p></body></html>"""
    (staging / "index.html").write_text(root_page, encoding="utf-8")
    icon_image = struct.pack("<IiiHHIIiiII", 40, 1, 2, 1, 32, 0, 4, 0, 0, 0, 0) + bytes((22, 43, 28, 255)) + bytes(4)
    icon_entry = struct.pack("<BBBBHHII", 1, 1, 0, 0, 1, 32, len(icon_image), 22)
    (staging / "favicon.ico").write_bytes(b"\x00\x00\x01\x00\x01\x00" + icon_entry + icon_image)
    artwork_config = config.get("integrations", {}).get("artwork", {})
    confirmed_path = project_path(root, config, artwork_config["confirmed_artworks"]) if artwork_config.get("confirmed_artworks") else output_path(root, config) / "work" / "confirmed-artworks.json"
    confirmed_count = len(read_json(confirmed_path).get("artworks", []))
    readme = f"""# {title} — static archive website

This directory is a deployable static website. Upload its contents to any static HTTP host while preserving the directory structure. It uses bundled PlayCanvas assets and relative URLs; visitors need no Exhibition Archive Engine or Python runtime.

The selected model SHA-256 is `{model_record["sha256"]}`. The package index at `config/package-index.json` lists every payload file except the index itself. Run `exhibition-archive verify --full-hash <workspace>` to verify the package in its project workspace before deployment.

The site excludes original photographs and private review records. The public catalogue contains only reviewer-confirmed artwork records; current confirmed count: {confirmed_count}. A static visitor website is not a self-contained Preservation Package. Public release and rights approval remain separate human decisions.
"""
    (staging / "README.md").write_text(readme, encoding="utf-8")
    license_text = """MIT License

Copyright (c) 2011-2026 PlayCanvas Ltd.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

The bundled viewer runtime is the accepted repository-local PlayCanvas / SuperSplat viewer distribution. Upstream reference: https://github.com/playcanvas/supersplat-viewer
Engine version detected in the bundle: 2.23.0. The upstream Viewer is MIT-licensed. The exhibition artwork and source photographs have separate project-specific rights and are not covered by this software license.
"""
    (staging / "viewer" / "vendor" / "LICENSE.txt").write_text(license_text, encoding="utf-8")
    # The index excludes itself and covers every deployable payload file.
    index = [{"path": p.relative_to(staging).as_posix(), "size_bytes": p.stat().st_size, "sha256": sha256_file(p)}
             for p in sorted(staging.rglob("*")) if p.is_file()]
    write_json(staging / "config" / "package-index.json", {
        "schema_version": "exhibition-archive.website-index.v2",
        "project_id": config["project_id"],
        "model_sha256": model_record["sha256"],
        "file_count": len(index),
        "total_bytes": sum(row["size_bytes"] for row in index),
        "files": index,
    })
    if website.exists():
        shutil.rmtree(website)
    staging.replace(website)
    packaged_model = website / "assets" / "exhibition.ply"
    from .model_registry import selected_model_record
    model_record = selected_model_record(root, config, state)
    visitor_details = _build_visitor_website(root, config, model_record, website)
    visitor_website = Path(visitor_details["website"])
    visitor_model = visitor_website / "assets" / f"exhibition.{visitor_details['primary_format']}"
    return {"status": "COMPLETED", "artifacts": [website / "index.html", website / "viewer" / "app.js", packaged_model,
                                                    website / "api" / "initial-camera.json",
                                                    website / "config" / "navigation.json", website / "config" / "package-index.json",
                                                    visitor_website / "index.html", visitor_website / "js" / "viewer.js",
                                                    visitor_website / "css" / "viewer.css",
                                                    visitor_website / "config" / "settings.json",
                                                    visitor_website / "config" / "exhibition.json",
                                                    visitor_model, visitor_website / "assets" / "enclosure-collision.glb",
                                                    visitor_website / "config" / "package-index.json"],
            "details": {"website": str(website), "review_website": str(website), "visitor_website": str(visitor_website),
                        "model_size_bytes": packaged_model.stat().st_size, "model_sha256": sha256_file(packaged_model),
                        "public_artwork_count": confirmed_count, "viewer_origin": "Review: existing diagnostic PlayCanvas viewer; Visitor: official SuperSplat Viewer",
                        "visitor": visitor_details, "source_photographs_included": False}}
