from __future__ import annotations

import json
import math
import struct
from pathlib import Path
from typing import Any


VIEWER_VERSION = "1.42.0"
VIEWER_DEFAULT_AXIS_FLIP = (0.0, 0.0, 1.0, 0.0)  # 180 degrees about Z in SuperSplat Viewer 1.42.0.
CAMERA_COLLISION_RADIUS = 0.2  # Current official FlyController constant, in viewer units.


def _vector(value: Any, name: str) -> tuple[float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"{name} must be a three-component vector")
    result = tuple(float(x) for x in value)
    if not all(math.isfinite(x) for x in result):
        raise ValueError(f"{name} contains a non-finite value")
    return result


def _add(a, b):
    return tuple(x + y for x, y in zip(a, b))


def _sub(a, b):
    return tuple(x - y for x, y in zip(a, b))


def _scale(a, factor):
    return tuple(x * factor for x in a)


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _length(a):
    return math.sqrt(_dot(a, a))


def _unit(a, name):
    length = _length(a)
    if length < 1e-12:
        raise ValueError(f"{name} must have non-zero length")
    return _scale(a, 1.0 / length)


def _quat_rotate(q, point):
    x, y, z, w = q
    px, py, pz = point
    # q * p * conjugate(q), expanded to avoid intermediate allocations.
    tx = 2.0 * (y * pz - z * py)
    ty = 2.0 * (z * px - x * pz)
    tz = 2.0 * (x * py - y * px)
    return (
        px + w * tx + (y * tz - z * ty),
        py + w * ty + (z * tx - x * tz),
        pz + w * tz + (x * ty - y * tx),
    )


def _quat_multiply(a, b):
    """Return the quaternion that applies ``b`` and then ``a``."""
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    )


def _quaternion_from_to(source, target=(0.0, 1.0, 0.0)):
    """Return a unit quaternion mapping one direction to another."""
    start = _unit(_vector(source, "up direction"), "up direction")
    end = _unit(_vector(target, "viewer up direction"), "viewer up direction")
    cosine = max(-1.0, min(1.0, _dot(start, end)))
    axis = (start[1] * end[2] - start[2] * end[1],
            start[2] * end[0] - start[0] * end[2],
            start[0] * end[1] - start[1] * end[0])
    if cosine < -1.0 + 1e-10:
        reference = (1.0, 0.0, 0.0) if abs(start[0]) < 0.9 else (0.0, 0.0, 1.0)
        axis = (start[1] * reference[2] - start[2] * reference[1],
                start[2] * reference[0] - start[0] * reference[2],
                start[0] * reference[1] - start[1] * reference[0])
        axis = _unit(axis, "up alignment axis")
        return (*axis, 0.0)
    q = (*axis, 1.0 + cosine)
    norm = math.sqrt(sum(component * component for component in q))
    return tuple(component / norm for component in q)


def _floor_up(envelope: dict):
    floor = envelope.get("floor", {})
    return _unit(_vector(floor.get("normal", [0.0, 1.0, 0.0]), "envelope.floor.normal"),
                 "envelope.floor.normal")


def _camera_enclosure_clearance(envelope: dict, position: tuple[float, float, float]) -> dict | None:
    """Validate the opening station against the accepted room shell when provided."""
    floor = envelope.get("floor", {})
    ceiling = envelope.get("ceiling", {})
    boundary = envelope.get("horizontalBoundary", {})
    basis = boundary.get("planeBasis", {})
    polygon = boundary.get("vertices2d")
    if (not all(key in floor for key in ("normal", "offset", "clearance")) or
            not all(key in ceiling for key in ("heightAlongFloorNormal", "clearance")) or
            not isinstance(polygon, list) or len(polygon) < 3 or not basis):
        return None
    normal = _unit(_vector(floor["normal"], "envelope.floor.normal"), "envelope.floor.normal")
    height = _dot(normal, position) + float(floor["offset"])
    floor_margin = height - float(floor["clearance"])
    ceiling_margin = float(ceiling["heightAlongFloorNormal"]) - float(ceiling["clearance"]) - height
    plane_point = _vector(basis.get("pointOnPlaneCommonViewer"), "horizontalBoundary plane point")
    u = _unit(_vector(basis.get("basisU"), "horizontalBoundary basisU"), "basisU")
    v = _unit(_vector(basis.get("basisV"), "horizontalBoundary basisV"), "basisV")
    delta = _sub(position, plane_point)
    point_uv = (_dot(delta, u), _dot(delta, v))
    points = [tuple(float(value) for value in point) for point in polygon]
    area2 = sum(points[i][0] * points[(i + 1) % len(points)][1] -
                points[(i + 1) % len(points)][0] * points[i][1] for i in range(len(points)))
    orientation = 1.0 if area2 >= 0 else -1.0
    signed_edge_distances = []
    for index, start in enumerate(points):
        end = points[(index + 1) % len(points)]
        dx, dy = end[0] - start[0], end[1] - start[1]
        edge_length = math.hypot(dx, dy)
        if edge_length < 1e-10:
            raise ValueError("accepted horizontal boundary contains a zero-length edge")
        signed_edge_distances.append(orientation * (dx * (point_uv[1] - start[1]) - dy * (point_uv[0] - start[0])) / edge_length)
    wall_distance = min(signed_edge_distances)
    wall_margin = wall_distance - float(boundary.get("cameraClearanceUnits", 0.0))
    if min(floor_margin, ceiling_margin, wall_margin) < -1e-6:
        raise ValueError("the accepted default station is outside the camera-clearance envelope")
    return {
        "floor_clearance_common_units": floor_margin,
        "ceiling_clearance_common_units": ceiling_margin,
        "nearest_wall_distance_common_units": wall_distance,
        "accepted_wall_clearance_common_units": float(boundary.get("cameraClearanceUnits", 0.0)),
        "minimum_wall_margin_common_units": wall_margin,
    }


