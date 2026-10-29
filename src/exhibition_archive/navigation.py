"""Conservative, dependency-free navigation proposal generation and validation.

Coordinates in the public navigation contract are floor-aligned, non-metric
scene units. Camera positions are transformed into the viewer frame once, then
projected into a local (u, v, height) frame whose up vector is derived from the
provided floor plane.
"""
from __future__ import annotations

import math
import struct
import copy
from collections import Counter, defaultdict, deque
from pathlib import Path
from statistics import median
from typing import Any, Iterable


EPS = 1e-9


def _finite_vector(value: Any, size: int, label: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != size:
        raise ValueError(f"{label} must contain {size} finite numbers")
    result = [float(x) for x in value]
    if not all(math.isfinite(x) for x in result):
        raise ValueError(f"{label} must contain {size} finite numbers")
    return result


def _normalize(v: list[float]) -> list[float]:
    length = math.sqrt(sum(x * x for x in v))
    if length <= EPS:
        raise ValueError("Cannot normalize a zero-length vector")
    return [x / length for x in v]


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _cross(a: list[float], b: list[float]) -> list[float]:
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def quaternion_wxyz_to_rotation(qvec: Iterable[float]) -> list[list[float]]:
    """Return COLMAP's world-to-camera rotation matrix from quaternion (w,x,y,z)."""
    q = _finite_vector(list(qvec), 4, "COLMAP quaternion")
    norm = math.sqrt(sum(x * x for x in q))
    if norm <= EPS:
        raise ValueError("COLMAP quaternion has zero norm")
    w, x, y, z = [v / norm for v in q]
    return [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]


def colmap_camera_center(qvec_wxyz: Iterable[float], tvec: Iterable[float]) -> list[float]:
    """Convert COLMAP world-to-camera pose to its camera center: C = -R^T t."""
    rotation = quaternion_wxyz_to_rotation(qvec_wxyz)
    t = _finite_vector(list(tvec), 3, "COLMAP translation")
    return [-sum(rotation[row][column] * t[row] for row in range(3)) for column in range(3)]


def _read_c_string(stream) -> str:
    value = bytearray()
    while True:
        char = stream.read(1)
        if not char:
            raise ValueError("Unexpected EOF while reading COLMAP image name")
        if char == b"\0":
            return value.decode("utf-8", errors="replace")
        value.extend(char)


def read_colmap_reconstruction(directory: str | Path, trust: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read camera/image poses and sparse XYZ coordinates from a COLMAP binary model."""
    root = Path(directory)
    cameras_path, images_path, points_path = (root / name for name in ("cameras.bin", "images.bin", "points3D.bin"))
    missing = [str(path) for path in (cameras_path, images_path, points_path) if not path.is_file()]
    if missing:
        raise ValueError(f"COLMAP reconstruction is incomplete: {missing}")
    camera_models: dict[int, dict[str, Any]] = {}
    model_params = {0: 3, 1: 4, 2: 4, 3: 5, 4: 8, 5: 8, 6: 12, 7: 5, 8: 4, 9: 5, 10: 12, 11: 16}
    with cameras_path.open("rb") as stream:
        count = struct.unpack("<Q", stream.read(8))[0]
        for _ in range(count):
            camera_id, model_id, width, height = struct.unpack("<iiQQ", stream.read(24))
            if model_id not in model_params:
                raise ValueError(f"Unsupported COLMAP camera model id {model_id}")
            params = struct.unpack(f"<{model_params[model_id]}d", stream.read(model_params[model_id] * 8))
            if not all(math.isfinite(x) for x in params):
                raise ValueError(f"COLMAP camera {camera_id} contains non-finite intrinsics")
            camera_models[camera_id] = {"camera_id": camera_id, "model_id": model_id, "width": width, "height": height, "parameters": list(params)}
    images: list[dict[str, Any]] = []
    with images_path.open("rb") as stream:
        count = struct.unpack("<Q", stream.read(8))[0]
        for _ in range(count):
            image_id = struct.unpack("<I", stream.read(4))[0]
            qvec = list(struct.unpack("<4d", stream.read(32)))
            tvec = list(struct.unpack("<3d", stream.read(24)))
            camera_id = struct.unpack("<I", stream.read(4))[0]
            name = _read_c_string(stream)
            point_count = struct.unpack("<Q", stream.read(8))[0]
            stream.seek(point_count * 24, 1)
            if camera_id not in camera_models:
                raise ValueError(f"COLMAP image {image_id} refers to missing camera {camera_id}")
            images.append({"image_id": image_id, "image_name": name, "camera_id": camera_id,
                           "qvec_wxyz": qvec, "tvec": tvec,
                           "camera_center": colmap_camera_center(qvec, tvec), "registered": True})
    sparse_points: list[list[float]] = []
    with points_path.open("rb") as stream:
        count = struct.unpack("<Q", stream.read(8))[0]
        for _ in range(count):
            stream.read(8)  # point3D id
            xyz = list(struct.unpack("<3d", stream.read(24)))
            stream.read(3)  # RGB
            stream.read(8)  # reprojection error
            track_len = struct.unpack("<Q", stream.read(8))[0]
            stream.seek(track_len * 8, 1)
            if all(math.isfinite(x) for x in xyz):
                sparse_points.append(xyz)
    trust = trust or {}
    trusted_ids = set(map(str, trust.get("trusted_image_ids", [])))
    trusted_names = set(map(str, trust.get("trusted_image_names", [])))
    untrusted_ids = set(map(str, trust.get("untrusted_image_ids", [])))
    untrusted_names = set(map(str, trust.get("untrusted_image_names", [])))
    if trusted_ids or trusted_names or untrusted_ids or untrusted_names:
        for image in images:
            key_id, key_name = str(image["image_id"]), image["image_name"]
            explicitly_listed = bool(trusted_ids or trusted_names)
            image["trusted"] = ((key_id in trusted_ids or key_name in trusted_names) if explicitly_listed
                                else key_id not in untrusted_ids and key_name not in untrusted_names)
            image["trust_reason"] = "configured_pose_classification"
    else:
        for image in images:
            image["trusted"] = True
            image["trust_reason"] = "COLMAP_REGISTERED_UNASSESSED"
    return {"source_reconstruction": {"identity": {"directory_name": root.name},
                                      "coordinate_frame": {"convention": "COLMAP_WORLD", "up_axis": "UNSPECIFIED", "metric_scale": "UNKNOWN"}},
            "camera_models": list(camera_models.values()), "cameras": images,
            "sparse_points": sparse_points, "sparse_point_count": len(sparse_points)}


def _mat_vec(rotation: list[list[float]], point: list[float]) -> list[float]:
    return [_dot(row, point) for row in rotation]


def _transform_spec(frame: dict[str, Any]) -> tuple[float, list[list[float]], list[float], bool]:
    raw = frame.get("source_to_viewer", {})
    scale = float(raw.get("uniform_scale", 1.0))
    rotation = raw.get("rotation", [[1, 0, 0], [0, 1, 0], [0, 0, 1]])
    translation = raw.get("translation", [0, 0, 0])
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError("source_to_viewer.uniform_scale must be finite and greater than zero")
    if not isinstance(rotation, list) or len(rotation) != 3 or any(len(row) != 3 for row in rotation):
        raise ValueError("source_to_viewer.rotation must be a 3 x 3 matrix")
    rot = [_finite_vector(row, 3, "source_to_viewer.rotation row") for row in rotation]
    trans = _finite_vector(translation, 3, "source_to_viewer.translation")
    # Reject matrices that would distort distances or change handedness.
    cols = [[rot[r][c] for r in range(3)] for c in range(3)]
    orthogonal = all(abs(_dot(cols[i], cols[j]) - (1.0 if i == j else 0.0)) < 1e-5 for i in range(3) for j in range(3))
    determinant = _dot(cols[0], _cross(cols[1], cols[2]))
    if not orthogonal or determinant < 0.999:
        raise ValueError("source_to_viewer.rotation must be a right-handed orthonormal rotation")
    return scale, rot, trans, bool(raw.get("verified", False))


def _transform_point(point: list[float], scale: float, rotation: list[list[float]], translation: list[float]) -> list[float]:
    return [scale * value + translation[i] for i, value in enumerate(_mat_vec(rotation, point))]


def _plane_in_viewer(plane: dict[str, Any], frame: dict[str, Any], scale: float, rotation: list[list[float]], translation: list[float]) -> dict[str, Any]:
    normal = _normalize(_finite_vector(plane.get("normal"), 3, "floor_plane.normal"))
    offset = float(plane.get("offset", 0.0))
    if not math.isfinite(offset):
        raise ValueError("Plane offset must be finite")
    plane_frame = plane.get("coordinate_frame", "viewer" if frame.get("coordinates_are_viewer") else "source")
    if plane_frame == "viewer":
        result_normal, result_offset = normal, offset
    elif plane_frame == "source":
        result_normal = _normalize(_mat_vec(rotation, normal))
        result_offset = scale * offset - _dot(result_normal, translation)
    else:
        raise ValueError("Plane coordinate_frame must be 'source' or 'viewer'")
    if plane.get("up_hint"):
        hint = _normalize(_finite_vector(plane["up_hint"], 3, "floor_plane.up_hint"))
        if _dot(result_normal, hint) < 0:
            result_normal = [-x for x in result_normal]
            result_offset = -result_offset
    return {"normal": result_normal, "offset": result_offset,
            "classification": plane.get("classification", "GEOMETRY_SUPPORTED"),
            "evidence": plane.get("evidence", []), "source": plane.get("source", "configured_plane")}


def _plane_basis(plane: dict[str, Any], frame: dict[str, Any]) -> dict[str, list[float]]:
    normal = plane["normal"]
    origin = [-plane["offset"] * x for x in normal]
    preferred = _finite_vector(frame.get("basis_reference_axis", [1, 0, 0]), 3, "basis_reference_axis")
    projected = [preferred[i] - _dot(preferred, normal) * normal[i] for i in range(3)]
    if math.sqrt(_dot(projected, projected)) < 1e-6:
        preferred = [0, 0, 1]
        projected = [preferred[i] - _dot(preferred, normal) * normal[i] for i in range(3)]
    u = _normalize(projected)
    v = _normalize(_cross(normal, u))
    return {"origin": origin, "normal": normal, "u": u, "v": v}


def _project(point: list[float], basis: dict[str, list[float]]) -> list[float]:
    delta = [point[i] - basis["origin"][i] for i in range(3)]
    return [_dot(delta, basis["u"]), _dot(delta, basis["v"]), _dot(delta, basis["normal"])]


def _unproject(local: list[float], basis: dict[str, list[float]]) -> list[float]:
    return [basis["origin"][i] + basis["u"][i] * local[0] + basis["v"][i] * local[1] + basis["normal"][i] * local[2] for i in range(3)]


def _distance(a: list[float], b: list[float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _median_spacing(points: list[list[float]]) -> float:
    unique = sorted({(round(p[0], 10), round(p[1], 10)) for p in points})
    if len(unique) < 2:
        return 0.0
    nearest = []
    for i, point in enumerate(unique):
        nearest.append(min(math.hypot(point[0] - other[0], point[1] - other[1]) for j, other in enumerate(unique) if i != j))
    nonzero = [d for d in nearest if d > EPS]
    return median(nonzero) if nonzero else 0.0


def _convex_hull(points: list[list[float]]) -> list[list[float]]:
    pts = sorted({(float(p[0]), float(p[1])) for p in points})
    if len(pts) < 3:
        return [[x, y] for x, y in pts]
    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lower = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= EPS:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= EPS:
            upper.pop()
        upper.append(p)
    return [[x, y] for x, y in lower[:-1] + upper[:-1]]


def _circumcircle(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]):
    d = 2 * (a[0] * (b[1] - c[1]) + b[0] * (c[1] - a[1]) + c[0] * (a[1] - b[1]))
    if abs(d) <= EPS:
        return None
    aa, bb, cc = a[0] ** 2 + a[1] ** 2, b[0] ** 2 + b[1] ** 2, c[0] ** 2 + c[1] ** 2
    ux = (aa * (b[1] - c[1]) + bb * (c[1] - a[1]) + cc * (a[1] - b[1])) / d
    uy = (aa * (c[0] - b[0]) + bb * (a[0] - c[0]) + cc * (b[0] - a[0])) / d
    return ux, uy, (ux - a[0]) ** 2 + (uy - a[1]) ** 2


def _delaunay(points: list[list[float]]) -> list[tuple[int, int, int]]:
    pts = sorted({(float(p[0]), float(p[1])) for p in points})
    if len(pts) < 3:
        return []
    minx, maxx = min(x for x, _ in pts), max(x for x, _ in pts)
    miny, maxy = min(y for _, y in pts), max(y for _, y in pts)
    span = max(maxx - minx, maxy - miny, 1.0)
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    pts.extend([(cx - 32 * span, cy - 16 * span), (cx, cy + 32 * span), (cx + 32 * span, cy - 16 * span)])
    n = len(pts) - 3
    triangles: list[tuple[int, int, int]] = [(n, n + 1, n + 2)]
    for index in range(n):
        p = pts[index]
        bad = []
        for tri in triangles:
            circle = _circumcircle(pts[tri[0]], pts[tri[1]], pts[tri[2]])
            if circle is not None and (p[0] - circle[0]) ** 2 + (p[1] - circle[1]) ** 2 <= circle[2] + max(1e-12, circle[2] * 1e-10):
                bad.append(tri)
        edge_counts = Counter()
        for tri in bad:
            for a, b in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
                edge_counts[tuple(sorted((a, b)))] += 1
        triangles = [tri for tri in triangles if tri not in bad]
        for edge, count in edge_counts.items():
            if count == 1 and abs((pts[edge[1]][0] - pts[edge[0]][0]) * (p[1] - pts[edge[0]][1]) -
                                  (pts[edge[1]][1] - pts[edge[0]][1]) * (p[0] - pts[edge[0]][0])) > EPS:
                triangles.append((edge[0], edge[1], index))
    triangles = [tri for tri in triangles if all(index < n for index in tri)]
    # Return indices into the original sorted unique point list.
    return triangles


def _alpha_boundary(points: list[list[float]], alpha: float) -> list[list[float]]:
    pts = sorted({(float(p[0]), float(p[1])) for p in points})
    if len(pts) < 4:
        return _convex_hull(points)
    triangles = _delaunay(points)
    kept = []
    for tri in triangles:
        circle = _circumcircle(pts[tri[0]], pts[tri[1]], pts[tri[2]])
        if circle and math.sqrt(circle[2]) <= alpha + EPS:
            kept.append(tri)
    if not kept:
        return _convex_hull(points)
    edge_counts = Counter()
    for tri in kept:
        for a, b in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
            edge_counts[tuple(sorted((a, b)))] += 1
    boundary_edges = [edge for edge, count in edge_counts.items() if count == 1]
    adjacency: dict[int, list[int]] = defaultdict(list)
    for a, b in boundary_edges:
        adjacency[a].append(b); adjacency[b].append(a)
    if not adjacency or any(len(neighbors) != 2 for neighbors in adjacency.values()):
        return _convex_hull(points)
    start = min(adjacency)
    ring = [start]
    previous, current = -1, start
    while True:
        options = sorted(v for v in adjacency[current] if v != previous)
        if not options:
            return _convex_hull(points)
        nxt = options[0]
        if nxt == start:
            break
        if nxt in ring:
            return _convex_hull(points)
        ring.append(nxt); previous, current = current, nxt
        if len(ring) > len(adjacency):
            return _convex_hull(points)
    if len(ring) < 3 or len(ring) != len(adjacency):
        return _convex_hull(points)
    return [[pts[i][0], pts[i][1]] for i in ring]


def _signed_area(poly: list[list[float]]) -> float:
    return sum(poly[i][0] * poly[(i + 1) % len(poly)][1] - poly[(i + 1) % len(poly)][0] * poly[i][1] for i in range(len(poly))) / 2


def _orient(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _geometry_tolerance(*points: list[float]) -> float:
    scale = max((abs(float(value)) for point in points for value in point[:2]), default=1.0)
    return max(1e-10, scale * 1e-12)


def _point_on_segment(point, a, b, tolerance: float) -> bool:
    length = math.hypot(b[0] - a[0], b[1] - a[1])
    if abs(_orient(a, b, point)) > tolerance * max(1.0, length):
        return False
    return (min(a[0], b[0]) - tolerance <= point[0] <= max(a[0], b[0]) + tolerance
            and min(a[1], b[1]) - tolerance <= point[1] <= max(a[1], b[1]) + tolerance)


def _segments_intersect(a, b, c, d) -> bool:
    tolerance = _geometry_tolerance(a, b, c, d)
    o1, o2, o3, o4 = _orient(a, b, c), _orient(a, b, d), _orient(c, d, a), _orient(c, d, b)
    area_tolerance = tolerance * max(1.0, _distance(a, b), _distance(c, d))
    if ((o1 > area_tolerance and o2 < -area_tolerance) or (o1 < -area_tolerance and o2 > area_tolerance)) \
            and ((o3 > area_tolerance and o4 < -area_tolerance) or (o3 < -area_tolerance and o4 > area_tolerance)):
        return True
    return ((abs(o1) <= area_tolerance and _point_on_segment(c, a, b, tolerance))
            or (abs(o2) <= area_tolerance and _point_on_segment(d, a, b, tolerance))
            or (abs(o3) <= area_tolerance and _point_on_segment(a, c, d, tolerance))
            or (abs(o4) <= area_tolerance and _point_on_segment(b, c, d, tolerance)))


def _simple_polygon(poly: list[list[float]]) -> bool:
    if len(poly) < 3 or abs(_signed_area(poly)) <= EPS:
        return False
    n = len(poly)
    for i in range(n):
        if _distance(poly[i], poly[(i + 1) % n]) <= EPS:
            return False
        for j in range(i + 1, n):
            if j in {i, (i + 1) % n} or i == (j + 1) % n:
                continue
            if _segments_intersect(poly[i], poly[(i + 1) % n], poly[j], poly[(j + 1) % n]):
                return False
    return True


def _offset_polygon(poly: list[list[float]], distance: float) -> list[list[float]]:
    """Miter-offset a simple ring; callers retain it only when topology stays valid."""
    if distance <= EPS:
        return [list(p) for p in poly]
    area = _signed_area(poly)
    direction = 1.0 if area > 0 else -1.0
    lines = []
    for i, a in enumerate(poly):
        b = poly[(i + 1) % len(poly)]
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        if length <= EPS:
            raise ValueError("Cannot offset a polygon with a zero-length edge")
        # CCW interior lies left; exterior points right.
        nx, ny = direction * dy / length, -direction * dx / length
        lines.append(((a[0] + nx * distance, a[1] + ny * distance), (dx / length, dy / length)))
    result = []
    for i in range(len(poly)):
        p1, d1 = lines[i - 1]
        p2, d2 = lines[i]
        denom = d1[0] * d2[1] - d1[1] * d2[0]
        if abs(denom) <= EPS:
            result.append([(p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2]); continue
        t = ((p2[0] - p1[0]) * d2[1] - (p2[1] - p1[1]) * d2[0]) / denom
        result.append([p1[0] + t * d1[0], p1[1] + t * d1[1]])
    if not _simple_polygon(result):
        raise ValueError("Configured margin would make the footprint self-intersect or collapse")
    return result


def _components(points: list[dict[str, Any]], radius: float) -> list[list[dict[str, Any]]]:
    if not points:
        return []
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for point in points:
        if point.get("region_hint"):
            buckets[str(point["region_hint"])].append(point)
    if buckets:
        no_hint = [point for point in points if not point.get("region_hint")]
        if no_hint:
            buckets["unassigned"] += no_hint
        return [buckets[key] for key in sorted(buckets)]
    adjacency = [[] for _ in points]
    for i in range(len(points)):
        for j in range(i + 1, len(points)):
            if _distance(points[i]["uvh"], points[j]["uvh"]) <= radius:
                adjacency[i].append(j); adjacency[j].append(i)
    seen, groups = set(), []
    for start in range(len(points)):
        if start in seen:
            continue
        queue, group = deque([start]), []
        seen.add(start)
        while queue:
            i = queue.popleft(); group.append(points[i])
            for j in adjacency[i]:
                if j not in seen:
                    seen.add(j); queue.append(j)
        groups.append(group)
    return groups


def _point_in_polygon(point: list[float], poly: list[list[float]]) -> bool:
    x, y = point; inside = False
    for i, a in enumerate(poly):
        b = poly[(i + 1) % len(poly)]
        if _point_segment_distance(point, a, b) <= 1e-8:
            return True
        if (a[1] > y) != (b[1] > y) and x < (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]) + a[0]:
            inside = not inside
    return inside


def _point_segment_distance(p: list[float], a: list[float], b: list[float]) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    denom = dx * dx + dy * dy
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (denom or 1.0)))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def _line_intersection_t(a, b, c, d) -> float | None:
    values = _segment_intersection_parameters(a, b, c, d)
    return values[0] if values else None


def _segment_intersection_parameters(a, b, c, d) -> list[float]:
    """Return every contact parameter on AB, including endpoints and collinear overlap."""
    tolerance = _geometry_tolerance(a, b, c, d)
    rx, ry = b[0] - a[0], b[1] - a[1]
    sx, sy = d[0] - c[0], d[1] - c[1]
    r_length, s_length = math.hypot(rx, ry), math.hypot(sx, sy)
    cross = rx * sy - ry * sx
    qx, qy = c[0] - a[0], c[1] - a[1]
    area_tolerance = tolerance * max(1.0, r_length, s_length)
    if r_length <= tolerance:
        return [0.0] if _point_on_segment(a, c, d, tolerance) else []
    if abs(cross) > area_tolerance:
        t = (qx * sy - qy * sx) / cross
        u = (qx * ry - qy * rx) / cross
        parameter_tolerance = tolerance / max(r_length, 1.0)
        if -parameter_tolerance <= t <= 1 + parameter_tolerance and -parameter_tolerance <= u <= 1 + parameter_tolerance:
            return [max(0.0, min(1.0, t))]
        return []
    if abs(qx * ry - qy * rx) > area_tolerance:
        return []
    denominator = rx * rx + ry * ry
    t0 = (qx * rx + qy * ry) / denominator
    t1 = ((d[0] - a[0]) * rx + (d[1] - a[1]) * ry) / denominator
    low, high = max(0.0, min(t0, t1)), min(1.0, max(t0, t1))
    parameter_tolerance = tolerance / max(r_length, 1.0)
    if high < low - parameter_tolerance:
        return []
    if high <= low + parameter_tolerance:
        return [max(0.0, min(1.0, (low + high) * 0.5))]
    return [max(0.0, min(1.0, low)), max(0.0, min(1.0, high))]


def _region_for(uv: list[float], regions: list[dict[str, Any]]) -> dict[str, Any] | None:
    matches = [region for region in regions if _point_in_polygon(uv, region["polygon_uv"])]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1 and all(min(_point_segment_distance(uv, p, region["polygon_uv"][(i + 1) % len(region["polygon_uv"])])
                                   for i, p in enumerate(region["polygon_uv"])) <= 1e-8 for region in matches):
        return matches[0]  # shared room boundary; portal validation below controls crossing
    return None


def _segment_distance(a, b, c, d) -> float:
    if _segments_intersect(a, b, c, d):
        return 0.0
    return min(_point_segment_distance(a, c, d), _point_segment_distance(b, c, d),
               _point_segment_distance(c, a, b), _point_segment_distance(d, a, b))


def _edge_without_portal_intervals(a, b, portals, tolerance: float) -> list[tuple[list[float], list[float]]]:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    if length <= tolerance:
        return [(list(a), list(b))]
    denominator = dx * dx + dy * dy
    cuts = []
    for first, second in portals:
        if max(abs(_orient(a, b, first)), abs(_orient(a, b, second))) > tolerance * max(1.0, length):
            continue
        t0 = ((first[0] - a[0]) * dx + (first[1] - a[1]) * dy) / denominator
        t1 = ((second[0] - a[0]) * dx + (second[1] - a[1]) * dy) / denominator
        low, high = max(0.0, min(t0, t1)), min(1.0, max(t0, t1))
        if high >= low:
            cuts.append((low, high))
    if not cuts:
        return [(list(a), list(b))]
    cuts.sort()
    merged = []
    for low, high in cuts:
        if merged and low <= merged[-1][1] + tolerance / max(length, 1.0):
            merged[-1] = (merged[-1][0], max(merged[-1][1], high))
        else:
            merged.append((low, high))
    remaining, cursor = [], 0.0
    for low, high in merged:
        if low > cursor:
            remaining.append(([a[0] + dx * cursor, a[1] + dy * cursor], [a[0] + dx * low, a[1] + dy * low]))
        cursor = max(cursor, high)
    if cursor < 1.0:
        remaining.append(([a[0] + dx * cursor, a[1] + dy * cursor], list(b)))
    return remaining


def _portal_on_polygon_boundary(portal: list[list[float]], polygon: list[list[float]], tolerance: float) -> bool:
    first, second = portal
    length = math.hypot(second[0] - first[0], second[1] - first[1])
    if length <= tolerance:
        return False
    dx, dy = second[0] - first[0], second[1] - first[1]
    denominator = length * length
    intervals = []
    for i, a in enumerate(polygon):
        b = polygon[(i + 1) % len(polygon)]
        if max(abs(_orient(first, second, a)), abs(_orient(first, second, b))) > tolerance * max(1.0, length):
            continue
        ta = ((a[0] - first[0]) * dx + (a[1] - first[1]) * dy) / denominator
        tb = ((b[0] - first[0]) * dx + (b[1] - first[1]) * dy) / denominator
        low, high = max(0.0, min(ta, tb)), min(1.0, max(ta, tb))
        if high >= low:
            intervals.append((low, high))
    intervals.sort()
    coverage, parameter_tolerance = 0.0, tolerance / max(length, 1.0)
    for low, high in intervals:
        if low > coverage + parameter_tolerance:
            return False
        coverage = max(coverage, high)
        if coverage >= 1.0 - parameter_tolerance:
            return True
    return False


def navigation_segment_allowed(artifact: dict[str, Any], start_local: list[float], end_local: list[float], *, margin: float | None = None) -> bool:
    """Check a swept local XYZ camera segment against polygons, floors, ceilings and portals."""
    try:
        start_local, end_local = [float(x) for x in start_local], [float(x) for x in end_local]
    except (TypeError, ValueError):
        return False
    if len(start_local) != 3 or len(end_local) != 3 or not all(math.isfinite(x) for x in [*start_local, *end_local]):
        return False
    regions = artifact.get("regions", [])
    start_regions = [region for region in regions if _point_in_polygon(start_local[:2], region.get("polygon_uv", []))]
    end_regions = [region for region in regions if _point_in_polygon(end_local[:2], region.get("polygon_uv", []))]
    if not start_regions or not end_regions:
        return False
    try:
        required = float(margin if margin is not None else artifact.get("camera_clearance", {}).get("horizontal_units", 0.0))
    except (TypeError, ValueError):
        return False
    if not math.isfinite(required) or required < 0:
        return False
    transitions = artifact.get("transitions", [])
    a, b = start_local[:2], end_local[:2]
    tolerance = _geometry_tolerance(a, b)
    cuts = {0.0, 1.0}
    for region in regions:
        poly = region.get("polygon_uv", [])
        for i, p in enumerate(poly):
            cuts.update(_segment_intersection_parameters(a, b, p, poly[(i + 1) % len(poly)]))
    portal_events = []
    for transition in transitions:
        portal = transition.get("portal", {}).get("segment_uv")
        if portal and len(portal) == 2:
            events = _segment_intersection_parameters(a, b, portal[0], portal[1])
            cuts.update(events)
            if transition.get("approved"):
                portal_events.append((transition, events))
    ordered = sorted(cuts)
    intervals = [(low, high) for low, high in zip(ordered, ordered[1:]) if high - low > 1e-14]
    param_tolerance = tolerance / max(_distance(a, b), 1.0)

    def within_heights(region: dict[str, Any], low_t: float, high_t: float) -> bool:
        vertical = region.get("vertical_bounds", {})
        floor, ceiling = vertical.get("floor") or {}, vertical.get("ceiling") or {}
        if floor.get("value") is None or ceiling.get("value") is None:
            return False
        low = float(floor["value"]) + float(floor.get("clearance", 0.0))
        high = float(ceiling["value"]) - float(ceiling.get("clearance", 0.0))
        if not math.isfinite(low) or not math.isfinite(high) or high < low:
            return False
        for t in (low_t, high_t):
            height = start_local[2] + (end_local[2] - start_local[2]) * t
            if not math.isfinite(height) or height < low - tolerance or height > high + tolerance:
                return False
        return True

    for initial in start_regions:
        current = initial
        valid = True
        for low_t, high_t in intervals:
            mid_t = (low_t + high_t) * 0.5
            mid = [a[i] + (b[i] - a[i]) * mid_t for i in range(2)]
            candidates = [region for region in regions if _point_in_polygon(mid, region.get("polygon_uv", []))]
            if not candidates:
                valid = False
                break
            if current not in candidates:
                choices = []
                for candidate in candidates:
                    for transition, events in portal_events:
                        if {transition.get("from_region"), transition.get("to_region")} != {current.get("id"), candidate.get("id")}:
                            continue
                        if not any(low_t - param_tolerance <= event <= high_t + param_tolerance for event in events):
                            continue
                        portal = transition.get("portal", {}).get("segment_uv")
                        if portal and _portal_on_polygon_boundary(portal, current["polygon_uv"], max(tolerance, 1e-8)) \
                                and _portal_on_polygon_boundary(portal, candidate["polygon_uv"], max(tolerance, 1e-8)):
                            choices.append(candidate)
                if len(choices) != 1:
                    valid = False
                    break
                current = choices[0]
            if not within_heights(current, low_t, high_t):
                valid = False
                break
            if required > 0:
                path_a = [a[i] + (b[i] - a[i]) * low_t for i in range(2)]
                path_b = [a[i] + (b[i] - a[i]) * high_t for i in range(2)]
                portals = [transition["portal"]["segment_uv"] for transition, _events in portal_events
                           if current["id"] in {transition.get("from_region"), transition.get("to_region")}
                           and _portal_on_polygon_boundary(transition["portal"]["segment_uv"], current["polygon_uv"], max(tolerance, 1e-8))]
                for index, edge_a in enumerate(current["polygon_uv"]):
                    edge_b = current["polygon_uv"][(index + 1) % len(current["polygon_uv"])]
                    for edge_start, edge_end in _edge_without_portal_intervals(edge_a, edge_b, portals, max(tolerance, 1e-8)):
                        if _segment_distance(path_a, path_b, edge_start, edge_end) < required - max(tolerance, 1e-8):
                            valid = False
                            break
                    if not valid:
                        break
            if not valid:
                break
        if valid and current in end_regions:
            return True
    return False


def validate_guided_path(artifact: dict[str, Any], points_local: list[list[float]]) -> dict[str, Any]:
    failures = [index for index, (a, b) in enumerate(zip(points_local, points_local[1:])) if not navigation_segment_allowed(artifact, a, b)]
    return {"valid": not failures, "invalid_segment_indices": failures, "segment_count": max(0, len(points_local) - 1)}


def _validate_artifact(artifact: dict[str, Any]) -> dict[str, Any]:
    errors = []
    if artifact.get("schema_version") != "exhibition-archive.navigation.v1":
        errors.append("unsupported_schema_version")
    frame = artifact.get("coordinate_frame", {})
    matrix = frame.get("source_to_viewer_matrix_row_major")
    scale = frame.get("uniform_scale")
    if not isinstance(matrix, list) or len(matrix) != 4 or any(not isinstance(row, list) or len(row) != 4 for row in matrix):
        errors.append("invalid_source_to_viewer_matrix")
    elif not all(math.isfinite(float(value)) for row in matrix for value in row):
        errors.append("non_finite_source_to_viewer_matrix")
    elif not isinstance(scale, (int, float)) or not math.isfinite(float(scale)) or float(scale) <= 0 or any(abs(float(matrix[3][i]) - expected) > 1e-8 for i, expected in enumerate((0, 0, 0, 1))):
        errors.append("invalid_source_to_viewer_affine_frame")
    else:
        columns = [[float(matrix[row][col]) / float(scale) for row in range(3)] for col in range(3)]
        if not all(abs(_dot(columns[i], columns[j]) - (1 if i == j else 0)) < 1e-5 for i in range(3) for j in range(3)) or _dot(columns[0], _cross(columns[1], columns[2])) < 0.999:
            errors.append("source_to_viewer_rotation_not_right_handed_rigid")
    local = frame.get("viewer_to_navigation", {})
    try:
        axes = [_normalize(_finite_vector(local.get(key), 3, "navigation basis")) for key in ("u_viewer", "v_viewer", "up_viewer")]
        _finite_vector(local.get("origin_viewer"), 3, "navigation origin")
        if any(abs(_dot(axes[i], axes[j])) > 1e-5 for i in range(3) for j in range(i + 1, 3)) or _dot(_cross(axes[0], axes[1]), axes[2]) < 0.999:
            errors.append("invalid_viewer_to_navigation_basis")
    except (TypeError, ValueError):
        if artifact.get("regions"):
            errors.append("missing_viewer_to_navigation_basis")
    regions = artifact.get("regions")
    if not isinstance(regions, list) or not regions:
        errors.append("no_regions")
        regions = []
    ids = set()
    for region in regions:
        rid = region.get("id")
        if not rid or rid in ids:
            errors.append("missing_or_duplicate_region_id")
        ids.add(rid)
        poly = region.get("polygon_uv", [])
        if not _simple_polygon(poly):
            errors.append(f"region_not_simple:{rid}")
        if poly and _signed_area(poly) < 0:
            errors.append(f"region_winding_not_counterclockwise:{rid}")
        vertical = region.get("vertical_bounds", {})
        floor = vertical.get("floor") or {}
        ceiling = vertical.get("ceiling") or {}
        for label, bound in (("floor", floor), ("ceiling", ceiling)):
            value = bound.get("value")
            if value is not None and not math.isfinite(float(value)):
                errors.append(f"non_finite_{label}:{rid}")
        if floor.get("value") is not None and ceiling.get("value") is not None and float(ceiling["value"]) <= float(floor["value"]):
            errors.append(f"vertical_bounds_inverted:{rid}")
    for transition in artifact.get("transitions", []):
        if transition.get("from_region") not in ids or transition.get("to_region") not in ids:
            errors.append("transition_references_unknown_region")
        segment = transition.get("portal", {}).get("segment_uv", [])
        if len(segment) != 2 or _distance(segment[0], segment[1]) <= EPS:
            errors.append("transition_has_invalid_portal")
            continue
        region_map = {region.get("id"): region for region in regions}
        first, second = region_map.get(transition.get("from_region")), region_map.get(transition.get("to_region"))
        if first and second and len(first.get("polygon_uv", [])) >= 2 and len(second.get("polygon_uv", [])) >= 2 and transition.get("from_region") != transition.get("to_region"):
            tolerance = max(_geometry_tolerance(*segment), 1e-8)
            if not _portal_on_polygon_boundary(segment, first["polygon_uv"], tolerance) \
                    or not _portal_on_polygon_boundary(segment, second["polygon_uv"], tolerance):
                errors.append(f"transition_portal_not_on_both_region_boundaries:{transition.get('from_region')}:{transition.get('to_region')}")
    return {"status": "PASS" if not errors else "INVALID", "errors": errors,
            "region_count": len(regions), "transition_count": len(artifact.get("transitions", [])),
            "finite_geometry": not errors, "public_ready": False}


def validate_navigation_artifact(artifact: dict[str, Any]) -> dict[str, Any]:
    try:
        result = _validate_artifact(artifact)
        guided = []
        for path in artifact.get("guided_paths", []):
            points = path.get("waypoints_local", path.get("points_local")) if isinstance(path, dict) else None
            if points:
                check = validate_guided_path(artifact, points)
                guided.append({"id": path.get("id"), **check})
                if not check["valid"]:
                    result["errors"].append(f"invalid_guided_path:{path.get('id')}")
            elif isinstance(path, dict) and (path.get("waypoints") or path.get("points")):
                guided.append({"id": path.get("id"), "valid": False, "reason": "guided path lacks floor-local coordinates for swept validation"})
                result["errors"].append(f"guided_path_coordinate_frame_unresolved:{path.get('id')}")
        if result["errors"]:
            result["status"] = "INVALID"
        result["guided_path_validation"] = guided
        initial = _validate_initial_camera(artifact)
        result["initial_camera_validation"] = initial
        if initial["status"] == "INVALID":
            result["errors"].append(initial["error"])
            result["status"] = "INVALID"
        result["public_ready"] = False
        return result
    except (TypeError, ValueError, OverflowError, IndexError, KeyError) as exc:
        return {"status": "INVALID", "errors": [f"malformed_navigation_artifact:{type(exc).__name__}"],
                "region_count": len(artifact.get("regions", [])) if isinstance(artifact, dict) and isinstance(artifact.get("regions"), list) else 0,
                "transition_count": len(artifact.get("transitions", [])) if isinstance(artifact, dict) and isinstance(artifact.get("transitions"), list) else 0,
                "finite_geometry": False, "guided_path_validation": [], "initial_camera_validation": {"status": "INVALID"},
                "public_ready": False}


def _validate_initial_camera(artifact: dict[str, Any]) -> dict[str, Any]:
    compatibility = artifact.get("viewer_compatibility") or {}
    default_id = (compatibility.get("stations") or {}).get("defaultStationId")
    if not default_id:
        return {"status": "MISSING", "station_id": None, "error": "default_camera_station_missing"}
    evidence = artifact.get("pose_evidence", {})
    centers = evidence.get("candidate_camera_centers", evidence.get("trusted_camera_centers", []))
    center = next((item for item in centers if item.get("station_id") == default_id), None)
    if center is None:
        return {"status": "INVALID", "station_id": default_id, "error": "default_camera_station_unresolved"}
    local = center.get("navigation_local_uvh") or []
    if len(local) != 3 or not all(math.isfinite(float(value)) for value in local):
        return {"status": "INVALID", "station_id": default_id, "error": "default_camera_position_invalid"}
    region = next((item for item in artifact.get("regions", []) if item.get("id") == center.get("region_id")), None)
    if region is None:
        region = _region_for(local[:2], artifact.get("regions", []))
    if region is None or not _point_in_polygon(local[:2], region.get("polygon_uv", [])):
        return {"status": "INVALID", "station_id": default_id, "error": "default_camera_outside_navigation_region"}
    vertical = region.get("vertical_bounds", {})
    floor, ceiling = vertical.get("floor") or {}, vertical.get("ceiling") or {}
    if floor.get("value") is None or ceiling.get("value") is None:
        return {"status": "INSUFFICIENT_GEOMETRY", "station_id": default_id, "region_id": region["id"],
                "error": "default_camera_vertical_bounds_unavailable"}
    low = float(floor["value"]) + float(floor.get("clearance", 0.0))
    high = float(ceiling["value"]) - float(ceiling.get("clearance", 0.0))
    if float(local[2]) < low - 1e-8 or float(local[2]) > high + 1e-8:
        return {"status": "INVALID", "station_id": default_id, "region_id": region["id"],
                "height_units": local[2], "allowed_height_units": [low, high], "error": "default_camera_outside_vertical_bounds"}
    clearance = float(artifact.get("camera_clearance", {}).get("horizontal_units", 0.0))
    distance = min(_point_segment_distance(local[:2], vertex, region["polygon_uv"][(index + 1) % len(region["polygon_uv"])])
                   for index, vertex in enumerate(region["polygon_uv"]))
    if distance < clearance - 1e-8:
        return {"status": "INVALID", "station_id": default_id, "region_id": region["id"],
                "boundary_distance_units": distance, "required_clearance_units": clearance,
                "error": "default_camera_violates_boundary_clearance"}
    return {"status": "PASS", "station_id": default_id, "region_id": region["id"],
            "navigation_local_uvh": local, "boundary_distance_units": distance,
            "allowed_height_units": [low, high]}


def generate_navigation(source: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Generate an explicitly unapproved navigation proposal from normalized SfM evidence."""
    config = config or {}
    frame = dict(source.get("coordinate_frame", {}))
    scale, rotation, translation, transform_verified = _transform_spec(frame)
    plane = source.get("floor_plane") or source.get("geometry", {}).get("floor_plane") or config.get("floor_plane")
    warnings: list[dict[str, str]] = []
    if plane:
        plane_viewer = _plane_in_viewer(plane, frame, scale, rotation, translation)
        basis = _plane_basis(plane_viewer, frame)
    else:
        plane_viewer = None
        basis = None
        warnings.append({"code": "FLOOR_UNOBSERVED", "severity": "BLOCKING", "message": "No floor plane was supplied; camera height is not sufficient evidence to manufacture one."})
    cameras = source.get("stations", source.get("cameras", []))
    projected: list[dict[str, Any]] = []
    pose_exclusions: list[dict[str, Any]] = []
    def is_trusted(row: dict[str, Any]) -> bool:
        value = row.get("trusted", row.get("pose_trust", "TRUSTED"))
        return value is True or (value is not False and str(value).upper() not in {"UNTRUSTED", "REJECTED", "FALSE", "OUTLIER"})
    def has_explicit_trust(row: dict[str, Any]) -> bool:
        reason = str(row.get("trust_reason", "")).upper()
        classification = str(row.get("pose_trust", "")).upper()
        if reason in {"COLMAP_REGISTERED_UNASSESSED", "POSE_TRUST_UNCLASSIFIED"} or classification in {"PROVISIONAL", "UNCLASSIFIED"}:
            return False
        return (row.get("trusted") is True or classification in {"TRUSTED", "VERIFIED", "ACCEPTED"}
                or bool(reason))
    for input_index, row in enumerate(cameras, 1):
        trusted = is_trusted(row)
        raw_center = row.get("centerCommonViewer") or row.get("center_viewer") or row.get("center") or row.get("camera_center")
        if raw_center is None and row.get("qvec_wxyz") is not None:
            raw_center = colmap_camera_center(row["qvec_wxyz"], row.get("tvec", [0, 0, 0]))
        if not trusted:
            pose_exclusions.append({"camera_id": row.get("camera_id"), "image_id": row.get("image_id"), "image_name": row.get("image_name"), "reason": row.get("trust_reason", "configured_untrusted_pose")})
            continue
        try:
            center = _finite_vector(raw_center, 3, "camera center")
        except (TypeError, ValueError) as exc:
            pose_exclusions.append({"camera_id": row.get("camera_id"), "image_id": row.get("image_id"), "image_name": row.get("image_name"), "reason": f"invalid_pose:{exc}"})
            continue
        if not frame.get("coordinates_are_viewer") and row.get("centerCommonViewer") is None and row.get("center_viewer") is None:
            center = _transform_point(center, scale, rotation, translation)
        forward = row.get("representativeForwardCommonViewer") or row.get("forward_viewer")
        if forward is None and row.get("qvec_wxyz") is not None:
            # World-to-camera rows; camera forward is the third camera axis in world coordinates.
            r = quaternion_wxyz_to_rotation(row["qvec_wxyz"])
            forward = [r[2][i] for i in range(3)]
            if not frame.get("coordinates_are_viewer"):
                forward = _mat_vec(rotation, forward)
        try:
            forward = _normalize(_finite_vector(forward or [0, 0, -1], 3, "camera forward"))
        except ValueError:
            forward = [0, 0, -1]
        explicit_trust = has_explicit_trust(row)
        trust_reason = str(row.get("trust_reason") or ("POSE_TRUST_UNCLASSIFIED" if not explicit_trust else "EXPLICITLY_CLASSIFIED"))
        image_id = row.get("image_id")
        station_id = row.get("stationId", row.get("station_id")) or (f"image-{image_id}" if image_id is not None else f"camera-{input_index:04d}")
        projected.append({"camera_id": row.get("camera_id"), "image_id": image_id,
                          "image_name": row.get("image_name"), "station_id": station_id,
                          "image_keys": row.get("imageKeys", row.get("image_keys", [])),
                          "center_viewer": center, "forward_viewer": forward,
                          "trust_explicit": explicit_trust, "trust_reason": trust_reason,
                          "region_hint": row.get("region_id", row.get("region_hint")),
                          "registered_image_count": row.get("registeredImageCount", 1)})
    if basis:
        for row in projected:
            row["uvh"] = _project(row["center_viewer"], basis)
    else:
        for row in projected:
            row["uvh"] = [row["center_viewer"][0], row["center_viewer"][2], row["center_viewer"][1]]
    xy = [row["uvh"] for row in projected]
    spacing = _median_spacing(xy)
    outlier_factor = float(config.get("nearest_neighbor_outlier_factor", 5.0))
    retained = []
    for row in projected:
        nearest = min((_distance(row["uvh"], other["uvh"]) for other in projected if other is not row), default=0.0)
        if spacing > EPS and nearest > spacing * outlier_factor:
            pose_exclusions.append({"camera_id": row["camera_id"], "image_id": row["image_id"], "image_name": row["image_name"], "station_id": row["station_id"], "reason": "isolated_spatial_pose_outlier", "nearest_neighbor_distance_units": nearest, "robust_station_spacing_units": spacing})
        else:
            retained.append(row)
    if pose_exclusions:
        warnings.append({"code": "POSES_EXCLUDED", "severity": "REVIEW", "message": f"{len(pose_exclusions)} pose record(s) were excluded from footprint construction and retained as diagnostics."})
    if retained and spacing > EPS:
        heights = [row["uvh"][2] for row in retained]
        vertical_spread = max(heights) - min(heights)
        vertical_limit = spacing * float(config.get("vertical_spread_spacing_factor", 2.5))
        if vertical_spread > vertical_limit:
            warnings.append({"code": "VERTICAL_SPREAD_REQUIRES_REVIEW", "severity": "REVIEW",
                             "message": "Camera-center heights vary substantially relative to local station spacing. A sloped or multi-level venue is possible; a single floor interval is not accepted automatically."})
    else:
        vertical_spread, vertical_limit = 0.0, 0.0
    if spacing <= EPS or len(retained) < 3:
        warnings.append({"code": "INSUFFICIENT_CAMERA_COVERAGE", "severity": "BLOCKING", "message": "At least three spatially distinct trusted camera positions are required for a footprint proposal."})
    connection_scale = float(config.get("region_connection_scale", 2.5))
    if not math.isfinite(connection_scale) or connection_scale <= 0:
        raise ValueError("region_connection_scale must be finite and greater than zero")
    connection_radius = max(spacing * connection_scale, EPS)
    components = _components(retained, connection_radius)
    alpha_scale = float(config.get("alpha_scale", 1.6))
    if not math.isfinite(alpha_scale) or alpha_scale <= 0:
        raise ValueError("alpha_scale must be finite and greater than zero")
    margin_units = float(config.get("margin_units", 0.0))
    if margin_units < 0 or not math.isfinite(margin_units):
        raise ValueError("margin_units must be finite and non-negative")
    ceiling_cfg = source.get("ceiling") or source.get("geometry", {}).get("ceiling") or config.get("ceiling")
    default_floor_clearance = float(config.get("floor_clearance_units", 0.0))
    default_ceiling_clearance = float(config.get("ceiling_clearance_units", 0.0))
    regions = []
    region_station_assignments: dict[str, list[dict[str, Any]]] = {}
    unresolved_components: list[dict[str, Any]] = []
    for index, component in enumerate(components, 1):
        component_points = [row["uvh"] for row in component]
        if len({(round(p[0], 10), round(p[1], 10)) for p in component_points}) < 3:
            unresolved_components.append({"camera_ids": [row.get("camera_id") for row in component],
                                          "station_ids": [row.get("station_id") for row in component],
                                          "reason": "fewer_than_three_spatially_distinct_positions"})
            continue
        component_spacing = _median_spacing(component_points)
        polygon = _alpha_boundary(component_points, max(component_spacing * alpha_scale, EPS))
        algorithm = "ALPHA_SHAPE"
        if len(polygon) < 3 or not _simple_polygon(polygon):
            polygon = _convex_hull(component_points); algorithm = "CONVEX_HULL_REVIEW_FALLBACK"
            warnings.append({"code": "CONCAVE_FOOTPRINT_UNSUPPORTED", "severity": "REVIEW", "message": "Alpha-shape topology was incomplete; convex fallback is a review proposal and is never auto-approved."})
        if _signed_area(polygon) < 0:
            polygon.reverse()
        if len(polygon) < 3 or not _simple_polygon(polygon):
            unresolved_components.append({"camera_ids": [row.get("camera_id") for row in component],
                                          "station_ids": [row.get("station_id") for row in component],
                                          "reason": "no_finite_simple_footprint"})
            continue
        if margin_units > 0:
            try:
                polygon = _offset_polygon(polygon, margin_units)
            except ValueError as exc:
                warnings.append({"code": "MARGIN_REJECTED", "severity": "REVIEW", "message": str(exc)})
                margin_units_used = 0.0
            else:
                margin_units_used = margin_units
        else:
            margin_units_used = 0.0
        rid = f"region-{index:02d}"
        if plane_viewer:
            floor_value = 0.0
            floor_class = plane_viewer["classification"]
            floor_evidence = plane_viewer.get("evidence", [])
        else:
            floor_value, floor_class, floor_evidence = None, "FLOOR_UNOBSERVED", []
        if ceiling_cfg and ceiling_cfg.get("height_along_floor_normal_units", ceiling_cfg.get("value")) is not None:
            ceiling_value = float(ceiling_cfg.get("height_along_floor_normal_units", ceiling_cfg.get("value")))
            ceiling_class = ceiling_cfg.get("classification", "INFERRED")
            ceiling_evidence = ceiling_cfg.get("evidence", [])
        else:
            ceiling_value, ceiling_class, ceiling_evidence = None, "CEILING_UNOBSERVED", []
            warnings.append({"code": "CEILING_UNOBSERVED", "severity": "BLOCKING", "message": f"No ceiling bound is available for {rid}; public movement cannot be enabled until reviewed."})
        station_ids = [row.get("station_id") or row.get("camera_id") or row.get("image_id") or row.get("image_name") for row in component]
        region_station_assignments[rid] = component
        region = {"id": rid, "polygon_uv": polygon, "coordinate_frame": "navigation_local_uv",
                  "algorithm": algorithm, "classification": "CAMERA_DERIVED_PROPOSAL",
                  "status": "PROPOSED", "confidence": "UNASSESSED",
                  "evidence": {"trusted_station_count": sum(1 for row in component if row["trust_explicit"]),
                               "candidate_station_count": len(component),
                               "provisional_station_count": sum(1 for row in component if not row["trust_explicit"]),
                               "station_ids": station_ids,
                               "median_nearest_neighbor_spacing_units": component_spacing,
                               "alpha_radius_units": component_spacing * alpha_scale,
                               "camera_occupied_footprint": "Camera locations are spatial evidence only; the footprint interior is not presumed traversable without review."},
                  "boundary_segments": [{"classification": "CAMERA_DERIVED", "inferred_margin_units": margin_units_used,
                                         "geometry_supported": False} for _ in polygon],
                  "vertical_bounds": {"floor": {"value": floor_value, "classification": floor_class, "clearance": default_floor_clearance, "evidence": floor_evidence},
                                       "ceiling": None if ceiling_value is None else {"value": ceiling_value, "classification": ceiling_class, "clearance": default_ceiling_clearance, "evidence": ceiling_evidence}},
                  "uncertainty": ["Camera trajectories do not prove the entire enclosed footprint is traversable.",
                                  "Boundary walls are not confirmed unless separately reviewed or supplied as geometric planes."]}
        regions.append(region)
    sparse_uvh = []
    sparse_input = source.get("sparse_points") or []
    if basis:
        for point in sparse_input:
            try:
                point_viewer = _finite_vector(point, 3, "sparse point")
                if not frame.get("coordinates_are_viewer"):
                    point_viewer = _transform_point(point_viewer, scale, rotation, translation)
                sparse_uvh.append(_project(point_viewer, basis))
            except (TypeError, ValueError):
                continue
    for region in regions:
        support_band = float(region["evidence"]["median_nearest_neighbor_spacing_units"])
        polygon = region["polygon_uv"]
        inside_count = near_boundary_count = near_boundary_inside_count = 0
        for point in sparse_uvh:
            uv = point[:2]
            inside = _point_in_polygon(uv, polygon)
            distance = min(_point_segment_distance(uv, vertex, polygon[(index + 1) % len(polygon)])
                           for index, vertex in enumerate(polygon))
            inside_count += int(inside)
            if distance <= support_band:
                near_boundary_count += 1
                near_boundary_inside_count += int(inside)
        region["evidence"]["sparse_geometry_observation"] = {
            "status": "OBSERVED_UNCLASSIFIED" if basis and sparse_input else "UNAVAILABLE_OR_FRAME_UNRESOLVED",
            "input_point_count": len(sparse_input), "finite_points_in_navigation_frame": len(sparse_uvh),
            "points_inside_candidate": inside_count, "points_within_station_spacing_of_candidate_boundary": near_boundary_count,
            "near_boundary_points_inside_candidate": near_boundary_inside_count,
            "boundary_band_units": support_band,
            "architectural_classification": "NOT_PERFORMED",
            "interpretation": "Sparse-point proximity is diagnostic only; it does not classify walls, floors, furniture, or traversability, and low density is not evidence of empty space."}
    transitions = []
    for item in (source.get("transitions") or config.get("transitions") or []):
        transitions.append({"from_region": item.get("from_region"), "to_region": item.get("to_region"),
                            "portal": {"segment_uv": item.get("segment_uv")}, "classification": item.get("classification", "REVIEW_PROPOSED"),
                            "approved": bool(item.get("approved", False)), "evidence": item.get("evidence", [])})
    if len(regions) > 1:
        warnings.append({"code": "MULTIPLE_REGIONS", "severity": "REVIEW", "message": f"{len(regions)} separate camera-supported regions are represented. No doorway or transition is inferred from proximity."})
    if len(regions) == 1 and len(components) == 1:
        convex = _convex_hull([row["uvh"] for row in retained])
        if len(regions[0]["polygon_uv"]) <= len(convex) and abs(abs(_signed_area(convex)) - abs(_signed_area(regions[0]["polygon_uv"]))) < max(1e-5, abs(_signed_area(convex)) * 0.01):
            regions[0]["topology"] = "CONVEX_OR_UNRESOLVED"
            warnings.append({"code": "CONVEXITY_NOT_ARCHITECTURALLY_CONFIRMED", "severity": "REVIEW", "message": "Camera coverage does not establish that the convex footprint perimeter is a wall or safe boundary."})
        else:
            regions[0]["topology"] = "CONCAVE_ALPHA_SHAPE"
    stations = []
    for row in retained:
        stations.append({"stationId": row["station_id"] or f"camera-{row['image_id'] or len(stations) + 1}",
                         "centerCommonViewer": row["center_viewer"], "representativeForwardCommonViewer": row["forward_viewer"],
                         "normalVisitorPositionCandidate": True, "planeCoordinates": row["uvh"][:2],
                         "regionId": next((rid for rid, members in region_station_assignments.items() if row in members), None),
                         "imageKeys": row["image_keys"]})
    if not transform_verified:
        warnings.append({"code": "VIEWER_ALIGNMENT_UNVERIFIED", "severity": "BLOCKING", "message": "The SfM-to-viewer transform is mathematically valid but not marked verified against the selected model's transform."})
    if not plane_viewer:
        warnings.append({"code": "NAVIGATION_INSUFFICIENT_GEOMETRY", "severity": "BLOCKING", "message": "A floor-aligned frame cannot be established without a floor plane."})
    if spacing > EPS and margin_units > 2.0 * spacing:
        warnings.append({"code": "EXTRAPOLATION_EXCEEDS_SUPPORTED_SCALE", "severity": "BLOCKING", "message": "Configured margin exceeds twice the observed median station spacing."})
    if margin_units > 0:
        warnings.append({"code": "INFERRED_NAVIGATION_MARGIN", "severity": "REVIEW", "message": f"The configured {margin_units:g} scene-unit extension is inferred; it has no metric interpretation."})
    provisional = [row for row in retained if not row["trust_explicit"]]
    if provisional:
        warnings.append({"code": "POSE_TRUST_UNASSESSED", "severity": "REVIEW", "message": f"{len(provisional)} camera poses lack an explicit trust classification and are only candidate evidence."})
    source_rec = dict(source.get("source_reconstruction", {}))
    source_rec.setdefault("identity", source.get("reconstruction_identity", {}))
    source_rec.setdefault("coordinate_frame", frame)
    source_rec["sparse_point_count"] = source.get("sparse_point_count", len(source.get("sparse_points", [])))
    transform_matrix = [[scale * rotation[i][j] for j in range(3)] + [translation[i]] for i in range(3)] + [[0, 0, 0, 1]]
    frame_doc = {"source_frame": frame.get("name", frame.get("convention", "UNSPECIFIED_SFM_FRAME")),
                 "navigation_frame": "floor-aligned local u/v with positive height along floor normal",
                 "viewer_frame": frame.get("viewer_frame", "PlayCanvas model frame"),
                 "source_to_viewer_matrix_row_major": transform_matrix,
                 "viewer_to_navigation": {"origin_viewer": basis["origin"] if basis else None,
                                          "u_viewer": basis["u"] if basis else None,
                                          "v_viewer": basis["v"] if basis else None,
                                          "up_viewer": basis["normal"] if basis else None},
                 "uniform_scale": scale, "scale_status": frame.get("scale_status", "UNKNOWN_NON_METRIC"),
                 "viewer_transform_verified": transform_verified, "inverse_stored": "transpose(rotation)/uniform_scale with inverse translation; represented by the orthonormal frame basis above",
                 "plane_definition": plane_viewer, "transform_provenance": frame.get("transform_provenance", "configured; verify against selected model before publication")}
    def center_record(row: dict[str, Any]) -> dict[str, Any]:
        return {"station_id": row["station_id"], "camera_id": row["camera_id"],
                "image_id": row["image_id"], "image_name": row["image_name"],
                "navigation_local_uvh": row["uvh"], "viewer_center": row["center_viewer"],
                "region_id": next((rid for rid, members in region_station_assignments.items() if row in members), None),
                "pose_trust": "TRUSTED" if row["trust_explicit"] else "PROVISIONAL",
                "trust_reason": row["trust_reason"]}
    candidate_centers = [center_record(row) for row in retained]
    trusted_centers = [center for center in candidate_centers if center["pose_trust"] == "TRUSTED"]
    provisional_centers = [center for center in candidate_centers if center["pose_trust"] == "PROVISIONAL"]
    pose_doc = {"input_pose_count": len(cameras), "trusted_input_count": sum(1 for row in projected if row["trust_explicit"]),
                "provisional_pose_count": len(provisional),
                "provisional_registered_pose_count": sum(1 for row in provisional if row["trust_reason"] == "COLMAP_REGISTERED_UNASSESSED"),
                "pose_trust_source": "explicit configured trust classification; otherwise poses remain provisional candidate evidence",
                "used_camera_station_count": len(retained), "excluded_pose_count": len(pose_exclusions), "excluded_poses": pose_exclusions,
                "unresolved_components": unresolved_components,
                "candidate_camera_centers": candidate_centers,
                "trusted_camera_centers": trusted_centers,
                "provisional_camera_centers": provisional_centers,
                "provisional_pose_reviewed": False,
                "outlier_method": {"algorithm": "isolated nearest-neighbor screening", "factor": outlier_factor, "reference_spacing_units": spacing,
                                   "note": "Isolated poses are retained for diagnosis; disconnected room clusters remain separate regions."},
                "camera_intrinsics_available": bool(source.get("camera_models")), "sparse_point_count": source_rec.get("sparse_point_count", 0)}
    pose_doc["vertical_spread"] = {"height_range_units": vertical_spread, "review_threshold_units": vertical_limit,
                                    "possible_multi_level_or_slope": vertical_spread > vertical_limit if vertical_limit > 0 else False,
                                    "interpretation": "Camera height variation is a review signal only; it does not estimate floor levels."}
    artifact = {"schema_version": "exhibition-archive.navigation.v1", "artifact_version": 1,
                "id": f"{source_rec.get('identity', {}).get('reconstruction_id', source_rec.get('identity', {}).get('directory_name', 'sfm'))}-navigation-proposal-v1",
                "status": "NAVIGATION_NEEDS_REVIEW", "source_reconstruction": source_rec, "coordinate_frame": frame_doc,
                "pose_evidence": pose_doc, "regions": regions, "transitions": transitions,
                "floor_constraints": [{"region_id": r["id"], **r["vertical_bounds"]["floor"]} for r in regions],
                "ceiling_constraints": [{"region_id": r["id"], **(r["vertical_bounds"].get("ceiling") or {"value": None, "classification": "CEILING_UNOBSERVED"})} for r in regions],
                "space_assumption": {"initial_model": "single floor-aligned level per region", "multi_level_suspected": pose_doc["vertical_spread"]["possible_multi_level_or_slope"],
                                     "requires_review_when_suspected": True},
                "camera_clearance": {"horizontal_units": float(config.get("camera_clearance_units", 0.0)), "distance_unit": "reconstruction/viewer scene units; metric scale is not assumed", "interior_object_collision": False},
                "guided_paths": source.get("guided_paths", config.get("guided_paths", [])), "viewpoints": source.get("viewpoints", config.get("viewpoints", [])),
                "review": {"status": "REQUIRED", "approved": False, "decisions": [], "warnings": warnings,
                           "required_checks": ["Confirm every region boundary", "Confirm floor and ceiling bounds per region", "Confirm any doorway transition", "Confirm default camera and guided paths"]},
                "validation": {}, "provenance": {"generator": "exhibition_archive.navigation", "generator_version": "0.1.0",
                                                  "algorithm": "camera-neighbor components + 2D alpha shape with convex review fallback", "configuration": config,
                                                  "input_station_ids": [row["station_id"] for row in projected],
                                                  "source_files": source.get("source_files", []), "derived_without_external_services": True}}
    result = validate_navigation_artifact(artifact)
    containment = []
    for region in regions:
        members = region_station_assignments[region["id"]]
        outside = [row.get("station_id") for row in members if not _point_in_polygon(row["uvh"][:2], region["polygon_uv"])]
        outside_trusted = [row.get("station_id") for row in members if row["trust_explicit"] and not _point_in_polygon(row["uvh"][:2], region["polygon_uv"])]
        outside_provisional = [row.get("station_id") for row in members if not row["trust_explicit"] and not _point_in_polygon(row["uvh"][:2], region["polygon_uv"])]
        containment.append({"region_id": region["id"], "trusted_station_count": sum(1 for row in members if row["trust_explicit"]),
                            "candidate_station_count": len(members), "outside_station_ids": outside,
                            "outside_trusted_station_ids": outside_trusted, "outside_provisional_station_ids": outside_provisional,
                            "all_stations_inside_or_on": not outside})
    result["station_containment"] = containment
    result["warnings"] = warnings
    result["evidence_assessment"] = {"floor": "SUPPORTED" if plane_viewer else "MISSING", "ceiling": "SUPPORTED_OR_REVIEW_CANDIDATE" if ceiling_cfg else "MISSING",
                                     "architectural_walls": "UNCONFIRMED unless evidence/review is attached", "scale": frame_doc["scale_status"],
                                     "region_connectivity": "EXPLICIT_TRANSITIONS_ONLY"}
    if any(item["outside_trusted_station_ids"] for item in containment):
        result["errors"].append("trusted_station_outside_proposed_region")
        result["status"] = "INVALID"
    if any(item["outside_provisional_station_ids"] for item in containment):
        warnings.append({"code": "PROVISIONAL_STATION_OUTSIDE_PROPOSED_REGION", "severity": "REVIEW",
                         "message": "One or more provisional camera stations fall outside their candidate region; inspect the pose and region assignments before approval."})
    if (not plane_viewer or len(retained) < 3 or not regions
            or any(not region.get("vertical_bounds", {}).get("ceiling") for region in regions)):
        result["status"] = "NAVIGATION_INSUFFICIENT_GEOMETRY"
    artifact["validation"] = result
    artifact["status"] = ("NAVIGATION_INSUFFICIENT_GEOMETRY" if result["status"] == "NAVIGATION_INSUFFICIENT_GEOMETRY"
                           else "NAVIGATION_INVALID" if result["status"] == "INVALID" else "NAVIGATION_NEEDS_REVIEW")
    # Compatibility for the existing static PlayCanvas visitor runtime. The new
    # contract remains authoritative; this is a generated projection of it.
    if basis and regions:
        plane_basis = {"normalUnit": basis["normal"], "offset": -_dot(basis["normal"], basis["origin"]),
                       "pointOnPlaneCommonViewer": basis["origin"], "basisU": basis["u"], "basisV": basis["v"],
                       "construction": "floor-aligned basis from explicit plane normal; no global-up assumption"}
        vertices_viewer = [_unproject([uv[0], uv[1], 0], basis) for uv in regions[0]["polygon_uv"]]
        viewer_regions = []
        for region in regions:
            viewer_regions.append({"regionId": region["id"], "vertices2d": region["polygon_uv"],
                                   "floor": region["vertical_bounds"]["floor"], "ceiling": region["vertical_bounds"].get("ceiling"),
                                   "classification": region["classification"], "status": region["status"]})
        envelope = {"schemaVersion": 1, "id": artifact["id"], "status": "NAVIGATION_NEEDS_REVIEW",
                    "classification": "CAMERA_DERIVED_PROPOSAL", "coordinateFrame": "Explicit floor-aligned navigation frame mapped to PlayCanvas viewer coordinates",
                    "sourceModelSha256": source.get("source_model_sha256"), "sourceReconstruction": source_rec,
                    "horizontalBoundary": {"candidateId": artifact["id"], "algorithm": regions[0]["algorithm"],
                                           "classification": "CAMERA_DERIVED_PROPOSAL", "closed": True,
                                           "planeBasis": plane_basis, "vertices2d": regions[0]["polygon_uv"],
                                           "verticesCommonViewer": vertices_viewer,
                                           "regions": viewer_regions, "transitions": transitions,
                                           "cameraStationInclusion": {"insideOrOn": sum(x["trusted_station_count"] - len(x["outside_trusted_station_ids"]) for x in containment),
                                                                         "total": sum(x["trusted_station_count"] for x in containment)},
                                           "marginUnits": margin_units, "cameraClearanceUnits": artifact["camera_clearance"]["horizontal_units"]},
                    "floor": {"normal": basis["normal"], "offset": -_dot(basis["normal"], basis["origin"]), "clearance": default_floor_clearance,
                              "classification": plane_viewer["classification"] if plane_viewer else "FLOOR_UNOBSERVED"},
                    "ceiling": {"heightAlongFloorNormal": ceiling_cfg.get("height_along_floor_normal_units", ceiling_cfg.get("value")) if ceiling_cfg else None,
                                "clearance": default_ceiling_clearance, "classification": ceiling_cfg.get("classification", "INFERRED") if ceiling_cfg else "CEILING_UNOBSERVED"},
                    "navigation": {"interiorObjectCollision": False, "freeXYZWithinEnvelope": True, "regionConnectivity": "explicit approved portals only"},
                    "limitations": ["Proposal only; camera coverage is not proof of traversability.", "Scene units are non-metric unless calibration is recorded."]}
        policy = {"schemaVersion": 1, "status": "NEEDS_REVIEW", "sourceModelSha256": source.get("source_model_sha256"),
                  "cameraPolicy": "architectural-envelope", "navigation": {"interiorObjectCollision": False, "freeXYZWithinEnvelope": True,
                                                                            "sweptHorizontalCrossingCheck": True, "publicApprovalRequired": True}}
        default_station_id = config.get("default_station_id", source.get("default_station_id"))
        if default_station_id not in {station["stationId"] for station in stations}:
            default_station_id = stations[0]["stationId"] if stations else None
        artifact["viewer_compatibility"] = {"envelope": envelope, "runtime_policy": policy,
                                            "stations": {"schemaVersion": 1, "defaultStationId": default_station_id, "stations": stations},
                                            "camera_centers": {"schemaVersion": 1, "stations": [{"stationId": s["stationId"], "planeCoordinates": s["planeCoordinates"]} for s in stations]},
                                            "guided_tours": {"schemaVersion": 1, "routes": artifact["guided_paths"]}}
    final_validation = validate_navigation_artifact(artifact)
    final_validation["errors"] = list(dict.fromkeys([*result.get("errors", []), *final_validation.get("errors", [])]))
    final_validation.update({key: value for key, value in result.items() if key not in final_validation})
    fatal_errors = list(final_validation["errors"])
    if not plane_viewer:
        fatal_errors = [error for error in fatal_errors if error != "missing_viewer_to_navigation_basis"]
    if not regions:
        fatal_errors = [error for error in fatal_errors if error != "no_regions"]
    if fatal_errors:
        final_validation["status"] = "INVALID"
        artifact["status"] = "NAVIGATION_INVALID"
    elif result["status"] == "NAVIGATION_INSUFFICIENT_GEOMETRY":
        final_validation["status"] = "NAVIGATION_INSUFFICIENT_GEOMETRY"
        artifact["status"] = "NAVIGATION_INSUFFICIENT_GEOMETRY"
    artifact["validation"] = final_validation
    return artifact


def render_navigation_review_html(artifact: dict[str, Any]) -> str:
    """Create a self-contained top-down reviewer/editor with JSON download."""
    import html
    import json
    payload = json.dumps(artifact, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    warnings = artifact.get("review", {}).get("warnings", [])
    warning_items = "".join(f"<li><b>{html.escape(item.get('severity','INFO'))}</b> {html.escape(item.get('message',''))}</li>" for item in warnings)
    page = r'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Navigation proposal review</title>
<style>body{font:15px system-ui;margin:0;background:#101827;color:#e7edf6;display:grid;grid-template-columns:minmax(500px,2fr) minmax(320px,1fr);height:100vh}main{padding:18px;min-height:0;display:flex;flex-direction:column}aside{padding:20px;background:#192538;overflow:auto}h1{font-size:20px;margin:0 0 12px}p,li{line-height:1.5;color:#c4d0df}svg{width:100%;height:100%;min-height:400px;background:#0b1220;border:1px solid #33445c;border-radius:8px;touch-action:none}.region{fill:#53a5b344;stroke:#6bc5da;stroke-width:2;vector-effect:non-scaling-stroke}.vertex{fill:#ffcc66;stroke:#231b0b;stroke-width:1;vector-effect:non-scaling-stroke;cursor:move}.camera{fill:#c9e8ff;opacity:.8}.cameraProvisional{fill:#ff9b65;opacity:.9}.cameraExcluded{fill:#808a98;opacity:.8}.region-label{fill:white;font-size:14px}button{background:#357a89;border:0;color:white;border-radius:6px;padding:10px 14px;font-size:14px;cursor:pointer;margin:8px 4px 8px 0}label{display:block;margin-top:12px;color:#ced9e8}input,textarea{box-sizing:border-box;width:100%;padding:7px;background:#101827;border:1px solid #52647d;border-radius:4px;color:white}.tag{color:#f0c86f}.tiny{font-size:12px;color:#94a8bf}</style>
<body><main><h1>Top-down navigation proposal</h1><div class="tiny">Blue dots are explicitly trusted stations; orange dots are provisional and grey dots are excluded. Drag vertices, edit vertical limits, review portals and save a versioned draft to the local Engine.</div><svg id="map" viewBox="0 0 1000 760" aria-label="Top-down navigation proposal"></svg></main><aside><h1 id="title"></h1><p id="summary"></p><ul>WARNINGS_PLACEHOLDER</ul><p id="frame"></p><label><input id="transformConfirmed" type="checkbox"> I verified this SfM-to-viewer transform against the selected model.</label><div id="bounds"></div><h2>Room connection</h2><label>From region <select id="fromRegion"></select></label><label>To region <select id="toRegion"></select></label><button id="portalButton">Draw doorway portal</button><p id="portalStatus" class="tiny">No portal edit active.</p><h2>Initial camera</h2><label>Default camera station <select id="defaultCamera"></select></label><label><input id="acceptProvisionalPoses" type="checkbox"> I reviewed and accept every provisional camera pose as trusted evidence.</label><h2>Decision</h2><label>Reviewer identity <input id="reviewer" value="Local reviewer"></label><label>Rationale <textarea id="rationale" rows="3">Reviewed navigation boundary and vertical limits.</textarea></label><p id="reviewStatus" class="tag">Engine approval state: CURRENT_ARTIFACT_STATUS</p><button id="saveDraft">Save draft to Engine</button><button id="approveNavigation">Approve navigation</button><button id="rejectNavigation">Reject navigation</button><button id="download">Download corrected navigation JSON</button><p class="tiny">Drafts are versioned in the project. Approval runs Engine geometry checks and records the exact artifact hash, reviewer, rationale, timestamp and input fingerprint.</p></aside>
<script>
const doc=ARTIFACT_PLACEHOLDER, svg=document.querySelector('#map'), ns='http://www.w3.org/2000/svg';let expectedSha='CURRENT_ARTIFACT_SHA256',lastSavedEditorState=JSON.stringify(doc);
document.querySelector('#title').textContent=doc.id;
document.querySelector('#summary').textContent=`${doc.status} · ${doc.regions.length} proposed region(s) · ${doc.pose_evidence.used_camera_station_count} camera station(s) · frame scale ${doc.coordinate_frame.scale_status}`;
document.querySelector('#frame').textContent=`Source frame: ${doc.coordinate_frame.source_frame} · Viewer frame: ${doc.coordinate_frame.viewer_frame} · uniform scale ${doc.coordinate_frame.uniform_scale} · transform verified: ${doc.coordinate_frame.viewer_transform_verified}`;
document.querySelector('#transformConfirmed').checked=!!doc.coordinate_frame.viewer_transform_verified;
const candidates=doc.pose_evidence.candidate_camera_centers??doc.pose_evidence.trusted_camera_centers??[],excluded=doc.pose_evidence.excluded_poses??[];
let all=[...candidates.map(x=>x.navigation_local_uvh),...excluded.map(x=>x.navigation_local_uvh).filter(Array.isArray),...doc.regions.flatMap(r=>r.polygon_uv)];if(!all.length)all=[[-1,-1],[1,1]];
let xs=all.map(x=>x[0]),ys=all.map(x=>x[1]),minX=Math.min(...xs),maxX=Math.max(...xs),minY=Math.min(...ys),maxY=Math.max(...ys),pad=40;
const sx=x=>pad+(x-minX)/(maxX-minX||1)*(1000-2*pad),sy=y=>760-pad-(y-minY)/(maxY-minY||1)*(760-2*pad),ux=x=>minX+(x-pad)/(1000-2*pad)*(maxX-minX||1),uy=y=>minY+(760-pad-y)/(760-2*pad)*(maxY-minY||1);
let drag=null,portalMode=false,portalPoints=[];
function el(name,attrs={}){const n=document.createElementNS(ns,name);for(const [k,v] of Object.entries(attrs))n.setAttribute(k,v);return n}
function draw(){svg.replaceChildren();for(const r of doc.regions){let pts=r.polygon_uv.map(p=>`${sx(p[0])},${sy(p[1])}`).join(' ');svg.append(el('polygon',{points:pts,class:'region'}));r.polygon_uv.forEach((p,i)=>{const v=el('circle',{cx:sx(p[0]),cy:sy(p[1]),r:7,class:'vertex','data-region':r.id,'data-index':i});svg.append(v)});const t=el('text',{x:sx(r.polygon_uv[0][0])+8,y:sy(r.polygon_uv[0][1])-8,class:'region-label'});t.textContent=r.id;svg.append(t)}for(const tr of doc.transitions??[]){const s=tr.portal?.segment_uv;if(s)svg.append(el('line',{x1:sx(s[0][0]),y1:sy(s[0][1]),x2:sx(s[1][0]),y2:sy(s[1][1]),stroke:'#ff9b65','stroke-width':5,'vector-effect':'non-scaling-stroke'}))}for(const c of candidates){svg.append(el('circle',{cx:sx(c.navigation_local_uvh[0]),cy:sy(c.navigation_local_uvh[1]),r:2.6,class:c.pose_trust==='PROVISIONAL'?'cameraProvisional':'camera'}))}for(const c of excluded){if(c.navigation_local_uvh?.length===3)svg.append(el('circle',{cx:sx(c.navigation_local_uvh[0]),cy:sy(c.navigation_local_uvh[1]),r:2.6,class:'cameraExcluded'}))}}
draw();
svg.addEventListener('pointerdown',e=>{if(portalMode||!e.target.classList.contains('vertex'))return;drag={region:e.target.dataset.region,index:+e.target.dataset.index};svg.setPointerCapture(e.pointerId)});
svg.addEventListener('pointermove',e=>{if(!drag)return;let p=svg.createSVGPoint();p.x=e.clientX;p.y=e.clientY;p=p.matrixTransform(svg.getScreenCTM().inverse());let r=doc.regions.find(x=>x.id===drag.region);r.polygon_uv[drag.index]=[ux(p.x),uy(p.y)];draw()});
svg.addEventListener('pointerup',()=>{drag=null});
function localClick(e){let p=svg.createSVGPoint();p.x=e.clientX;p.y=e.clientY;p=p.matrixTransform(svg.getScreenCTM().inverse());return [ux(p.x),uy(p.y)]}
svg.addEventListener('click',e=>{if(!portalMode)return;portalPoints.push(localClick(e));if(portalPoints.length===1){document.querySelector('#portalStatus').textContent='Select the second end of the doorway portal.';return}const from=document.querySelector('#fromRegion').value,to=document.querySelector('#toRegion').value;if(!from||!to||from===to){document.querySelector('#portalStatus').textContent='Choose two different regions first.';portalPoints=[];return}doc.transitions??=[];doc.transitions.push({from_region:from,to_region:to,portal:{segment_uv:portalPoints},classification:'REVIEW_PROPOSED',approved:false,evidence:['Reviewer-marked portal; awaiting explicit approval']});portalPoints=[];portalMode=false;document.querySelector('#portalStatus').textContent=`Portal proposed between ${from} and ${to}; crossing remains blocked until the artifact is approved.`;draw()});
for(const r of doc.regions){for(const id of ['fromRegion','toRegion']){const o=document.createElement('option');o.value=r.id;o.textContent=r.id;document.querySelector('#'+id).append(o)}}
document.querySelector('#portalButton').onclick=()=>{portalMode=true;portalPoints=[];document.querySelector('#portalStatus').textContent='Click the two ends of a supported doorway on the map.'};
const defaultCamera=document.querySelector('#defaultCamera');for(const c of candidates){if(!c.station_id)continue;const o=document.createElement('option');o.value=c.station_id;o.textContent=`${c.station_id} · ${c.pose_trust}`;defaultCamera.append(o)}defaultCamera.value=doc.viewer_compatibility?.stations?.defaultStationId??'';
defaultCamera.onchange=()=>{doc.viewer_compatibility??={};doc.viewer_compatibility.stations??={stations:[]};doc.viewer_compatibility.stations.defaultStationId=defaultCamera.value};
const bounds=document.querySelector('#bounds');for(const r of doc.regions){const h=document.createElement('div');h.innerHTML=`<h2>${r.id}</h2><p>Boundary: ${r.algorithm} · ${r.evidence.trusted_station_count} trusted / ${r.evidence.candidate_station_count} candidate stations · status ${r.status}</p><label>Floor height (local units) <input data-region="${r.id}" data-axis="floor" value="${r.vertical_bounds.floor.value??''}"></label><label>Ceiling height (local units) <input data-region="${r.id}" data-axis="ceiling" value="${r.vertical_bounds.ceiling?.value??''}"></label><p class="tiny">Floor evidence: ${r.vertical_bounds.floor.classification??'UNCLASSIFIED'} · Ceiling evidence: ${r.vertical_bounds.ceiling?.classification??'UNCLASSIFIED'} · Units follow the reconstruction scale; the engine does not assume meters.</p>`;bounds.append(h)}
function syncEditorState(){for(const input of document.querySelectorAll('input[data-axis]')){const r=doc.regions.find(x=>x.id===input.dataset.region),v=input.value.trim()===''?null:Number(input.value);if(input.dataset.axis==='floor')r.vertical_bounds.floor.value=v;else{r.vertical_bounds.ceiling??={classification:'INFERRED',clearance:0,evidence:[]};r.vertical_bounds.ceiling.value=v}}doc.coordinate_frame.viewer_transform_verified=document.querySelector('#transformConfirmed').checked;if(document.querySelector('#acceptProvisionalPoses').checked){for(const c of candidates){if(c.pose_trust==='PROVISIONAL'){c.pose_trust='TRUSTED_BY_REVIEW';c.trust_reason='Reviewer accepted provisional camera pose';}}doc.pose_evidence.provisional_pose_reviewed=true;doc.pose_evidence.provisional_camera_centers=[];doc.pose_evidence.trusted_camera_centers=candidates.filter(c=>c.pose_trust!=='PROVISIONAL')}if(doc.viewer_compatibility?.stations)doc.viewer_compatibility.stations.defaultStationId=defaultCamera.value}
document.querySelector('#download').onclick=()=>{syncEditorState();const blob=new Blob([JSON.stringify(doc,null,2)+'\n'],{type:'application/json'}),a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='navigation-review-candidate.json';a.click();URL.revokeObjectURL(a.href)};
async function submitReview(endpoint,needsRationale){syncEditorState();const reviewer=document.querySelector('#reviewer').value.trim(),rationale=document.querySelector('#rationale').value.trim();if(!reviewer||needsRationale&&!rationale){document.querySelector('#reviewStatus').textContent='Reviewer and rationale are required.';return}try{if(endpoint.endsWith('/approve')&&JSON.stringify(doc)!==lastSavedEditorState){const draftBody={artifact:doc,reviewer,expected_sha256:expectedSha},draftResponse=await fetch('/api/navigation/draft',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(draftBody)}),draftResult=await draftResponse.json();if(!draftResponse.ok)throw new Error(draftResult.error??`HTTP ${draftResponse.status}`);expectedSha=draftResult.sha256;doc.artifact_version=draftResult.artifact_version;lastSavedEditorState=JSON.stringify(doc)}const body={artifact:doc,reviewer,rationale,expected_sha256:expectedSha},response=await fetch(endpoint,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),result=await response.json();if(!response.ok)throw new Error(result.error??`HTTP ${response.status}`);if(result.sha256){expectedSha=result.sha256;doc.artifact_version=result.artifact_version;lastSavedEditorState=JSON.stringify(doc)}document.querySelector('#reviewStatus').textContent=`${result.status} · ${result.approval_status??result.validation?.status??''} · ${result.path??result.approved_artifact??''}`;if(result.approved_sha256)expectedSha=result.approved_sha256}catch(error){document.querySelector('#reviewStatus').textContent=`Review action blocked: ${error.message}`}}
document.querySelector('#saveDraft').onclick=()=>submitReview('/api/navigation/draft',false);
document.querySelector('#approveNavigation').onclick=()=>submitReview('/api/navigation/approve',true);
document.querySelector('#rejectNavigation').onclick=()=>submitReview('/api/navigation/reject',true);
</script></body></html>'''
    page = page.replace('<button id="portalButton">Draw doorway portal</button>',
                        '<button id="portalButton">Draw doorway portal</button><label>Saved portal <select id="portalSelect"></select></label><div class="row"><label>Endpoint A U <input id="portalA0" type="number" step="0.001"></label><label>V <input id="portalA1" type="number" step="0.001"></label></div><div class="row"><label>Endpoint B U <input id="portalB0" type="number" step="0.001"></label><label>V <input id="portalB1" type="number" step="0.001"></label></div><button id="applyPortalEdit">Apply endpoint edit</button>')
    portal_editor = r'''function syncPortalEditor(){const select=document.querySelector('#portalSelect'),transitions=doc.transitions??[],selected=Number(select.value);select.replaceChildren();transitions.forEach((tr,i)=>{const o=document.createElement('option');o.value=String(i);o.textContent=`${tr.from_region} → ${tr.to_region} · ${tr.approved?'approved':'proposed'}`;select.append(o)});if(transitions.length){select.value=String(Math.min(Number.isInteger(selected)?selected:0,transitions.length-1));const segment=transitions[Number(select.value)]?.portal?.segment_uv??[[],[]];for(const [id,value] of [['portalA0',segment[0]?.[0]],['portalA1',segment[0]?.[1]],['portalB0',segment[1]?.[0]],['portalB1',segment[1]?.[1]]])document.querySelector('#'+id).value=Number.isFinite(value)?value:''}else for(const id of ['portalA0','portalA1','portalB0','portalB1'])document.querySelector('#'+id).value='';document.querySelector('#applyPortalEdit').disabled=!transitions.length}
document.querySelector('#portalSelect').onchange=syncPortalEditor;
document.querySelector('#applyPortalEdit').onclick=()=>{const index=Number(document.querySelector('#portalSelect').value),tr=(doc.transitions??[])[index];if(!tr)return;const vals=['portalA0','portalA1','portalB0','portalB1'].map(id=>Number(document.querySelector('#'+id).value));if(!vals.every(Number.isFinite)){document.querySelector('#portalStatus').textContent='Portal endpoints must be finite numbers.';return}tr.portal.segment_uv=[[vals[0],vals[1]],[vals[2],vals[3]]];tr.approved=false;tr.classification='REVIEW_PROPOSED';document.querySelector('#portalStatus').textContent='Portal endpoints edited; save the draft to validate and persist this proposal.';draw();syncPortalEditor()};
syncPortalEditor();
'''
    page = page.replace("const defaultCamera=document.querySelector('#defaultCamera');",
                        portal_editor + "const defaultCamera=document.querySelector('#defaultCamera');")
    page = page.replace("portalMode=false;document.querySelector('#portalStatus').textContent=`Portal proposed between ${from} and ${to}; crossing remains blocked until the artifact is approved.`;draw()});",
                        "portalMode=false;document.querySelector('#portalStatus').textContent=`Portal proposed between ${from} and ${to}; crossing remains blocked until the artifact is approved.`;draw();syncPortalEditor()});")
    page = page.replace("WARNINGS_PLACEHOLDER", warning_items).replace("ARTIFACT_PLACEHOLDER", payload)
    return page


def approve_navigation_artifact(artifact: dict[str, Any], reviewer: str, rationale: str, *, timestamp: str, source_artifact_sha256: str) -> dict[str, Any]:
    """Return an auditable reviewed version; approval never edits the proposal in place."""
    if not reviewer.strip() or not rationale.strip():
        raise ValueError("Reviewer and rationale are required")
    reviewed = copy.deepcopy(artifact)
    result = validate_navigation_artifact(artifact)
    if result["status"] != "PASS":
        raise ValueError(f"Navigation artifact is technically invalid: {result['errors']}")
    if not reviewed.get("coordinate_frame", {}).get("viewer_transform_verified"):
        raise ValueError("Navigation cannot be approved until the SfM-to-viewer transform is verified")
    if reviewed.get("pose_evidence", {}).get("unresolved_components"):
        raise ValueError("Navigation cannot be approved while camera components lack a simple region footprint")
    pose_evidence = reviewed.setdefault("pose_evidence", {})
    candidates = pose_evidence.get("candidate_camera_centers", pose_evidence.get("trusted_camera_centers", []))
    provisional = [center for center in candidates if center.get("pose_trust") == "PROVISIONAL"]
    had_provisional = bool(pose_evidence.get("provisional_camera_centers")) or bool(provisional)
    if had_provisional and not pose_evidence.get("provisional_pose_reviewed"):
        raise ValueError("Navigation cannot be approved until every provisional camera pose is explicitly reviewed")
    if provisional:
        raise ValueError("Provisional camera poses remain unaccepted; use the review editor to record the pose decision")
    region_map = {region["id"]: region for region in reviewed["regions"]}
    for center in candidates:
        rid = center.get("region_id")
        region = region_map.get(rid) if rid else _region_for(center.get("navigation_local_uvh", [])[:2], reviewed["regions"])
        if region is None or not _point_in_polygon(center.get("navigation_local_uvh", [])[:2], region["polygon_uv"]):
            raise ValueError(f"Camera station {center.get('station_id')} is not assigned to a supported navigation region")
        center["region_id"] = region["id"]
        if center.get("pose_trust") == "TRUSTED_BY_REVIEW":
            center["pose_trust"] = "TRUSTED"
            center["trust_reason"] = "Reviewer accepted provisional camera pose"
    pose_evidence["candidate_camera_centers"] = candidates
    pose_evidence["trusted_camera_centers"] = candidates
    pose_evidence["provisional_camera_centers"] = []
    reviewer_accepted_count = sum(1 for center in candidates if center.get("trust_reason") == "Reviewer accepted provisional camera pose")
    pose_evidence["trusted_input_count"] = max(int(pose_evidence.get("trusted_input_count", 0)) + reviewer_accepted_count,
                                                len(candidates))
    pose_evidence["provisional_pose_count"] = 0
    pose_evidence["provisional_registered_pose_count"] = 0
    pose_evidence["pose_trust_source"] = "configured classification plus explicit reviewer decisions"
    pose_evidence["provisional_pose_reviewed"] = had_provisional or pose_evidence.get("provisional_pose_reviewed", False)
    for region in reviewed["regions"]:
        members = [center for center in candidates if center.get("region_id") == region["id"]]
        region.setdefault("evidence", {})["candidate_station_count"] = len(members)
        region["evidence"]["trusted_station_count"] = sum(1 for center in members if center.get("pose_trust") != "PROVISIONAL")
        region["evidence"]["provisional_station_count"] = sum(1 for center in members if center.get("pose_trust") == "PROVISIONAL")
    reviewed["artifact_version"] = int(artifact.get("artifact_version", 1)) + 1
    reviewed["status"] = "NAVIGATION_APPROVED_BY_REVIEW"
    reviewed["regions"] = []
    for original in artifact["regions"]:
        region = {**original, "status": "APPROVED_BY_REVIEW", "classification": "REVIEWED_NAVIGATION_BOUNDARY"}
        region["boundary_segments"] = [{**segment, "classification": "REVIEWED_BOUNDARY", "reviewer": reviewer} for segment in original.get("boundary_segments", [])]
        vertical = dict(original.get("vertical_bounds", {}))
        for axis in ("floor", "ceiling"):
            bound = vertical.get(axis)
            if bound is None or bound.get("value") is None:
                raise ValueError(f"Region {original['id']} has no {axis} value; add it to the reviewed artifact")
            vertical[axis] = {**bound, "classification": bound.get("classification", "UNCLASSIFIED"),
                              "review_status": "REVIEWED", "reviewed_by": reviewer, "reviewed_at_utc": timestamp}
        region["vertical_bounds"] = vertical
        region["boundary_segments"] = [{"classification": "REVIEWED_BOUNDARY", "reviewer": reviewer,
                                         "inferred_margin_units": 0.0} for _ in region.get("polygon_uv", [])]
        reviewed["regions"].append(region)
    reviewed["floor_constraints"] = [{"region_id": region["id"], **region["vertical_bounds"]["floor"]} for region in reviewed["regions"]]
    reviewed["ceiling_constraints"] = [{"region_id": region["id"], **region["vertical_bounds"]["ceiling"]} for region in reviewed["regions"]]
    initial_check = validate_navigation_artifact(reviewed).get("initial_camera_validation", {})
    if initial_check.get("status") != "PASS":
        raise ValueError(f"Default camera is not valid inside the reviewed navigation region: {initial_check}")
    for transition in reviewed.get("transitions", []):
        if transition.get("classification") == "REVIEW_PROPOSED":
            transition["classification"] = "REVIEWED_PORTAL"
        transition["approved"] = True
        transition["reviewed_by"] = reviewer
        transition["reviewed_at_utc"] = timestamp
    reviewed["review"] = {**reviewed.get("review", {}), "status": "APPROVED", "approved": True,
                           "reviewer": reviewer, "rationale": rationale, "reviewed_at_utc": timestamp,
                           "source_artifact_sha256": source_artifact_sha256,
                           "decisions": [*reviewed.get("review", {}).get("decisions", []),
                                         {"decision": "APPROVE", "reviewer": reviewer, "rationale": rationale,
                                          "timestamp_utc": timestamp, "source_artifact_sha256": source_artifact_sha256}]}
    reviewed["validation"] = {**result, "status": "PASS", "public_ready": True,
                              "review_status": "NAVIGATION_APPROVED_BY_REVIEW"}
    if reviewed.get("viewer_compatibility"):
        envelope = reviewed["viewer_compatibility"]["envelope"]
        envelope["status"] = "NAVIGATION_APPROVED_BY_REVIEW"
        h = envelope.setdefault("horizontalBoundary", {})
        h["regions"] = [{"regionId": region["id"], "vertices2d": region["polygon_uv"],
                          "floor": region["vertical_bounds"]["floor"], "ceiling": region["vertical_bounds"]["ceiling"],
                          "classification": region["classification"], "status": "APPROVED_BY_REVIEW"} for region in reviewed["regions"]]
        if reviewed["regions"]:
            h["vertices2d"] = reviewed["regions"][0]["polygon_uv"]
            h["algorithm"] = reviewed["regions"][0]["algorithm"]
            h["candidateId"] = reviewed["regions"][0]["id"]
            viewer_basis = reviewed.get("coordinate_frame", {}).get("viewer_to_navigation", {})
            if viewer_basis.get("origin_viewer"):
                basis = {"origin": viewer_basis["origin_viewer"], "u": viewer_basis["u_viewer"],
                         "v": viewer_basis["v_viewer"], "normal": viewer_basis["up_viewer"]}
                h["verticesCommonViewer"] = [_unproject([uv[0], uv[1], 0], basis) for uv in h["vertices2d"]]
            floor = reviewed["regions"][0]["vertical_bounds"]["floor"]
            ceiling = reviewed["regions"][0]["vertical_bounds"]["ceiling"]
            envelope["floor"] = {**envelope.get("floor", {}), "clearance": floor.get("clearance", 0.0), "classification": floor.get("classification")}
            envelope["ceiling"] = {**envelope.get("ceiling", {}), "heightAlongFloorNormal": ceiling["value"],
                                    "clearance": ceiling.get("clearance", 0.0), "classification": ceiling.get("classification")}
        h["transitions"] = reviewed.get("transitions", [])
        h["cameraStationInclusion"] = {"insideOrOn": len(reviewed.get("pose_evidence", {}).get("candidate_camera_centers", [])),
                                       "total": len(reviewed.get("pose_evidence", {}).get("candidate_camera_centers", []))}
        policy = reviewed["viewer_compatibility"]["runtime_policy"]
        policy["status"] = "APPROVED_BY_REVIEW"
        policy["review"] = reviewed["review"]
        station_regions = {center.get("station_id"): center.get("region_id") for center in reviewed.get("pose_evidence", {}).get("candidate_camera_centers", [])}
        for station in reviewed["viewer_compatibility"].get("stations", {}).get("stations", []):
            station["regionId"] = station_regions.get(station.get("stationId"))
    return reviewed

