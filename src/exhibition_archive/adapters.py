from __future__ import annotations

import hashlib
import json
import re
import struct
from pathlib import Path
from typing import Any


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: str | Path, value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(target)


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def colmap_binary_counts(directory: str | Path) -> dict[str, Any]:
    """Read-only structural validation of COLMAP cameras.bin/images.bin/points3D.bin."""
    root = Path(directory)
    cameras_path, images_path, points_path = (root / n for n in ("cameras.bin", "images.bin", "points3D.bin"))
    missing = [str(p) for p in (cameras_path, images_path, points_path) if not p.is_file()]
    if missing:
        raise ValueError(f"COLMAP reconstruction is incomplete; missing {missing}")
    with cameras_path.open("rb") as f:
        camera_count = struct.unpack("<Q", f.read(8))[0]
        cameras = {}
        for _ in range(camera_count):
            camera_id, model_id, width, height = struct.unpack("<iiQQ", f.read(24))
            param_count = {0:3,1:4,2:4,3:5,4:8,5:8,6:12,7:5,8:4,9:5,10:12,11:16}.get(model_id)
            if param_count is None:
                raise ValueError(f"Unsupported/unknown COLMAP camera model id {model_id} in {cameras_path}")
            params = struct.unpack(f"<{param_count}d", f.read(param_count * 8))
            cameras[camera_id] = {"model_id": model_id, "width": width, "height": height, "parameter_count": param_count, "parameters_finite": all(map(lambda x: x == x and abs(x) != float("inf"), params))}
    images = {}
    with images_path.open("rb") as f:
        image_count = struct.unpack("<Q", f.read(8))[0]
        for _ in range(image_count):
            image_id = struct.unpack("<I", f.read(4))[0]
            f.read(56)  # quaternion and translation
            camera_id = struct.unpack("<I", f.read(4))[0]
            name_bytes = bytearray()
            while True:
                b = f.read(1)
                if not b or b == b"\0":
                    break
                name_bytes.extend(b)
            name = name_bytes.decode("utf-8", errors="replace")
            npoints = struct.unpack("<Q", f.read(8))[0]
            f.seek(npoints * 24, 1)
            if camera_id not in cameras:
                raise ValueError(f"Registered image {name} refers to missing camera id {camera_id}")
            images[image_id] = {"name": name, "camera_id": camera_id, "points2d": npoints}
    with points_path.open("rb") as f:
        point_count = struct.unpack("<Q", f.read(8))[0]
        for _ in range(point_count):
            f.read(43)  # xyz doubles, rgb bytes, reprojection error double
            track_length = struct.unpack("<Q", f.read(8))[0]
            f.seek(track_length * 8, 1)  # image id + point2D index
    return {
        "format": "COLMAP_BINARY",
        "directory": str(root.resolve()),
        "camera_count": camera_count,
        "registered_image_count": image_count,
        "sparse_point_count": point_count,
        "camera_ids": sorted(cameras),
        "registered_image_camera_ids": sorted({x["camera_id"] for x in images.values()}),
        "all_camera_parameters_finite": all(x["parameters_finite"] for x in cameras.values()),
        "all_registered_images_reference_known_cameras": True,
        "registered_image_name_sample": [images[k]["name"] for k in list(images)[:5]],
        "validation": "All three binary files parsed to their declared records; image camera references resolve; no reconstruction was run or changed.",
    }


def ply_identity(path: str | Path) -> dict[str, Any]:
    file = Path(path)
    with file.open("rb") as f:
        header = bytearray()
        while len(header) < 1024 * 1024:
            line = f.readline()
            if not line:
                raise ValueError("PLY header ended before end_header")
            header.extend(line)
            if line.strip() == b"end_header":
                break
    text = header.decode("ascii", errors="replace")
    match = re.search(r"^element vertex (\d+)$", text, re.MULTILINE)
    if not text.startswith("ply\n") or match is None:
        raise ValueError("PLY header is invalid or has no element vertex count")
    return {"path": str(file.resolve()), "size_bytes": file.stat().st_size, "gaussian_count": int(match.group(1)), "sha256": sha256_file(file), "format_header": text.splitlines()[:3]}


def validate_navigation(envelope_path: str | Path, policy_path: str | Path, expected_model_sha256: str | None) -> dict[str, Any]:
    envelope, policy = read_json(envelope_path), read_json(policy_path)
    h = envelope.get("horizontalBoundary", {})
    vertices = h.get("vertices2d")
    if not envelope.get("id") or not isinstance(vertices, list) or len(vertices) < 3:
        raise ValueError("Navigation envelope lacks a named closed polygon")
    if h.get("closed") is not True or h.get("cameraStationInclusion", {}).get("insideOrOn") != h.get("cameraStationInclusion", {}).get("total"):
        raise ValueError("Navigation polygon closure or trusted station inclusion gate failed")
    if policy.get("sourceModelSha256") != expected_model_sha256:
        raise ValueError("Navigation source model hash does not match selected model")
    collision = policy.get("navigation", {}).get("interiorObjectCollision", policy.get("disabledRestrictions", {}).get("interiorFurnitureOrArtworkCollision"))
    if collision is not False:
        raise ValueError("Unexpected interior-object collision policy")
    return {
        "status": "ADOPTED_VALIDATED",
        "envelope_id": envelope["id"],
        "algorithm": h.get("algorithm"),
        "vertex_count": len(vertices),
        "camera_station_inclusion": h.get("cameraStationInclusion"),
        "camera_clearance_units": h.get("cameraClearanceUnits"),
        "floor_classification": envelope.get("floor", {}).get("classification"),
        "ceiling_classification": envelope.get("ceiling", {}).get("classification"),
        "movement_policy": policy,
        "coordinate_frame": envelope.get("coordinateFrame"),
        "limitations": envelope.get("limitations", []),
        "source_files": [str(Path(envelope_path).resolve()), str(Path(policy_path).resolve())],
    }


def public_json(value: Any) -> Any:
    """Remove host-specific and private provenance strings from a public JSON tree."""
    private_keys = ("path", "source", "provenance", "notes", "uncertainty", "limitation", "reviewer",
                    "reviewed_by", "reviewed_at_utc", "review_status", "rationale", "imagekey", "image_id", "imageid")
    if isinstance(value, dict):
        out = {}
        for key, child in value.items():
            if any(token in key.casefold() for token in private_keys):
                continue
            out[key] = public_json(child)
        return out
    if isinstance(value, list):
        return [public_json(x) for x in value]
    if isinstance(value, str):
        if value.startswith("/") or re.match(r"^[A-Za-z]:\\", value):
            return "[omitted local path]"
        value = re.sub(r"IMG_\d{3,}(?:\s+\d+)?", "source-image", value)
        value = re.sub(r"Phase 2E[A-Z]", "archive workflow", value)
    return value