def _rotate_position(point, quaternion):
    return _quat_rotate(quaternion, _vector(point, "common position"))


def common_to_supersplat(point, up_alignment_quaternion):
    """Map accepted common-frame positions into SuperSplat's Y-up world frame."""
    return _rotate_position(point, up_alignment_quaternion)


def supersplat_to_common(point, up_alignment_quaternion):
    """Map SuperSplat Y-up world positions back into the accepted common frame."""
    q = tuple(float(value) for value in up_alignment_quaternion)
    return _quat_rotate((-q[0], -q[1], -q[2], q[3]), _vector(point, "SuperSplat position"))


def supersplat_scene_transform(transform_node: dict, up_alignment_quaternion):
    """Compose the accepted model-to-common transform with the Y-up alignment."""
    node = viewer_transform_node(transform_node)
    align = tuple(float(value) for value in up_alignment_quaternion)
    rotation = _quat_multiply(align, tuple(node["rotationQuaternionXYZW"]))
    rotation_length = math.sqrt(sum(value * value for value in rotation))
    rotation = tuple(value / rotation_length for value in rotation)
    translation = _quat_rotate(align, node["translation"])
    return {"translation": list(translation), "rotationQuaternionXYZW": list(rotation),
            "uniformScale": node["uniformScale"]}


def viewer_transform_node(transform: dict | None) -> dict:
    """Return a validated Phase 4D source-to-common scene-node transform or identity."""
    if not isinstance(transform, dict):
        return {"translation": [0.0, 0.0, 0.0], "rotationQuaternionXYZW": [0.0, 0.0, 0.0, 1.0], "uniformScale": 1.0}
    node = transform.get("viewerSceneNode")
    if node is None and "translation" in transform and "uniformScale" in transform:
        node = transform
    if node is None and isinstance(transform.get("transform"), dict):
        node = transform["transform"].get("viewerSceneNode")
    if node is None:
        return {"translation": [0.0, 0.0, 0.0], "rotationQuaternionXYZW": [0.0, 0.0, 0.0, 1.0], "uniformScale": 1.0}
    if not isinstance(node, dict):
        raise ValueError("viewerSceneNode must be an object")
    translation = _vector(node.get("translation"), "viewerSceneNode.translation")
    raw_q = node.get("rotationQuaternionXYZW", node.get("quaternionXYZW"))
    if not isinstance(raw_q, (list, tuple)) or len(raw_q) != 4:
        raise ValueError("viewerSceneNode rotation quaternion must have four components")
    q = tuple(float(x) for x in raw_q)
    q_len = math.sqrt(sum(x * x for x in q))
    if not math.isfinite(q_len) or q_len < 1e-12:
        raise ValueError("viewerSceneNode rotation quaternion must be finite and non-zero")
    q = tuple(x / q_len for x in q)
    uniform_scale = float(node.get("uniformScale"))
    if not math.isfinite(uniform_scale) or uniform_scale <= 0:
        raise ValueError("viewerSceneNode.uniformScale must be finite and positive")
    return {"translation": list(translation), "rotationQuaternionXYZW": list(q), "uniformScale": uniform_scale}


def common_to_viewer(point: Any, transform_node: dict) -> tuple[float, float, float]:
    """Map accepted common-viewer coordinates into the official Viewer PLY-local frame.

    The accepted model transform is ``common = D * (scale * R * native + translation)``.
    SuperSplat Viewer 1.42.0 applies the default ``D = Rz(180 degrees)`` to PLY/SOG
    entities when no custom scene transform is supplied. Inverting the accepted
    similarity transform and then applying D makes camera and collision coordinates
    agree with the unmodified master PLY loaded by the supported Viewer API.
    """
    common = _vector(point, "common point")
    d_common = (-common[0], -common[1], common[2])
    translation = _vector(transform_node["translation"], "translation")
    scale = float(transform_node["uniformScale"])
    q = tuple(float(x) for x in transform_node["rotationQuaternionXYZW"])
    pre_rotate = _scale(_sub(d_common, translation), 1.0 / scale)
    inverse_q = (-q[0], -q[1], -q[2], q[3])
    native = _quat_rotate(inverse_q, pre_rotate)
    return (-native[0], -native[1], native[2])


def _offset_convex_polygon(points: list[tuple[float, float]], distance: float) -> list[tuple[float, float]]:
    if len(points) < 3:
        raise ValueError("navigation enclosure must contain at least three vertices")
    area2 = sum(points[i][0] * points[(i + 1) % len(points)][1] - points[(i + 1) % len(points)][0] * points[i][1]
                for i in range(len(points)))
    if abs(area2) < 1e-10:
        raise ValueError("navigation enclosure has zero area")
    orientation = 1.0 if area2 > 0 else -1.0
    if orientation < 0:
        points = list(reversed(points))
        orientation = 1.0
    edge_normals = []
    for i, start in enumerate(points):
        end = points[(i + 1) % len(points)]
        dx, dy = end[0] - start[0], end[1] - start[1]
        length = math.hypot(dx, dy)
        if length < 1e-10:
            raise ValueError("navigation enclosure contains a zero-length edge")
        edge_normals.append((orientation * dy / length, -orientation * dx / length))
    result = []
    for i, point in enumerate(points):
        before, after = edge_normals[i - 1], edge_normals[i]
        denom = 1.0 + before[0] * after[0] + before[1] * after[1]
        if denom < 1e-5:
            raise ValueError("navigation enclosure has an unstable offset corner")
        result.append((point[0] + distance * (before[0] + after[0]) / denom,
                       point[1] + distance * (before[1] + after[1]) / denom))
    return result


def _glb_from_mesh(positions: list[tuple[float, float, float]], indices: list[int]) -> bytes:
    if not positions or not indices or len(indices) % 3:
        raise ValueError("collision mesh requires vertices and triangle indices")
    if any(index < 0 or index >= len(positions) for index in indices):
        raise ValueError("collision mesh contains an invalid vertex index")
    position_bytes = b"".join(struct.pack("<3f", *point) for point in positions)
    while len(position_bytes) % 4:
        position_bytes += b"\x00"
    index_offset = len(position_bytes)
    index_bytes = b"".join(struct.pack("<I", int(index)) for index in indices)
    binary = position_bytes + index_bytes
    mins = [min(point[i] for point in positions) for i in range(3)]
    maxs = [max(point[i] for point in positions) for i in range(3)]
    document = {
        "asset": {"version": "2.0", "generator": "Exhibition Archive Phase 4D enclosure collision"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": "Accepted exhibition enclosure"}],
        "meshes": [{"name": "Enclosure shell only", "primitives": [{"attributes": {"POSITION": 0}, "indices": 1, "mode": 4}]}],
        "buffers": [{"byteLength": len(binary)}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(position_bytes), "target": 34962},
            {"buffer": 0, "byteOffset": index_offset, "byteLength": len(index_bytes), "target": 34963},
        ],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": len(positions), "type": "VEC3", "min": mins, "max": maxs},
            {"bufferView": 1, "componentType": 5125, "count": len(indices), "type": "SCALAR"},
        ],
    }
    json_chunk = json.dumps(document, separators=(",", ":")).encode("utf-8")
    json_chunk += b" " * ((4 - len(json_chunk) % 4) % 4)
    binary += b"\x00" * ((4 - len(binary) % 4) % 4)
    total = 12 + 8 + len(json_chunk) + 8 + len(binary)
    return (struct.pack("<III", 0x46546C67, 2, total)
            + struct.pack("<II", len(json_chunk), 0x4E4F534A) + json_chunk
            + struct.pack("<II", len(binary), 0x004E4942) + binary)


def enclosure_collision_glb(envelope: dict, transform_node: dict | None = None,
                            camera_radius: float = CAMERA_COLLISION_RADIUS) -> tuple[bytes, dict]:
    """Build the accepted enclosure in official SuperSplat's Y-up world frame."""
    node = viewer_transform_node(transform_node)
    up_alignment = _quaternion_from_to(_floor_up(envelope))
    horizontal = envelope.get("horizontalBoundary", {})
    vertices = horizontal.get("vertices2d")
    basis = horizontal.get("planeBasis", {})
    if not isinstance(vertices, list) or not basis:
        raise ValueError("accepted enclosure must provide horizontalBoundary.vertices2d and planeBasis")
    uv = [tuple(float(component) for component in point) for point in vertices]
    if any(len(point) != 2 or not all(math.isfinite(component) for component in point) for point in uv):
        raise ValueError("accepted enclosure vertices must be finite 2D points")
    floor = envelope.get("floor", {})
    ceiling = envelope.get("ceiling", {})
    normal = _unit(_vector(floor.get("normal"), "envelope.floor.normal"), "floor normal")
    base = _vector(basis.get("pointOnPlaneCommonViewer"), "horizontalBoundary plane point")
    u = _unit(_vector(basis.get("basisU"), "horizontalBoundary basisU"), "basisU")
    v = _unit(_vector(basis.get("basisV"), "horizontalBoundary basisV"), "basisV")
    if abs(_dot(u, v)) > 1e-5 or abs(_dot(u, normal)) > 1e-5 or abs(_dot(v, normal)) > 1e-5:
        raise ValueError("accepted enclosure basis is not orthonormal to the floor normal")
    # Collision and camera coordinates share the accepted common units after the
    # rigid Y-up rotation. The Viewer radius is therefore not multiplied by the
    # model's local-to-common scale.
    radius_common = float(camera_radius)
    floor_clearance = float(floor.get("clearance", 0.0))
    ceiling_clearance = float(ceiling.get("clearance", 0.0))
    ceiling_height = float(ceiling.get("heightAlongFloorNormal"))
    if not all(math.isfinite(x) for x in (radius_common, floor_clearance, ceiling_clearance, ceiling_height)):
        raise ValueError("accepted floor, ceiling, or Viewer collision dimensions are invalid")
    edge_clearance = float(horizontal.get("cameraClearanceUnits", 0.0))
    outboard = radius_common - edge_clearance
    if outboard <= 0:
        raise ValueError("the Viewer collision radius must exceed the accepted horizontal clearance")
    expanded = _offset_convex_polygon(uv, outboard)
    low_height = floor_clearance - radius_common
    high_height = ceiling_height - ceiling_clearance + radius_common
    if high_height <= low_height:
        raise ValueError("accepted vertical enclosure is too short for the Viewer collision sphere")

    def common_point(point_uv, height):
        return _add(_add(base, _add(_scale(u, point_uv[0]), _scale(v, point_uv[1]))), _scale(normal, height))

    lower = [common_point(point, low_height) for point in expanded]
    upper = [common_point(point, high_height) for point in expanded]
    center_low = _scale(tuple(sum(point[i] for point in lower) for i in range(3)), 1.0 / len(lower))
    center_high = _scale(tuple(sum(point[i] for point in upper) for i in range(3)), 1.0 / len(upper))
    common_positions = lower + upper + [center_low, center_high]
    count = len(expanded)
    indices: list[int] = []
    # Side walls: the polygon is CCW in the floor basis; each side faces outward.
    for i in range(count):
        j = (i + 1) % count
        indices.extend((i, j, count + j, i, count + j, count + i))
    # Floor faces down; ceiling faces up.
    floor_center, ceiling_center = 2 * count, 2 * count + 1
    for i in range(count):
        j = (i + 1) % count
        indices.extend((floor_center, j, i))
        indices.extend((ceiling_center, count + i, count + j))
    positions = [_rotate_position(point, up_alignment) for point in common_positions]
    glb = _glb_from_mesh(positions, indices)
    metadata = {
        "source": "accepted camera-derived horizontal boundary, accepted floor and ceiling only",
        "source_model_sha256": envelope.get("horizontalBoundary", {}).get("sourceModelSha256"),
        "viewer_version": VIEWER_VERSION,
        "camera_radius_viewer_units": camera_radius,
        "accepted_edge_clearance_common_units": edge_clearance,
        "expanded_shell_outboard_common_units": outboard,
        "floor_collision_height_common_units": low_height,
        "ceiling_collision_height_common_units": high_height,
        "vertex_count": len(positions),
        "triangle_count": len(indices) // 3,
        "contains_interior_objects": False,
        "coordinate_frame": "SuperSplat Y-up world frame; the accepted common-viewer frame is rigidly rotated so its floor normal maps to +Y",
        "common_to_supersplat_rotation_quaternion_xyzw": list(up_alignment),
        "model_scene_transform_supersplat": supersplat_scene_transform(node, up_alignment),
        "coordinate_transform": "The accepted enclosure is rotated into SuperSplat Y-up world. The model uses the accepted viewerSceneNode composed with the same rotation; the immutable source model is not rewritten.",
    }
    return glb, metadata


def canonical_initial_camera(envelope: dict, stations: dict, transform_node: dict | None = None) -> dict:
    """Return the shared opening camera used by Review and Visitor profiles."""
    node = viewer_transform_node(transform_node)
    station_id = stations.get("defaultStationId")
    station = next((row for row in stations.get("stations", []) if row.get("stationId") == station_id), None)
    if not station:
        station = next((row for row in stations.get("stations", []) if row.get("normalVisitorPositionCandidate")), None)
    if not station and stations.get("stations"):
        station = stations["stations"][0]
    if not station:
        raise ValueError("accepted navigation has no trusted station for an initial visitor camera")
    position_common = _vector(station.get("centerCommonViewer"), "default station center")
    forward_common = _unit(_vector(station.get("representativeForwardCommonViewer"), "default station forward"), "station forward")
    target_common = _add(position_common, _scale(forward_common, 2.0))
    up_common = _floor_up(envelope)
    enclosure_clearance = _camera_enclosure_clearance(envelope, position_common)
    up_alignment = _quaternion_from_to(up_common)
    pose_common = {"position": list(position_common), "target": list(target_common),
                   "up": list(up_common), "fov": 70.75}
    pose_supersplat = {
        "position": list(common_to_supersplat(position_common, up_alignment)),
        "target": list(common_to_supersplat(target_common, up_alignment)),
        "fov": pose_common["fov"],
    }
    return {
        "schema_version": 1,
        "source_station_id": station.get("stationId"),
        "coordinate_frame": "accepted archive common viewer frame",
        "pose_common_viewer": pose_common,
        "viewer_coordinate_frame": "SuperSplat Y-up world frame",
        "pose_supersplat": pose_supersplat,
        "common_to_supersplat_rotation_quaternion_xyzw": list(up_alignment),
        "model_scene_transform_common": node,
        "model_scene_transform_supersplat": supersplat_scene_transform(node, up_alignment),
        "enclosure_clearance": enclosure_clearance,
        "derivation": "The accepted default station supplies position and representative forward. The target is two common-frame units along that direction; up is the accepted floor normal; FOV is 70.75 degrees.",
    }


def visitor_camera_settings(envelope: dict, stations: dict, transform_node: dict | None = None) -> tuple[dict, dict]:
    canonical = canonical_initial_camera(envelope, stations, transform_node)
    pose = canonical["pose_supersplat"]
    settings = {
        "version": 2,
        "tonemapping": "none",
        "highPrecisionRendering": False,
        "background": {"color": [0.012, 0.016, 0.013]},
        "postEffectSettings": {
            "sharpness": {"enabled": False, "amount": 0},
            "bloom": {"enabled": False, "intensity": 0.1, "blurLevel": 2},
            "grading": {"enabled": False, "brightness": 1, "contrast": 1, "saturation": 1, "tint": [1, 1, 1]},
            "vignette": {"enabled": False, "intensity": 0.5, "inner": 0.3, "outer": 0.75, "curvature": 1},
            "fringing": {"enabled": False, "intensity": 0.5},
        },
        "animTracks": [],
        "cameras": [{"initial": pose}],
        "annotations": [],
        "startMode": "default",
    }
    return settings, {
        "station_id": canonical["source_station_id"],
        "coordinate_frame": canonical["viewer_coordinate_frame"],
        "pose_common_viewer": canonical["pose_common_viewer"],
        "pose_supersplat": pose,
        "up_supersplat": [0.0, 1.0, 0.0],
        "common_to_supersplat_rotation_quaternion_xyzw": canonical["common_to_supersplat_rotation_quaternion_xyzw"],
        "model_scene_transform_supersplat": canonical["model_scene_transform_supersplat"],
    }


def write_glb(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
