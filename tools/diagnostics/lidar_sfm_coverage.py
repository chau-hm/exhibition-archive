#!/usr/bin/env python3
"""Read-only diagnostic for a saved rejected COLMAP–LiDAR similarity transform.

The utility reads the Engine's JSON provenance records and source geometry. It
does not open the Engine SQLite database, perform global registration, or write
to the input workspace. All generated files go beneath --output-dir.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import struct
import sys
from collections import deque
from pathlib import Path
from typing import Any, Iterable

# Keep numerical runtimes bounded even when this utility is called directly.
for _thread_env in (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "NUMEXPR_NUM_THREADS",
):
    os.environ[_thread_env] = "2"

try:
    import numpy as np
except ImportError as exc:  # pragma: no cover - exercised by the CLI in a real env
    raise SystemExit(
        "NumPy is required. Install the existing optional registration environment "
        "later with: uv sync --extra lidar-registration"
    ) from exc


DEFAULT_THRESHOLDS = (0.005, 0.01, 0.015, 0.03, 0.05, 0.10, 0.15)
ROBUST_TAIL = 0.005
COMMON_PADDING_NORMALIZED = 0.03
VISUAL_SAMPLE_LIMIT = 12_000
UTILITY_VERSION = "2"


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object at {path}")
    return value


def read_ascii_ply_xyz(path: str | Path) -> tuple[np.ndarray, dict[str, Any]]:
    """Read scalar XYZ from the declared ASCII PLY without changing the file."""
    path = Path(path)
    vertex_count: int | None = None
    vertex_properties: list[str] = []
    comments: list[str] = []
    header_lines = 0
    current_element: str | None = None
    with path.open("rb") as stream:
        for raw in stream:
            header_lines += 1
            try:
                line = raw.decode("ascii").strip()
            except UnicodeDecodeError as exc:
                raise ValueError("PLY header must be ASCII") from exc
            if line.startswith("comment "):
                comments.append(line[8:])
            if line.startswith("format ") and line != "format ascii 1.0":
                raise ValueError(f"Expected ASCII PLY, found: {line}")
            if line.startswith("element "):
                fields = line.split()
                current_element = fields[1] if len(fields) == 3 else None
                if current_element == "vertex":
                    vertex_count = int(fields[2])
            elif current_element == "vertex" and line.startswith("property "):
                fields = line.split()
                if len(fields) != 3 or fields[1] == "list":
                    raise ValueError("Only scalar vertex properties are supported")
                vertex_properties.append(fields[2])
            if line == "end_header":
                break
        else:
            raise ValueError("PLY header has no end_header")

    if vertex_count is None or vertex_count <= 0:
        raise ValueError("PLY has no non-empty vertex element")
    if not {"x", "y", "z"}.issubset(vertex_properties):
        raise ValueError(f"PLY is missing scalar XYZ fields: {vertex_properties}")
    usecols = tuple(vertex_properties.index(axis) for axis in "xyz")
    try:
        points = np.loadtxt(
            path,
            dtype=np.float64,
            skiprows=header_lines,
            max_rows=vertex_count,
            usecols=usecols,
            ndmin=2,
        )
    except (OSError, ValueError) as exc:
        raise ValueError(f"Could not parse ASCII PLY vertices: {exc}") from exc
    if points.shape != (vertex_count, 3):
        raise ValueError(
            f"PLY declared {vertex_count} vertices but provided {points.shape[0]} XYZ rows"
        )
    finite = np.isfinite(points).all(axis=1)
    if not finite.all():
        raise ValueError(f"PLY contains {int((~finite).sum())} non-finite XYZ rows")
    return points, {
        "vertex_count": vertex_count,
        "vertex_properties": vertex_properties,
        "comments": comments,
        "header_lines": header_lines,
        "format": "ascii",
    }


def read_colmap_points3d(path: str | Path) -> np.ndarray:
    """Read every finite COLMAP points3D.bin XYZ record, ignoring tracks."""
    path = Path(path)
    points: list[tuple[float, float, float]] = []
    with path.open("rb") as stream:
        raw_count = stream.read(8)
        if len(raw_count) != 8:
            raise ValueError("COLMAP points3D.bin is truncated before its count")
        count = struct.unpack("<Q", raw_count)[0]
        for index in range(count):
            record = stream.read(51)
            if len(record) != 51:
                raise ValueError(f"COLMAP points3D.bin is truncated at record {index}")
            unpacked = struct.unpack("<Q3d3BdQ", record)
            xyz = tuple(float(value) for value in unpacked[1:4])
            if all(math.isfinite(value) for value in xyz):
                points.append(xyz)
            track_length = int(unpacked[-1])
            stream.seek(track_length * 8, 1)
        if stream.read(1):
            raise ValueError("COLMAP points3D.bin has trailing bytes")
    return np.asarray(points, dtype=np.float64).reshape((-1, 3))


def _quaternion_rotation(q: Iterable[float]) -> np.ndarray:
    w, x, y, z = (float(value) for value in q)
    return np.asarray(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ],
        dtype=np.float64,
    )


def read_colmap_camera_centers(path: str | Path) -> tuple[np.ndarray, list[str]]:
    """Read registered camera centers from COLMAP world-to-camera poses."""
    centers: list[np.ndarray] = []
    names: list[str] = []
    with Path(path).open("rb") as stream:
        raw_count = stream.read(8)
        if len(raw_count) != 8:
            raise ValueError("COLMAP images.bin is truncated before its count")
        count = struct.unpack("<Q", raw_count)[0]
        for index in range(count):
            record = stream.read(64)
            if len(record) != 64:
                raise ValueError(f"COLMAP images.bin is truncated at image {index}")
            values = struct.unpack("<I4d3dI", record)
            qvec, tvec = values[1:5], np.asarray(values[5:8], dtype=np.float64)
            name_bytes = bytearray()
            while True:
                char = stream.read(1)
                if not char:
                    raise ValueError(f"COLMAP images.bin has an unterminated name at image {index}")
                if char == b"\x00":
                    break
                name_bytes.extend(char)
            raw_points2d = stream.read(8)
            if len(raw_points2d) != 8:
                raise ValueError(f"COLMAP images.bin is truncated before observations at image {index}")
            points2d_count = struct.unpack("<Q", raw_points2d)[0]
            if points2d_count:
                stream.seek(points2d_count * 24, 1)
            rotation = _quaternion_rotation(qvec)
            center = -rotation.T @ tvec
            if not np.isfinite(center).all():
                raise ValueError(f"COLMAP image {index} has a non-finite camera center")
            centers.append(center)
            names.append(name_bytes.decode("utf-8", errors="replace"))
        if stream.read(1):
            raise ValueError("COLMAP images.bin has trailing bytes")
    return np.asarray(centers, dtype=np.float64).reshape((-1, 3)), names


def apply_transform(points: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """Apply a row-major homogeneous matrix using column-vector convention."""
    points = np.asarray(points, dtype=np.float64)
    matrix = np.asarray(matrix, dtype=np.float64)
    if points.ndim != 2 or points.shape[1] != 3:
        raise ValueError("Points must have shape (n, 3)")
    if matrix.shape != (4, 4):
        raise ValueError("Transform must have shape (4, 4)")
    return points @ matrix[:3, :3].T + matrix[:3, 3]


def validate_similarity_pair(
    sfm_to_lidar: np.ndarray,
    lidar_to_sfm: np.ndarray,
    declared_scale: float | None = None,
    declared_rotation: np.ndarray | None = None,
    tolerance: float = 1e-8,
) -> dict[str, Any]:
    """Validate homogeneous form, mutual inverse, orientation, and uniform scale."""
    forward = np.asarray(sfm_to_lidar, dtype=np.float64)
    inverse = np.asarray(lidar_to_sfm, dtype=np.float64)
    if forward.shape != (4, 4) or inverse.shape != (4, 4):
        raise ValueError("Both saved transforms must be 4×4 row-major matrices")
    if not np.isfinite(forward).all() or not np.isfinite(inverse).all():
        raise ValueError("A transform contains non-finite values")
    expected_last_row = np.asarray([0.0, 0.0, 0.0, 1.0])
    if not np.allclose(forward[3], expected_last_row, atol=tolerance, rtol=0):
        raise ValueError("SfM-to-LiDAR matrix has an invalid homogeneous last row")
    if not np.allclose(inverse[3], expected_last_row, atol=tolerance, rtol=0):
        raise ValueError("LiDAR-to-SfM matrix has an invalid homogeneous last row")
    identity = np.eye(4)
    if not np.allclose(forward @ inverse, identity, atol=tolerance, rtol=tolerance):
        raise ValueError("Saved SfM-to-LiDAR and LiDAR-to-SfM transforms are not mutual inverses")
    if not np.allclose(inverse @ forward, identity, atol=tolerance, rtol=tolerance):
        raise ValueError("Saved LiDAR-to-SfM and SfM-to-LiDAR transforms are not mutual inverses")
    linear = forward[:3, :3]
    determinant = float(np.linalg.det(linear))
    if determinant <= 0:
        raise ValueError("Saved transform contains a reflection or non-positive scale")
    scale = float(np.cbrt(determinant))
    gram = linear.T @ linear
    if not np.allclose(gram, np.eye(3) * scale * scale, atol=tolerance, rtol=1e-6):
        raise ValueError("Saved transform's linear component is not a uniform-scale rotation")
    if declared_scale is not None and not math.isclose(
        scale, float(declared_scale), rel_tol=1e-7, abs_tol=tolerance
    ):
        raise ValueError("Saved matrix scale does not match its declared scale")
    rotation_from_matrix = linear / scale
    auxiliary_rotation_matches = None
    auxiliary_rotation_error = None
    if declared_rotation is not None:
        declared_rotation = np.asarray(declared_rotation, dtype=np.float64)
        if declared_rotation.shape != (3, 3) or not np.isfinite(declared_rotation).all():
            raise ValueError("Redundant rotation metadata must be a finite 3×3 matrix")
        auxiliary_rotation_error = float(np.max(np.abs(rotation_from_matrix - declared_rotation)))
        auxiliary_rotation_matches = bool(
            np.allclose(rotation_from_matrix, declared_rotation, atol=tolerance, rtol=1e-6)
        )
    return {
        "valid": True,
        "matrix_convention": "row-major JSON, column-vector homogeneous application; p_dst = p_src @ A.T + t",
        "direction": "SfM source to DepthEye LiDAR target",
        "scale_lidar_units_per_sfm_unit": scale,
        "linear_determinant": determinant,
        "forward_inverse_max_abs_error": float(np.max(np.abs(forward @ inverse - identity))),
        "inverse_forward_max_abs_error": float(np.max(np.abs(inverse @ forward - identity))),
        "uniform_scale_rotation_error": float(
            np.max(np.abs(gram - np.eye(3) * scale * scale))
        ),
        "rotation_derived_from_similarity_matrix": rotation_from_matrix.tolist(),
        "auxiliary_rotation_metadata_matches_matrix": auxiliary_rotation_matches,
        "auxiliary_rotation_metadata_max_abs_error": auxiliary_rotation_error,
        "auxiliary_rotation_metadata_note": (
            "The saved similarity matrix and its inverse are authoritative for this diagnostic. "
            "A separate rotation field, if inconsistent, is reported but is not used to transform points."
        ),
    }


def _open3d():
    try:
        import open3d as o3d
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "Open3D is required for bounded CPU nearest-neighbor queries. "
            "Install later with: uv sync --extra lidar-registration"
        ) from exc
    if hasattr(o3d, "__version__") and str(o3d.__version__).startswith("0.21"):
        raise RuntimeError("This repository pins Open3D below 0.21; the installed version is incompatible")
    return o3d


def nearest_neighbor_distances(source: np.ndarray, target: np.ndarray) -> np.ndarray:
    """Return each source point's distance to the closest target point."""
    source = np.asarray(source, dtype=np.float64)
    target = np.asarray(target, dtype=np.float64)
    if source.ndim != 2 or target.ndim != 2 or source.shape[1:] != (3,) or target.shape[1:] != (3,):
        raise ValueError("Nearest-neighbor inputs must each have shape (n, 3)")
    if len(source) == 0 or len(target) == 0:
        raise ValueError("Nearest-neighbor inputs must not be empty")
    o3d = _open3d()
    source_cloud = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(source))
    target_cloud = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(target))
    return np.asarray(source_cloud.compute_point_cloud_distance(target_cloud), dtype=np.float64)


def nearest_spacing(points: np.ndarray) -> np.ndarray:
    """Return within-cloud nearest-neighbor spacing using Open3D's CPU KD-tree."""
    points = np.asarray(points, dtype=np.float64)
    if len(points) < 2:
        return np.asarray([], dtype=np.float64)
    o3d = _open3d()
    cloud = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(points))
    return np.asarray(cloud.compute_nearest_neighbor_distance(), dtype=np.float64)


def distance_statistics(values: np.ndarray) -> dict[str, Any]:
    values = np.asarray(values, dtype=np.float64)
    values = values[np.isfinite(values)]
    if len(values) == 0:
        return {"count": 0, "minimum": None, "median": None, "p75": None, "p90": None, "p95": None, "p99": None, "maximum": None}
    return {
        "count": int(len(values)),
        "minimum": float(np.min(values)),
        "median": float(np.median(values)),
        "p75": float(np.percentile(values, 75)),
        "p90": float(np.percentile(values, 90)),
        "p95": float(np.percentile(values, 95)),
        "p99": float(np.percentile(values, 99)),
        "maximum": float(np.max(values)),
    }


def overlap_table(
    forward_normalized: np.ndarray,
    reverse_normalized: np.ndarray,
    thresholds: Iterable[float] = DEFAULT_THRESHOLDS,
) -> list[dict[str, Any]]:
    forward = np.asarray(forward_normalized, dtype=np.float64)
    reverse = np.asarray(reverse_normalized, dtype=np.float64)
    rows = []
    for threshold in thresholds:
        rows.append(
            {
                "threshold_normalized": float(threshold),
                "sfm_to_lidar_fraction": float(np.mean(forward <= threshold)) if len(forward) else 0.0,
                "lidar_to_sfm_fraction": float(np.mean(reverse <= threshold)) if len(reverse) else 0.0,
                "sfm_to_lidar_count": int(np.count_nonzero(forward <= threshold)),
                "lidar_to_sfm_count": int(np.count_nonzero(reverse <= threshold)),
            }
        )
    return rows


def coordinate_normalized_distance(raw_distance: np.ndarray, bbox_diagonal: float) -> np.ndarray:
    if not math.isfinite(bbox_diagonal) or bbox_diagonal <= 0:
        raise ValueError("Bounding-box diagonal must be positive and finite")
    return np.asarray(raw_distance, dtype=np.float64) / bbox_diagonal


def _bounds(points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    return np.min(points, axis=0), np.max(points, axis=0)


def engine_validation_sample_indices(point_count: int, maximum: int = 10_000) -> np.ndarray:
    """Match the existing Engine overlap screen's deterministic stride sampling."""
    if point_count < 0 or maximum < 1:
        raise ValueError("Point count must be non-negative and maximum must be positive")
    stride = max(1, math.ceil(point_count / maximum))
    return np.arange(0, point_count, stride, dtype=np.int64)


def _robust_bounds(points: np.ndarray, tail: float = ROBUST_TAIL) -> tuple[np.ndarray, np.ndarray]:
    return np.quantile(points, tail, axis=0), np.quantile(points, 1.0 - tail, axis=0)


def _bbox_summary(points: np.ndarray) -> dict[str, Any]:
    if len(points) == 0:
        return {"point_count": 0, "minimum": None, "maximum": None, "diagonal": None}
    minimum, maximum = _bounds(points)
    return {
        "point_count": int(len(points)),
        "minimum": minimum.tolist(),
        "maximum": maximum.tolist(),
        "diagonal": float(np.linalg.norm(maximum - minimum)),
    }


def _voxel_summary(points: np.ndarray, diagonal: float, normalized_sizes=(0.005, 0.01, 0.03)) -> list[dict[str, Any]]:
    rows = []
    origin = np.min(points, axis=0)
    for size in normalized_sizes:
        cell = max(float(size) * diagonal, 1e-12)
        keys, counts = np.unique(np.floor((points - origin) / cell).astype(np.int64), axis=0, return_counts=True)
        rows.append(
            {
                "voxel_size_normalized": float(size),
                "occupied_voxels": int(len(keys)),
                "points_per_occupied_voxel": distance_statistics(counts),
            }
        )
    return rows


def point_cloud_summary(points: np.ndarray, name: str) -> dict[str, Any]:
    points = np.asarray(points, dtype=np.float64)
    minimum, maximum = _bounds(points)
    diagonal = float(np.linalg.norm(maximum - minimum))
    center = np.mean(points, axis=0)
    covariance = np.cov(points.T)
    eigenvalues, eigenvectors = np.linalg.eigh(covariance)
    order = np.argsort(eigenvalues)[::-1]
    eigenvalues = eigenvalues[order]
    eigenvectors = eigenvectors[:, order]
    eigenvalues = np.maximum(eigenvalues, 0)
    ratios = eigenvalues / max(float(eigenvalues.sum()), 1e-30)
    spacing = nearest_spacing(points)
    spacing_median = float(np.median(spacing)) if len(spacing) else 0.0
    spacing_mad = float(np.median(np.abs(spacing - spacing_median))) if len(spacing) else 0.0
    outlier_limit = spacing_median + 6 * 1.4826 * spacing_mad
    robust_minimum, robust_maximum = _robust_bounds(points)
    robust_diagonal = float(np.linalg.norm(robust_maximum - robust_minimum))
    outside_robust = ~np.logical_and(points >= robust_minimum, points <= robust_maximum).all(axis=1)
    downsample = _open3d().geometry.PointCloud(_open3d().utility.Vector3dVector(points)).voxel_down_sample(
        max(0.015 * diagonal, 1e-12)
    )
    return {
        "name": name,
        "finite_point_count": int(len(points)),
        "raw_bounds": _bbox_summary(points),
        "robust_axis_bounds_0_5_to_99_5_percent": {
            "minimum": robust_minimum.tolist(),
            "maximum": robust_maximum.tolist(),
            "diagonal": robust_diagonal,
            "outside_point_count": int(np.count_nonzero(outside_robust)),
            "outside_fraction": float(np.mean(outside_robust)),
        },
        "centroid": center.tolist(),
        "principal_axes": {
            "eigenvalues_descending": eigenvalues.tolist(),
            "variance_fractions_descending": ratios.tolist(),
            "axis_vectors_columns": eigenvectors.tolist(),
        },
        "nearest_neighbor_spacing_raw_units": distance_statistics(spacing),
        "nearest_neighbor_spacing_method": "Exact within-cloud nearest neighbor distances from Open3D CPU KD-tree over all finite vertices.",
        "spatial_outlier_screen": {
            "candidate_count": int(np.count_nonzero(spacing > outlier_limit)),
            "candidate_threshold_raw_units": float(outlier_limit),
            "method": "nearest-neighbor spacing > median + 6 × 1.4826 × MAD; candidates are retained and not classified as errors",
        },
        "voxel_density": _voxel_summary(points, diagonal),
        "downsampled_point_count_at_0_015_bbox_diagonal_voxel": int(len(downsample.points)),
    }


def _inside_bounds(points: np.ndarray, minimum: np.ndarray, maximum: np.ndarray, padding: float = 0.0) -> np.ndarray:
    return np.logical_and(points >= minimum - padding, points <= maximum + padding).all(axis=1)


def _largest_voxel_component(cells: np.ndarray) -> dict[str, Any]:
    remaining = {tuple(int(axis) for axis in cell) for cell in cells}
    total = len(remaining)
    if not total:
        return {"component_count": 0, "largest_component_voxels": 0, "largest_component_fraction": 0.0}
    offsets = [(dx, dy, dz) for dx in (-1, 0, 1) for dy in (-1, 0, 1) for dz in (-1, 0, 1) if (dx, dy, dz) != (0, 0, 0)]
    component_sizes = []
    while remaining:
        seed = remaining.pop()
        queue = deque([seed])
        size = 0
        while queue:
            x, y, z = queue.popleft()
            size += 1
            for dx, dy, dz in offsets:
                candidate = (x + dx, y + dy, z + dz)
                if candidate in remaining:
                    remaining.remove(candidate)
                    queue.append(candidate)
        component_sizes.append(size)
    largest = max(component_sizes)
    return {
        "component_count": len(component_sizes),
        "largest_component_voxels": largest,
        "largest_component_fraction": largest / total,
    }


def _occupancy_overlap(
    sfm_in_lidar: np.ndarray,
    lidar: np.ndarray,
    lidar_minimum: np.ndarray,
    lidar_diagonal: float,
    normalized_sizes=(0.01, 0.02),
) -> list[dict[str, Any]]:
    rows = []
    for normalized_size in normalized_sizes:
        cell = normalized_size * lidar_diagonal
        sfm_cells = np.unique(np.floor((sfm_in_lidar - lidar_minimum) / cell).astype(np.int64), axis=0)
        lidar_cells = np.unique(np.floor((lidar - lidar_minimum) / cell).astype(np.int64), axis=0)
        sfm_set = {tuple(row) for row in sfm_cells.tolist()}
        lidar_set = {tuple(row) for row in lidar_cells.tolist()}
        common = sfm_set & lidar_set
        union = sfm_set | lidar_set
        row = {
            "voxel_size_normalized_to_lidar_diagonal": float(normalized_size),
            "sfm_occupied_voxels": len(sfm_set),
            "lidar_occupied_voxels": len(lidar_set),
            "same_voxel_intersection": len(common),
            "same_voxel_jaccard": len(common) / len(union) if union else 0.0,
            "same_voxel_fraction_of_sfm": len(common) / len(sfm_set) if sfm_set else 0.0,
            "same_voxel_fraction_of_lidar": len(common) / len(lidar_set) if lidar_set else 0.0,
        }
        if normalized_size == max(normalized_sizes):
            row["shared_voxel_components_26_neighbor"] = _largest_voxel_component(
                np.asarray(list(common), dtype=np.int64).reshape((-1, 3))
            )
        rows.append(row)
    return rows


def _local_octants(
    common_sfm: np.ndarray,
    common_forward: np.ndarray,
    center: np.ndarray,
    threshold: float,
) -> list[dict[str, Any]]:
    if len(common_sfm) == 0:
        return []
    flags = common_sfm >= center
    rows = []
    for mask in range(8):
        selection = np.ones(len(common_sfm), dtype=bool)
        axes = []
        for axis in range(3):
            high = bool(mask & (1 << axis))
            selection &= flags[:, axis] if high else ~flags[:, axis]
            axes.append(("x", "y", "z")[axis] + ("+" if high else "-"))
        if np.count_nonzero(selection) < 30:
            continue
        values = common_forward[selection]
        rows.append(
            {
                "octant": "".join(axes),
                "point_count": int(len(values)),
                "residual_normalized": distance_statistics(values),
                "overlap_fraction_at_0_03": float(np.mean(values <= threshold)),
            }
        )
    return rows


def _downsampled_overlap_sensitivity(
    sfm_in_lidar: np.ndarray,
    lidar: np.ndarray,
    lidar_diagonal: float,
    thresholds=(0.03,),
) -> list[dict[str, Any]]:
    o3d = _open3d()
    rows = []
    sfm_cloud = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(sfm_in_lidar))
    lidar_cloud = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(lidar))
    for normalized_size in (0.005, 0.01, 0.02):
        cell = normalized_size * lidar_diagonal
        sfm_down = np.asarray(sfm_cloud.voxel_down_sample(cell).points, dtype=np.float64)
        lidar_down = np.asarray(lidar_cloud.voxel_down_sample(cell).points, dtype=np.float64)
        if not len(sfm_down) or not len(lidar_down):
            continue
        forward = nearest_neighbor_distances(sfm_down, lidar_down) / lidar_diagonal
        reverse = nearest_neighbor_distances(lidar_down, sfm_down) / lidar_diagonal
        rows.append(
            {
                "voxel_size_normalized_to_lidar_diagonal": normalized_size,
                "sfm_downsampled_count": len(sfm_down),
                "lidar_downsampled_count": len(lidar_down),
                "sfm_to_lidar_residual_normalized": distance_statistics(forward),
                "lidar_to_sfm_residual_normalized_common_lidar_frame": distance_statistics(reverse),
                "overlap_fractions": {
                    str(threshold): {
                        "sfm_to_lidar": float(np.mean(forward <= threshold)),
                        "lidar_to_sfm": float(np.mean(reverse <= threshold)),
                    }
                    for threshold in thresholds
                },
            }
        )
    return rows


def _trimmed_outlier_sensitivity(
    sfm_raw: np.ndarray,
    lidar: np.ndarray,
    sfm_in_lidar: np.ndarray,
    scale: float,
    sfm_diagonal: float,
    lidar_diagonal: float,
    threshold: float,
) -> dict[str, Any]:
    sfm_min, sfm_max = _robust_bounds(sfm_raw)
    lidar_min, lidar_max = _robust_bounds(lidar)
    sfm_mask = _inside_bounds(sfm_raw, sfm_min, sfm_max)
    lidar_mask = _inside_bounds(lidar, lidar_min, lidar_max)
    sfm_core = sfm_in_lidar[sfm_mask]
    lidar_core = lidar[lidar_mask]
    forward = nearest_neighbor_distances(sfm_core, lidar_core) / lidar_diagonal
    reverse = nearest_neighbor_distances(lidar_core, sfm_core) / (scale * sfm_diagonal)
    return {
        "selection": "Independent 0.5th–99.5th percentile bounds on each input cloud's raw axes; no residual-based point removal.",
        "sfm_retained_count": int(sfm_mask.sum()),
        "lidar_retained_count": int(lidar_mask.sum()),
        "full_sfm_count": int(len(sfm_raw)),
        "full_lidar_count": int(len(lidar)),
        "sfm_to_lidar_residual_normalized": distance_statistics(forward),
        "lidar_to_sfm_residual_normalized": distance_statistics(reverse),
        "overlap_fractions_at_0_03": {
            "sfm_to_lidar": float(np.mean(forward <= threshold)),
            "lidar_to_sfm": float(np.mean(reverse <= threshold)),
        },
        "limitation": "This sensitivity screen retains all source files and does not establish that trimmed points are outliers.",
    }


def validate_inputs(
    sfm_summary_path: str | Path,
    sfm_manifest_path: str | Path,
    lidar_inspection_path: str | Path,
    alignment_report_path: str | Path,
) -> tuple[dict[str, Any], dict[str, Path], dict[str, Any], dict[str, Any], dict[str, Any]]:
    summary_path = Path(sfm_summary_path).resolve()
    manifest_path = Path(sfm_manifest_path).resolve()
    inspection_path = Path(lidar_inspection_path).resolve()
    alignment_path = Path(alignment_report_path).resolve()
    summary = read_json(summary_path)
    sfm_manifest = read_json(manifest_path)
    inspection = read_json(inspection_path)
    alignment = read_json(alignment_path)

    if summary.get("execution_status") != "COMPLETED" or summary.get("quality_gate") != "SFM_VALID":
        raise ValueError("SfM summary does not identify a completed SFM_VALID artifact")
    if summary.get("project_id") != sfm_manifest.get("project_id"):
        raise ValueError("SfM summary and export manifest project IDs differ")
    if sfm_manifest.get("dataset_format") != "COLMAP_BINARY":
        raise ValueError("SfM export manifest does not identify a COLMAP binary dataset")
    if int(summary.get("sparse_point_count", -1)) != int(sfm_manifest.get("sparse_point_count", -2)):
        raise ValueError("SfM summary point count differs from the exported manifest")
    if not sfm_manifest.get("compatibility_validation", {}).get("structural_colmap_dataset") == "PASS":
        raise ValueError("SfM export manifest does not record structural COLMAP validation")

    sfm_dir = Path(summary["reconstruction_directory"]).resolve()
    if not sfm_dir.is_dir():
        raise ValueError(f"Validated SfM reconstruction directory does not exist: {sfm_dir}")
    sparse_manifest = sfm_manifest.get("sparse_files", {})
    sfm_files: dict[str, Path] = {}
    sfm_file_hashes = {}
    for name in ("cameras.bin", "images.bin", "points3D.bin"):
        path = sfm_dir / name
        manifest_entry = sparse_manifest.get(name)
        if not path.is_file() or not isinstance(manifest_entry, dict):
            raise ValueError(f"Missing validated COLMAP file or manifest entry: {name}")
        if sha256_file(path) != manifest_entry.get("sha256"):
            raise ValueError(f"Validated SfM file hash does not match export manifest: {path}")
        sfm_files[name] = path
        sfm_file_hashes[name] = sha256_file(path)

    selected_lidar = inspection.get("selected") or {}
    if inspection.get("inspection_status") != "VALID" or selected_lidar.get("status") != "VALID":
        raise ValueError("LiDAR inspection manifest does not identify a valid selected scan")
    lidar_path = Path(selected_lidar.get("path", "")).resolve()
    if not lidar_path.is_file():
        raise ValueError(f"Selected raw LiDAR source is missing: {lidar_path}")
    lidar_hash = sha256_file(lidar_path)
    if lidar_hash != selected_lidar.get("sha256"):
        raise ValueError("Selected raw LiDAR file hash does not match the inspection manifest")

    if alignment.get("accepted") is not False or alignment.get("status") != "REJECTED":
        raise ValueError("The supplied alignment report is not the saved rejected candidate")
    if alignment.get("input_coordinate_systems", {}).get("sfm") is None:
        raise ValueError("Alignment report lacks its SfM coordinate-system declaration")
    if alignment.get("input_coordinate_systems", {}).get("lidar") is None:
        raise ValueError("Alignment report lacks its LiDAR coordinate-system declaration")

    files = {
        "sfm_summary": summary_path,
        "sfm_manifest": manifest_path,
        "lidar_inspection": inspection_path,
        "alignment_report": alignment_path,
        "lidar_ply": lidar_path,
        **{f"sfm_{name}": path for name, path in sfm_files.items()},
    }
    provenance = {
        key: {"path": str(path), "sha256": sha256_file(path)} for key, path in files.items()
    }
    provenance["sfm_artifact_identity"] = {
        "summary_execution_status": summary.get("execution_status"),
        "summary_quality_gate": summary.get("quality_gate"),
        "summary_attempt_directory": summary.get("attempt_directory"),
        "reconstruction_directory": str(sfm_dir),
        "export_manifest_sha256_matches_all_three_sparse_files": True,
        "all_sparse_file_sha256": sfm_file_hashes,
        "registered_images": summary.get("registered_image_count"),
        "sparse_points": summary.get("sparse_point_count"),
    }
    provenance["lidar_artifact_identity"] = {
        "selected_path": str(lidar_path),
        "sha256_matches_inspection_manifest": True,
        "sha256": lidar_hash,
        "point_count": selected_lidar.get("point_count"),
        "coordinate_units_status": selected_lidar.get("coordinate_units", {}).get("status"),
        "format": selected_lidar.get("format"),
    }
    return provenance, sfm_files, summary, inspection, alignment


def analyze_geometry(
    sfm: np.ndarray,
    lidar: np.ndarray,
    camera_centers: np.ndarray,
    alignment: dict[str, Any],
    sfm_point_summary: dict[str, Any],
    lidar_point_summary: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    forward = np.asarray(alignment["similarity_transform_sfm_to_lidar_row_major"], dtype=np.float64)
    inverse = np.asarray(alignment["similarity_transform_lidar_to_sfm_row_major"], dtype=np.float64)
    transform_check = validate_similarity_pair(
        forward,
        inverse,
        alignment.get("scale_lidar_units_per_sfm_unit"),
        alignment.get("rotation_sfm_to_lidar_row_major"),
    )
    sfm_in_lidar = apply_transform(sfm, forward)
    cameras_in_lidar = apply_transform(camera_centers, forward)

    sfm_min, sfm_max = _bounds(sfm)
    lidar_min, lidar_max = _bounds(lidar)
    sfm_diagonal = float(np.linalg.norm(sfm_max - sfm_min))
    lidar_diagonal = float(np.linalg.norm(lidar_max - lidar_min))
    scale = transform_check["scale_lidar_units_per_sfm_unit"]

    forward_raw = nearest_neighbor_distances(sfm_in_lidar, lidar)
    reverse_raw = nearest_neighbor_distances(lidar, sfm_in_lidar)
    forward_normalized = coordinate_normalized_distance(forward_raw, lidar_diagonal)
    reverse_normalized = coordinate_normalized_distance(reverse_raw, scale * sfm_diagonal)
    common_lidar_frame_reverse = coordinate_normalized_distance(reverse_raw, lidar_diagonal)
    thresholds = DEFAULT_THRESHOLDS
    overlap = overlap_table(forward_normalized, reverse_normalized, thresholds)
    forward_screen_indices = engine_validation_sample_indices(len(forward_normalized))
    reverse_screen_indices = engine_validation_sample_indices(len(reverse_normalized))
    forward_screen = forward_normalized[forward_screen_indices]
    reverse_screen = reverse_normalized[reverse_screen_indices]
    saved_evidence = alignment.get("registration_evidence", {})
    screen_reproduction = {
        "method": "Same deterministic max-10,000 stride sample used by src/exhibition_archive/lidar.py::_nearest_overlap.",
        "sfm_to_lidar_sample_count": int(len(forward_screen)),
        "sfm_to_lidar_stride": int(forward_screen_indices[1] - forward_screen_indices[0]) if len(forward_screen_indices) > 1 else 1,
        "lidar_to_sfm_sample_count": int(len(reverse_screen)),
        "lidar_to_sfm_stride": int(reverse_screen_indices[1] - reverse_screen_indices[0]) if len(reverse_screen_indices) > 1 else 1,
        "sfm_to_lidar_overlap_fraction_at_0_03": float(np.mean(forward_screen <= 0.03)),
        "lidar_to_sfm_overlap_fraction_at_0_03": float(np.mean(reverse_screen <= 0.03)),
        "sfm_to_lidar_p95": distance_statistics(forward_screen)["p95"],
        "lidar_to_sfm_p95": distance_statistics(reverse_screen)["p95"],
        "bidirectional_p95": max(distance_statistics(forward_screen)["p95"], distance_statistics(reverse_screen)["p95"]),
        "saved_report_values": {
            "sfm_to_lidar_overlap_fraction_at_0_03": saved_evidence.get("forward_overlap_fraction"),
            "lidar_to_sfm_overlap_fraction_at_0_03": saved_evidence.get("reverse_overlap_fraction"),
            "bidirectional_p95": saved_evidence.get("bidirectional_p95_normalized_residual"),
        },
    }
    screen_reproduction["deltas_vs_saved_report"] = {
        key: screen_reproduction[key] - float(screen_reproduction["saved_report_values"][key])
        for key in screen_reproduction["saved_report_values"]
        if screen_reproduction["saved_report_values"][key] is not None
    }

    sfm_robust_min, sfm_robust_max = _robust_bounds(sfm_in_lidar)
    lidar_robust_min, lidar_robust_max = _robust_bounds(lidar)
    intersection_min = np.maximum(sfm_robust_min, lidar_robust_min)
    intersection_max = np.minimum(sfm_robust_max, lidar_robust_max)
    has_intersection = bool(np.all(intersection_max > intersection_min))
    padding = COMMON_PADDING_NORMALIZED * lidar_diagonal
    sfm_in_lidar_box = _inside_bounds(sfm_in_lidar, lidar_robust_min, lidar_robust_max, padding)
    lidar_in_sfm_box = _inside_bounds(lidar, sfm_robust_min, sfm_robust_max, padding)

    if has_intersection:
        sfm_in_intersection = _inside_bounds(sfm_in_lidar, intersection_min, intersection_max, padding)
        lidar_in_intersection = _inside_bounds(lidar, intersection_min, intersection_max, padding)
    else:
        sfm_in_intersection = np.zeros(len(sfm), dtype=bool)
        lidar_in_intersection = np.zeros(len(lidar), dtype=bool)

    sfm_spatial = sfm_in_lidar[sfm_in_intersection]
    lidar_spatial = lidar[lidar_in_intersection]
    sfm_spatial_forward = forward_normalized[sfm_in_intersection]
    lidar_spatial_reverse = reverse_normalized[lidar_in_intersection]
    sfm_spatial_common = sfm_spatial_forward <= 0.03
    lidar_spatial_common = lidar_spatial_reverse <= 0.03

    sfm_unmatched = forward_normalized > 0.03
    lidar_unmatched = reverse_normalized > 0.03
    sfm_outside_box = ~sfm_in_lidar_box
    lidar_outside_box = ~lidar_in_sfm_box

    common_bounds = {
        "selection": (
            "Intersection of independent 0.5th–99.5th percentile axis-aligned bounds "
            "for the candidate-transformed SfM cloud and LiDAR cloud, padded by 3% "
            "of the raw LiDAR bounding-box diagonal. Bounds are selected before residuals are inspected."
        ),
        "has_intersection": has_intersection,
        "minimum_lidar_frame_units": intersection_min.tolist() if has_intersection else None,
        "maximum_lidar_frame_units": intersection_max.tolist() if has_intersection else None,
        "unpadded_diagonal_lidar_frame_units": float(np.linalg.norm(intersection_max - intersection_min)) if has_intersection else None,
        "padding_lidar_frame_units": padding,
        "sfm_spatial_candidate_count": int(len(sfm_spatial)),
        "sfm_spatial_candidate_fraction_of_full": float(np.mean(sfm_in_intersection)) if len(sfm) else 0.0,
        "sfm_spatial_candidate_extent": _bbox_summary(sfm_spatial),
        "lidar_spatial_candidate_count": int(len(lidar_spatial)),
        "lidar_spatial_candidate_fraction_of_full": float(np.mean(lidar_in_intersection)) if len(lidar) else 0.0,
        "lidar_spatial_candidate_extent": _bbox_summary(lidar_spatial),
        "sfm_residuals_in_spatial_candidate": distance_statistics(sfm_spatial_forward),
        "lidar_residuals_in_spatial_candidate": distance_statistics(lidar_spatial_reverse),
        "sfm_overlap_fraction_at_0_03_in_spatial_candidate": float(np.mean(sfm_spatial_common)) if len(sfm_spatial_common) else 0.0,
        "lidar_overlap_fraction_at_0_03_in_spatial_candidate": float(np.mean(lidar_spatial_common)) if len(lidar_spatial_common) else 0.0,
        "sfm_common_candidate_count_within_0_03": int(np.count_nonzero(sfm_spatial_common)),
        "lidar_common_candidate_count_within_0_03": int(np.count_nonzero(lidar_spatial_common)),
        "octant_consistency": _local_octants(
            sfm_spatial,
            sfm_spatial_forward,
            (intersection_min + intersection_max) / 2,
            0.03,
        ),
        "limitations": [
            "Axis-aligned bounds are a spatial support screen, not a room surface or survey boundary.",
            "Residual-qualified subsets inherit the fixed rejected candidate transform and are not an independent registration.",
            "Unmatched points can arise from density, noise, occlusion, scene change, or wrong alignment as well as incomplete scan coverage.",
        ],
    }

    occupancy = _occupancy_overlap(sfm_in_lidar, lidar, lidar_min, lidar_diagonal)
    downsample_sensitivity = _downsampled_overlap_sensitivity(sfm_in_lidar, lidar, lidar_diagonal)
    trimmed = _trimmed_outlier_sensitivity(
        sfm,
        lidar,
        sfm_in_lidar,
        scale,
        sfm_diagonal,
        lidar_diagonal,
        0.03,
    )

    aligned_bbox = _bbox_summary(sfm_in_lidar)
    robust_sfm_diagonal_raw = float(sfm_point_summary["robust_axis_bounds_0_5_to_99_5_percent"]["diagonal"])
    robust_lidar_diagonal = float(lidar_point_summary["robust_axis_bounds_0_5_to_99_5_percent"]["diagonal"])
    aligned_sfm_robust_diagonal = float(np.linalg.norm(sfm_robust_max - sfm_robust_min))
    raw_diagonal_scale_ratio = lidar_diagonal / sfm_diagonal
    robust_diagonal_scale_ratio = robust_lidar_diagonal / robust_sfm_diagonal_raw

    metrics = {
        "classification": None,
        "transform": transform_check,
        "coordinate_frames": {
            "sfm": "COLMAP arbitrary non-metric reconstruction units",
            "lidar": "DepthEye PLY coordinates; units unknown and not treated as metres",
            "aligned_candidate": "SfM points and camera centers transformed into the original LiDAR coordinate frame",
        },
        "geometry": {
            "sfm": sfm_point_summary,
            "lidar": lidar_point_summary,
            "candidate_aligned_sfm_bounds": aligned_bbox,
            "camera_center_count": int(len(camera_centers)),
            "camera_center_bounds_candidate_frame": _bbox_summary(cameras_in_lidar),
            "density_comparison_in_candidate_lidar_frame": {
                "sfm_nearest_neighbor_median_lidar_units": float(
                    sfm_point_summary["nearest_neighbor_spacing_raw_units"]["median"] * scale
                ),
                "lidar_nearest_neighbor_median_lidar_units": lidar_point_summary["nearest_neighbor_spacing_raw_units"]["median"],
                "mapped_sfm_to_lidar_median_spacing_ratio": float(
                    sfm_point_summary["nearest_neighbor_spacing_raw_units"]["median"]
                    * scale
                    / max(lidar_point_summary["nearest_neighbor_spacing_raw_units"]["median"], 1e-30)
                ),
                "interpretation": "SfM median nearest-neighbor spacing mapped through the saved scale and raw LiDAR spacing are in comparable LiDAR-frame coordinates; a ratio below 1 means denser SfM point sampling by this statistic.",
            },
        },
        "normalization_and_scale": {
            "sfm_raw_bbox_diagonal": sfm_diagonal,
            "lidar_raw_bbox_diagonal": lidar_diagonal,
            "sfm_robust_bbox_diagonal_0_5_to_99_5": robust_sfm_diagonal_raw,
            "lidar_robust_bbox_diagonal_0_5_to_99_5": robust_lidar_diagonal,
            "candidate_aligned_sfm_robust_bbox_diagonal_lidar_units": aligned_sfm_robust_diagonal,
            "candidate_aligned_sfm_to_lidar_robust_bbox_diagonal_ratio": aligned_sfm_robust_diagonal / robust_lidar_diagonal,
            "candidate_similarity_scale_lidar_units_per_sfm_unit": scale,
            "lidar_to_sfm_raw_bbox_diagonal_ratio": raw_diagonal_scale_ratio,
            "lidar_to_sfm_robust_bbox_diagonal_ratio": robust_diagonal_scale_ratio,
            "candidate_scale_vs_raw_diagonal_ratio": scale / raw_diagonal_scale_ratio,
            "candidate_scale_vs_robust_diagonal_ratio": scale / robust_diagonal_scale_ratio,
            "normalization_explanation": (
                "The forward engine metric is distance after SfM→LiDAR divided by the raw LiDAR bbox diagonal. "
                "The reverse engine metric applies the inverse and divides by the raw SfM bbox diagonal; "
                "equivalently, raw LiDAR-frame distance is divided by candidate scale × SfM diagonal. "
                "A separate common-LiDAR-frame reverse metric is also reported for directionally comparable units."
            ),
        },
        "candidate_residuals": {
            "sfm_to_lidar_raw_lidar_frame_units": distance_statistics(forward_raw),
            "lidar_to_sfm_raw_lidar_frame_units": distance_statistics(reverse_raw),
            "sfm_to_lidar_engine_normalized": distance_statistics(forward_normalized),
            "lidar_to_sfm_engine_normalized": distance_statistics(reverse_normalized),
            "lidar_to_sfm_common_lidar_diagonal_normalized": distance_statistics(common_lidar_frame_reverse),
            "engine_normalized_overlap_by_threshold": overlap,
            "bidirectional_engine_normalized_p95": max(
                distance_statistics(forward_normalized)["p95"],
                distance_statistics(reverse_normalized)["p95"],
            ),
            "saved_report_forward_overlap_fraction": alignment.get("registration_evidence", {}).get("forward_overlap_fraction"),
            "saved_report_reverse_overlap_fraction": alignment.get("registration_evidence", {}).get("reverse_overlap_fraction"),
            "saved_report_bidirectional_p95": alignment.get("registration_evidence", {}).get("bidirectional_p95_normalized_residual"),
            "engine_validation_sample_reproduction": screen_reproduction,
        },
        "spatial_coverage": {
            "robust_bounds_and_padding": common_bounds,
            "sfm_points_inside_lidar_robust_bounds_plus_padding": {
                "count": int(np.count_nonzero(sfm_in_lidar_box)),
                "fraction": float(np.mean(sfm_in_lidar_box)),
            },
            "lidar_points_inside_transformed_sfm_robust_bounds_plus_padding": {
                "count": int(np.count_nonzero(lidar_in_sfm_box)),
                "fraction": float(np.mean(lidar_in_sfm_box)),
            },
            "unmatched_distribution_at_0_03": {
                "sfm_unmatched_count": int(np.count_nonzero(sfm_unmatched)),
                "sfm_unmatched_fraction": float(np.mean(sfm_unmatched)),
                "sfm_unmatched_outside_lidar_envelope_count": int(np.count_nonzero(sfm_unmatched & sfm_outside_box)),
                "sfm_unmatched_outside_lidar_envelope_fraction_of_unmatched": float(np.mean(sfm_outside_box[sfm_unmatched])) if np.any(sfm_unmatched) else None,
                "sfm_inside_lidar_envelope_but_unmatched_count": int(np.count_nonzero(sfm_unmatched & ~sfm_outside_box)),
                "lidar_unmatched_count": int(np.count_nonzero(lidar_unmatched)),
                "lidar_unmatched_fraction": float(np.mean(lidar_unmatched)),
                "lidar_unmatched_outside_sfm_envelope_count": int(np.count_nonzero(lidar_unmatched & lidar_outside_box)),
                "lidar_unmatched_outside_sfm_envelope_fraction_of_unmatched": float(np.mean(lidar_outside_box[lidar_unmatched])) if np.any(lidar_unmatched) else None,
                "lidar_inside_sfm_envelope_but_unmatched_count": int(np.count_nonzero(lidar_unmatched & ~lidar_outside_box)),
            },
            "same_voxel_occupancy_overlap": occupancy,
            "multi_resolution_overlap_sensitivity": downsample_sensitivity,
            "independent_robust_bound_outlier_sensitivity": trimmed,
        },
        "saved_local_refinement": alignment.get("registration_evidence", {}).get("local_similarity_refinement", {}),
    }
    arrays = {
        "sfm": sfm,
        "lidar": lidar,
        "sfm_in_lidar": sfm_in_lidar,
        "camera_centers": camera_centers,
        "cameras_in_lidar": cameras_in_lidar,
        "forward_raw": forward_raw,
        "reverse_raw": reverse_raw,
        "forward_normalized": forward_normalized,
        "reverse_normalized": reverse_normalized,
        "sfm_in_spatial_candidate": sfm_in_intersection,
        "lidar_in_spatial_candidate": lidar_in_intersection,
    }
    return metrics, arrays


def classify_diagnostic(metrics: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    coverage = metrics["spatial_coverage"]
    common = coverage["robust_bounds_and_padding"]
    unmatched = coverage["unmatched_distribution_at_0_03"]
    local_f = common["sfm_residuals_in_spatial_candidate"]
    local_r = common["lidar_residuals_in_spatial_candidate"]
    octants = common["octant_consistency"]
    coherent = [row for row in octants if row["overlap_fraction_at_0_03"] >= 0.5]
    outside_unmatched = unmatched["sfm_unmatched_outside_lidar_envelope_fraction_of_unmatched"]
    enough_support = common["sfm_spatial_candidate_count"] >= 100 and common["lidar_spatial_candidate_count"] >= 100

    if (
        enough_support
        and local_f["p95"] is not None
        and local_r["p95"] is not None
        and local_f["p95"] <= 0.03
        and local_r["p95"] <= 0.03
        and len(coherent) >= 3
        and outside_unmatched is not None
        and outside_unmatched >= 0.5
    ):
        classification = "PROMISING_PARTIAL_ALIGNMENT"
    elif (
        enough_support
        and local_f["p95"] is not None
        and local_r["p95"] is not None
        and max(local_f["p95"], local_r["p95"]) > 0.08
        and len(coherent) < 2
    ):
        classification = "LIKELY_WRONG_TRANSFORM"
    elif (
        common["sfm_spatial_candidate_fraction_of_full"] < 0.05
        and common["lidar_spatial_candidate_fraction_of_full"] < 0.05
    ):
        classification = "LIKELY_INCOMPATIBLE_COVERAGE"
    else:
        classification = "INSUFFICIENT_EVIDENCE"

    evidence = {
        "classification_is_diagnostic_only": True,
        "classification_rules": {
            "promising_partial_alignment": "Both spatially selected direction P95 values ≤0.03, at least three octants with ≥0.5 in-threshold support, at least 100 points from each cloud, and at least half of unmatched SfM points outside the robust LiDAR envelope.",
            "likely_wrong_transform": "Spatial support exists but both directions have high (>0.08) local P95 and fewer than two coherent octants.",
            "likely_incompatible_coverage": "Both spatial candidate fractions are below 5%.",
            "otherwise": "INSUFFICIENT_EVIDENCE; no positive classification is inferred from global feature fit alone.",
        },
        "local_spatial_candidate_p95": {"sfm_to_lidar": local_f["p95"], "lidar_to_sfm": local_r["p95"]},
        "coherent_octant_count": len(coherent),
        "sfm_unmatched_outside_envelope_fraction": outside_unmatched,
    }
    return classification, evidence


def _fmt(value: Any, digits: int = 6) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}g}"
    return str(value)


def render_report(
    metrics: dict[str, Any],
    provenance: dict[str, Any],
    summary: dict[str, Any],
    inspection: dict[str, Any],
    alignment: dict[str, Any],
    output_dir: Path,
    command: str,
) -> str:
    classification, classification_evidence = classify_diagnostic(metrics)
    metrics["classification"] = classification
    metrics["classification_evidence"] = classification_evidence
    evidence = alignment.get("registration_evidence", {})
    common = metrics["spatial_coverage"]["robust_bounds_and_padding"]
    unmatched = metrics["spatial_coverage"]["unmatched_distribution_at_0_03"]
    norm = metrics["normalization_and_scale"]
    cand = metrics["candidate_residuals"]
    sfm = metrics["geometry"]["sfm"]
    lidar = metrics["geometry"]["lidar"]

    lines = [
        "# LiDAR–SfM partial-coverage diagnostic",
        "",
        f"**Diagnostic classification:** `{classification}` (analysis label only; the Engine result remains `REJECTED`).",
        "",
        "## Scope and safety",
        "",
        "This read-only analysis applies the saved rejected transform exactly as recorded. It did not run FPFH, RANSAC, registration refinement, SfM, or Gaussian training. It did not read or open the Engine SQLite database, write into `.engine`, or modify source geometry, navigation, or alignment reports. All generated files are in this independent diagnostics directory.",
        "",
        "Distance thresholds are normalized according to the existing Engine implementation. Raw coordinate units are not declared as metres.",
        "",
        "## Data provenance",
        "",
        f"- Project: `{summary.get('project_id')}`; execution: `{summary.get('attempt_directory')}`; status `{summary.get('execution_status')}` / `{summary.get('quality_gate')}`.",
        f"- SfM: {summary.get('registered_image_count')} registered images, {summary.get('sparse_point_count')} sparse points; the three source binary hashes match the exported COLMAP manifest.",
        f"- LiDAR: `{provenance['lidar_artifact_identity']['selected_path']}`; {inspection.get('selected', {}).get('point_count')} points; SHA-256 matches the inspection manifest.",
        f"- Alignment input identity: point counts and raw bounding-box extrema are checked against the saved report. That report has no source-file hashes, so its historical SfM input cannot be cryptographically tied to the current reconstruction beyond those checks.",
        "",
        "| Artifact | Path | SHA-256 |",
        "|---|---|---|",
    ]
    for key, row in provenance.items():
        if isinstance(row, dict) and "path" in row:
            lines.append(f"| {key} | `{row['path']}` | `{row['sha256']}` |")
    lines.extend(
        [
            "",
            "## Saved transform validation",
            "",
            f"- Direction: `{metrics['transform']['direction']}`.",
            f"- Convention: `{metrics['transform']['matrix_convention']}`.",
            f"- Similarity scale: `{_fmt(metrics['transform']['scale_lidar_units_per_sfm_unit'])}` LiDAR coordinate units per SfM unit.",
            f"- Forward/inverse product maximum absolute error: `{_fmt(metrics['transform']['forward_inverse_max_abs_error'], 4)}`; inverse/forward: `{_fmt(metrics['transform']['inverse_forward_max_abs_error'], 4)}`.",
            f"- Uniform-scale rotation error: `{_fmt(metrics['transform']['uniform_scale_rotation_error'], 4)}`. The forward and inverse matrices and declared scale pass. The redundant `rotation_sfm_to_lidar_row_major` field matches the matrix-derived rotation: `{metrics['transform']['auxiliary_rotation_metadata_matches_matrix']}` (maximum difference `{_fmt(metrics['transform']['auxiliary_rotation_metadata_max_abs_error'], 4)}`); it is not used to transform points.",
            "- The candidate label remains `REJECTED_CANDIDATE — DIAGNOSTIC_ONLY`.",
            "",
            "## Independent geometry summary",
            "",
            "| Cloud | Finite points | Raw bbox diagonal | Robust 0.5–99.5% diagonal | Approx. downsampled points | NN spacing median / P95 |",
            "|---|---:|---:|---:|---:|---:|",
            f"| SfM | {sfm['finite_point_count']:,} | {_fmt(sfm['raw_bounds']['diagonal'])} SfM units | {_fmt(sfm['robust_axis_bounds_0_5_to_99_5_percent']['diagonal'])} SfM units | {sfm['downsampled_point_count_at_0_015_bbox_diagonal_voxel']:,} | {_fmt(sfm['nearest_neighbor_spacing_raw_units']['median'])} / {_fmt(sfm['nearest_neighbor_spacing_raw_units']['p95'])} SfM units |",
            f"| DepthEye | {lidar['finite_point_count']:,} | {_fmt(lidar['raw_bounds']['diagonal'])} unknown LiDAR units | {_fmt(lidar['robust_axis_bounds_0_5_to_99_5_percent']['diagonal'])} unknown LiDAR units | {lidar['downsampled_point_count_at_0_015_bbox_diagonal_voxel']:,} | {_fmt(lidar['nearest_neighbor_spacing_raw_units']['median'])} / {_fmt(lidar['nearest_neighbor_spacing_raw_units']['p95'])} unknown LiDAR units |",
            "",
            "Raw extrema are shown alongside percentile bounds because a bbox can be stretched by isolated geometry. Nearest-neighbor and voxel-density summaries describe sampling density, not physical completeness. The exact coordinate bounds, centroids, PCA axes, density bins, and outlier screens are in `diagnostic-metrics.json`.",
            "",
            "## Original global registration and rejection",
            "",
            f"The original result remains `REJECTED` (`accepted: false`). RANSAC correspondences: {alignment.get('global_registration', {}).get('feature_correspondence_count')}; feature-fit P95: {_fmt(evidence.get('feature_fit_residuals', {}).get('p95'))}; forward overlap: {_fmt(evidence.get('forward_overlap_fraction'))}; reverse overlap: {_fmt(evidence.get('reverse_overlap_fraction'))}; bidirectional normalized P95: {_fmt(evidence.get('bidirectional_p95_normalized_residual'))}; maximum accepted P95: {_fmt(alignment.get('validation_policy', {}).get('maximum_normalized_p95_residual'))}.",
            "",
            f"Local refinement: {alignment.get('registration_evidence', {}).get('local_similarity_refinement', {}).get('iterations')} iterations, {alignment.get('registration_evidence', {}).get('local_similarity_refinement', {}).get('final_correspondence_count')} final correspondences, converged by parameter delta: {alignment.get('registration_evidence', {}).get('local_similarity_refinement', {}).get('converged_by_parameter_delta')}.",
            "",
            "## Candidate coverage and residual results",
            "",
            f"The exact saved transform produced a forward normalized P95 of `{_fmt(cand['sfm_to_lidar_engine_normalized']['p95'])}`, reverse P95 `{_fmt(cand['lidar_to_sfm_engine_normalized']['p95'])}`, and bidirectional P95 `{_fmt(cand['bidirectional_engine_normalized_p95'])}`. The forward/reverse denominators are each cloud's raw bbox diagonal, matching the Engine code. Reverse distances in a common LiDAR-frame denominator are also reported separately.",
            "",
            f"The saved Engine overlap validation sampled at most 10,000 source points per direction with the same deterministic stride. Reproduction used {cand['engine_validation_sample_reproduction']['sfm_to_lidar_sample_count']:,} SfM points and {cand['engine_validation_sample_reproduction']['lidar_to_sfm_sample_count']:,} LiDAR points; its sampled bidirectional P95 is `{_fmt(cand['engine_validation_sample_reproduction']['bidirectional_p95'])}`. The all-point distributions below use every source point and therefore need not have exactly the same overlap fraction as that sampled screen.",
            "",
            "| Normalized distance tolerance | SfM→LiDAR overlap | LiDAR→SfM overlap |",
            "|---:|---:|---:|",
        ]
    )
    for row in cand["engine_normalized_overlap_by_threshold"]:
        lines.append(
            f"| {_fmt(row['threshold_normalized'], 4)} | {_fmt(row['sfm_to_lidar_fraction'] * 100, 4)}% ({row['sfm_to_lidar_count']:,}) | {_fmt(row['lidar_to_sfm_fraction'] * 100, 4)}% ({row['lidar_to_sfm_count']:,}) |"
        )
    lines.extend(
        [
            "",
            f"Same-stride reproduction deltas vs. the saved report at 0.03: forward overlap {_fmt(cand['engine_validation_sample_reproduction']['deltas_vs_saved_report'].get('sfm_to_lidar_overlap_fraction_at_0_03'), 4)}, reverse overlap {_fmt(cand['engine_validation_sample_reproduction']['deltas_vs_saved_report'].get('lidar_to_sfm_overlap_fraction_at_0_03'), 4)}, bidirectional P95 {_fmt(cand['engine_validation_sample_reproduction']['deltas_vs_saved_report'].get('bidirectional_p95'), 4)}.",
            "",
            f"The raw LiDAR/SfM bbox diagonal ratio is `{_fmt(norm['lidar_to_sfm_raw_bbox_diagonal_ratio'])}` and the candidate similarity scale is `{_fmt(norm['candidate_similarity_scale_lidar_units_per_sfm_unit'])}`. The robust raw diagonal ratio is `{_fmt(norm['lidar_to_sfm_robust_bbox_diagonal_ratio'])}`; after applying the saved transform, the robust SfM AABB diagonal is `{_fmt(norm['candidate_aligned_sfm_robust_bbox_diagonal_lidar_units'])}` LiDAR units versus `{_fmt(norm['lidar_robust_bbox_diagonal_0_5_to_99_5'])}` LiDAR units for the scan. These are extent diagnostics only; partial coverage and outliers can distort bbox comparisons.",
            "",
            "### Spatially selected common-envelope candidate",
            "",
            common["selection"],
            "",
            f"The intersection contains {common['sfm_spatial_candidate_count']:,} SfM points ({_fmt(common['sfm_spatial_candidate_fraction_of_full'] * 100, 4)}% of SfM) and {common['lidar_spatial_candidate_count']:,} LiDAR points ({_fmt(common['lidar_spatial_candidate_fraction_of_full'] * 100, 4)}% of LiDAR). Within this residual-independent spatial subset, SfM→LiDAR P95 is `{_fmt(common['sfm_residuals_in_spatial_candidate']['p95'])}` and LiDAR→SfM P95 is `{_fmt(common['lidar_residuals_in_spatial_candidate']['p95'])}` under the original directional normalization.",
            "",
            f"Within the spatial candidate, {common['sfm_common_candidate_count_within_0_03']:,} SfM points and {common['lidar_common_candidate_count_within_0_03']:,} LiDAR points are individually within 0.03 of a point in the opposite cloud. These are directional point counts, not unique reciprocal correspondences.",
            "",
            f"Of all SfM points beyond 0.03, {unmatched['sfm_unmatched_outside_lidar_envelope_count']:,} lie outside the robust LiDAR envelope plus padding ({_fmt(unmatched['sfm_unmatched_outside_lidar_envelope_fraction_of_unmatched'], 4)} of unmatched SfM points); {unmatched['sfm_inside_lidar_envelope_but_unmatched_count']:,} are inside that envelope but still over threshold. For LiDAR, {unmatched['lidar_unmatched_outside_sfm_envelope_count']:,} unmatched points lie outside the transformed robust SfM envelope and {unmatched['lidar_inside_sfm_envelope_but_unmatched_count']:,} are inside it.",
            "",
            "Spatial-bin residuals and same-voxel occupancy overlap are available in the JSON metrics. Same-voxel occupancy is a conservative support indicator and changes with resolution; it is not an acceptance test.",
            "",
            "## H1–H5 assessment",
            "",
        ]
    )
    octants = common["octant_consistency"]
    local_good = sum(row["overlap_fraction_at_0_03"] >= 0.5 for row in octants)
    local_bad = sum(row["overlap_fraction_at_0_03"] < 0.2 for row in octants)
    lines.extend(
        [
            f"### H1 — Partial coverage: {'supported' if unmatched['sfm_unmatched_outside_lidar_envelope_fraction_of_unmatched'] is not None and unmatched['sfm_unmatched_outside_lidar_envelope_fraction_of_unmatched'] >= 0.5 and common['sfm_residuals_in_spatial_candidate']['p95'] is not None and common['sfm_residuals_in_spatial_candidate']['p95'] <= 0.03 else 'not established'}",
            "",
            f"Relative plausibility: weak as the main explanation. Forward/reverse global overlap is asymmetric ({_fmt(evidence.get('forward_overlap_fraction'))} vs. {_fmt(evidence.get('reverse_overlap_fraction'))}); however, only {unmatched['sfm_unmatched_outside_lidar_envelope_count']:,} unmatched SfM points lie outside the robust scan envelope ({_fmt(unmatched['sfm_unmatched_outside_lidar_envelope_fraction_of_unmatched'] * 100 if unmatched['sfm_unmatched_outside_lidar_envelope_fraction_of_unmatched'] is not None else None, 4)}% of unmatched SfM), while {unmatched['sfm_inside_lidar_envelope_but_unmatched_count']:,} are inside it. Spatial-subset P95 values are {_fmt(common['sfm_residuals_in_spatial_candidate']['p95'])} and {_fmt(common['lidar_residuals_in_spatial_candidate']['p95'])}; {local_good} spatial bins have at least half their points within threshold. The envelope is only a percentile AABB, not a surveyed coverage boundary.",
            "",
            f"### H2 — Incorrect transformation: {'supported' if local_bad >= 2 and common['sfm_residuals_in_spatial_candidate']['p95'] is not None and common['sfm_residuals_in_spatial_candidate']['p95'] > 0.08 else 'not ruled out'}",
            "",
            f"Relative plausibility: remains material. The saved candidate has global bidirectional P95 {_fmt(cand['bidirectional_engine_normalized_p95'])}; even inside the spatially selected envelope, the reverse P95 is {_fmt(common['lidar_residuals_in_spatial_candidate']['p95'])} and forward P95 is {_fmt(common['sfm_residuals_in_spatial_candidate']['p95'])}. {local_bad} evaluated spatial bins have less than 20% of SfM points inside threshold, while {local_good} bins have at least half. The report records {alignment.get('global_registration', {}).get('feature_correspondence_count')} FPFH/RANSAC correspondences and a low feature-fit P95, but neither proves a correct global match. No independent manual landmarks or local correction were used.",
            "",
            "### H3 — Different geometry representations: plausible, unproven",
            "",
            f"The clouds have {sfm['finite_point_count']:,} vs. {lidar['finite_point_count']:,} points. Under the candidate scale, median SfM nearest-neighbor spacing is {_fmt(metrics['geometry']['density_comparison_in_candidate_lidar_frame']['sfm_nearest_neighbor_median_lidar_units'])} LiDAR units versus {_fmt(metrics['geometry']['density_comparison_in_candidate_lidar_frame']['lidar_nearest_neighbor_median_lidar_units'])} LiDAR units for DepthEye (ratio {_fmt(metrics['geometry']['density_comparison_in_candidate_lidar_frame']['mapped_sfm_to_lidar_median_spacing_ratio'])}). That indicates different sampling density, but does not explain whether the nearest points lie on corresponding surfaces. Occlusion, view-dependent texture, vegetation, and scene change are not separable from these coordinates alone. No artwork or surface labels were inferred.",
            "",
            "### H4 — Scale or coordinate normalization: evaluated",
            "",
            f"Relative plausibility: a secondary contributor. Candidate scale is {_fmt(norm['candidate_similarity_scale_lidar_units_per_sfm_unit'])}; raw bbox scale ratio is {_fmt(norm['lidar_to_sfm_raw_bbox_diagonal_ratio'])}; robust raw bbox ratio is {_fmt(norm['lidar_to_sfm_robust_bbox_diagonal_ratio'])}. The SfM raw bbox diagonal contracts from {_fmt(norm['sfm_raw_bbox_diagonal'])} to {_fmt(norm['sfm_robust_bbox_diagonal_0_5_to_99_5'])} under percentile bounds. The independently trimmed sensitivity in `diagnostic-metrics.json` retains input points and does not select an alternate transform. This shows bbox sensitivity, not a corrected scale.",
            "",
            f"### H5 — Insufficient local refinement: {'present in saved evidence' if metrics.get('saved_local_refinement', {}).get('converged_by_parameter_delta') is False else 'not indicated'}",
            "",
            f"Relative plausibility: the recorded refinement is insufficient by its own convergence criterion, but it does not explain the residual by itself. It stopped at {metrics.get('saved_local_refinement', {}).get('iterations')} of {metrics.get('saved_local_refinement', {}).get('maximum_iterations')} iterations with {metrics.get('saved_local_refinement', {}).get('final_correspondence_count')} final correspondences and no parameter-delta convergence. More iterations are not assumed to solve a wrong initialization.",
            "",
            "## Diagnostic classification and recommendation",
            "",
            f"Classification: `{classification}`. This label does not alter the Engine's current rejection.",
            "",
        ]
    )
    if classification == "PROMISING_PARTIAL_ALIGNMENT":
        lines.append(
            "The measurements justify a targeted overlap-aware registration experiment in an isolated workspace. Keep the existing threshold and rejection intact; test robust spatial trimming, geometry-supported local refinement, and validation on held-out spatial regions. Require independently reviewed correspondences or structures before treating any alternate fit as plausible."
        )
    elif classification == "LIKELY_WRONG_TRANSFORM":
        lines.append(
            "Investigate initialization with independent geometric evidence or manual correspondences before testing relaxed global thresholds. The current evidence does not support explaining the rejection by partial coverage alone."
        )
    elif classification == "LIKELY_INCOMPATIBLE_COVERAGE":
        lines.append(
            "The current scan and reconstruction have little robust spatial-envelope overlap under the saved candidate. Establish corresponding landmarks or a new capture overlap before developing overlap-aware validation for this pair."
        )
    else:
        lines.append(
            "Partial coverage alone is not supported as the main cause: most unmatched SfM points still fall inside the robust scan envelope, and all-point reverse residuals remain high. The saved transform has localized spatial support, but the evidence is not strong enough to separate a wrong initialization from representation and density differences. This single run does not yet justify adding overlap-aware acceptance to the general Engine. Do not change the quality gate. First obtain manually verified shared structures or correspondences; then a small held-out overlap-aware experiment would be informative."
        )
    lines.extend(
        [
            "",
            "## Visual diagnostics",
            "",
            "- [Interactive point-cloud viewer](coverage-viewer.html): candidate alignment and raw side-by-side coordinate mode, orbit/pan/zoom, cloud and camera visibility, and point-size control.",
            "- [Raw and candidate geometry overview](overview.png). Raw rows use separate cloud axes; the aligned row applies only the rejected candidate.",
            "- [Spatial coverage and candidate residual view](coverage.png).",
            "- [Directional normalized residual distributions](residuals.png).",
            "",
            "## Reproduction",
            "",
            "```sh",
            command,
            "```",
            "",
            "`diagnostic-cache.npz` and `diagnostic-cache.json` cache the read-only geometry calculations. Rerun with `--force` to recompute. The diagnostic output is independent and can be regenerated without modifying Engine state.",
            "",
            "## Limits",
            "",
            "This analysis is geometric only. It cannot establish that a matched region corresponds to the same physical object, that the transform is free of symmetry ambiguity, or that the full scan coverage is known. Camera pose centers show the photo path but do not define LiDAR coverage. Synthetic tests verify utility mathematics, not Nam Shan alignment quality.",
            "",
        ]
    )
    return "\n".join(lines)


def _sample(points: np.ndarray, limit: int, seed: int) -> np.ndarray:
    if len(points) <= limit:
        return np.arange(len(points), dtype=np.int64)
    rng = np.random.default_rng(seed)
    return np.sort(rng.choice(len(points), size=limit, replace=False))


def _point_subset(points: np.ndarray, mask: np.ndarray, limit: int, seed: int) -> np.ndarray:
    selected = np.flatnonzero(mask)
    if len(selected) > limit:
        rng = np.random.default_rng(seed)
        selected = np.sort(rng.choice(selected, size=limit, replace=False))
    return points[selected]


def generate_static_images(metrics: dict[str, Any], arrays: dict[str, np.ndarray], output_dir: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.load_default()
    dark = (24, 28, 36)
    light = (245, 247, 250)
    grid = (213, 218, 226)
    sfm_color = (32, 118, 220)
    lidar_color = (241, 143, 45)
    matched_color = (22, 157, 116)
    unmatched_color = (212, 73, 87)
    camera_color = (97, 73, 180)
    axis_pairs = ((0, 1, "XY"), (0, 2, "XZ"), (1, 2, "YZ"))

    sfm = arrays["sfm"]
    lidar = arrays["lidar"]
    aligned = arrays["sfm_in_lidar"]
    cameras = arrays["cameras_in_lidar"]
    forward = arrays["forward_normalized"]
    reverse = arrays["reverse_normalized"]
    sfm_idx = _sample(sfm, 10_000, 2701)
    lidar_idx = _sample(lidar, 10_000, 2702)
    aligned_idx = _sample(aligned, 10_000, 2701)
    lidar_aligned_idx = _sample(lidar, 10_000, 2702)
    camera_idx = _sample(cameras, 1000, 2703)

    def draw_header(draw, size, title, subtitle):
        draw.rectangle((0, 0, size[0], 84), fill=dark)
        draw.text((24, 18), title, font=font, fill=light)
        draw.text((24, 48), subtitle, font=font, fill=(190, 200, 215))

    def draw_panel(draw, box, point_sets, pair, title, robust=True, cameras_points=None):
        x0, y0, x1, y1 = box
        pad = 42
        plot = (x0 + pad, y0 + 34, x1 - 16, y1 - 32)
        draw.rectangle(box, fill=(255, 255, 255), outline=grid, width=1)
        draw.text((x0 + 12, y0 + 10), title, font=font, fill=dark)
        draw.rectangle(plot, outline=grid, width=1)
        clouds = [points for points, _, _ in point_sets if len(points)]
        if cameras_points is not None and len(cameras_points):
            clouds.append(cameras_points)
        if not clouds:
            return
        axes = [points[:, [pair[0], pair[1]]] for points in clouds]
        merged = np.concatenate(axes, axis=0)
        low = np.quantile(merged, 0.005, axis=0) if robust else np.min(merged, axis=0)
        high = np.quantile(merged, 0.995, axis=0) if robust else np.max(merged, axis=0)
        span = np.maximum(high - low, 1e-9)
        draw.text((plot[0], plot[3] + 7), f"{('X','Y','Z')[pair[0]]}", font=font, fill=dark)
        draw.text((plot[0] + 22, plot[1] + 2), f"{('X','Y','Z')[pair[1]]}", font=font, fill=dark)
        for points, color, radius in point_sets:
            if not len(points):
                continue
            xy = points[:, [pair[0], pair[1]]]
            uv = (xy - low) / span
            keep = ((uv >= 0) & (uv <= 1)).all(axis=1)
            for u, v in uv[keep]:
                px = int(plot[0] + u * (plot[2] - plot[0]))
                py = int(plot[3] - v * (plot[3] - plot[1]))
                draw.ellipse((px-radius, py-radius, px+radius, py+radius), fill=color)
        if cameras_points is not None and len(cameras_points):
            xy = cameras_points[:, [pair[0], pair[1]]]
            uv = (xy - low) / span
            keep = ((uv >= 0) & (uv <= 1)).all(axis=1)
            projected = []
            for u, v in uv[keep]:
                projected.append((int(plot[0] + u * (plot[2] - plot[0])), int(plot[3] - v * (plot[3] - plot[1]))))
            if len(projected) > 1:
                draw.line(projected, fill=camera_color, width=2)
            for px, py in projected:
                draw.ellipse((px-3, py-3, px+3, py+3), fill=camera_color)

    width, height = 1900, 1480
    image = Image.new("RGB", (width, height), light)
    draw = ImageDraw.Draw(image)
    draw_header(
        draw,
        image.size,
        "Nam Shan Playground - point cloud geometry overview",
        "Raw rows use separate coordinate frames. Candidate row applies only the saved REJECTED_CANDIDATE - DIAGNOSTIC_ONLY SfM-to-LiDAR transform.",
    )
    col_width = width // 3
    row_height = (height - 96) // 3
    raw_sfm = sfm[sfm_idx]
    raw_lidar = lidar[lidar_idx]
    for row, (cloud, label, color) in enumerate(((raw_sfm, "Raw SfM coordinates (arbitrary units)", sfm_color), (raw_lidar, "Raw DepthEye coordinates (units unknown)", lidar_color))):
        y0 = 92 + row * row_height
        for col, (a, b, name) in enumerate(axis_pairs):
            box = (col * col_width + 8, y0, (col + 1) * col_width - 8, y0 + row_height - 8)
            draw_panel(draw, box, [(cloud, color, 1)], (a, b, name), f"{label} - {name}")
    y0 = 92 + 2 * row_height
    for col, (a, b, name) in enumerate(axis_pairs):
        box = (col * col_width + 8, y0, (col + 1) * col_width - 8, height - 8)
        draw_panel(
            draw,
            box,
            [(aligned[aligned_idx], sfm_color, 1), (lidar[lidar_aligned_idx], lidar_color, 1)],
            (a, b, name),
            f"Candidate aligned in LiDAR frame - {name}",
            cameras_points=cameras[camera_idx],
        )
    image.save(output_dir / "overview.png", optimize=True)

    image = Image.new("RGB", (1900, 760), light)
    draw = ImageDraw.Draw(image)
    draw_header(
        draw,
        image.size,
        "Spatial coverage under the rejected candidate",
        "SfM points are green when within 0.03 directional normalized distance; red when farther. LiDAR is gray; cameras are purple.",
    )
    for col, (a, b, name) in enumerate(axis_pairs):
        box = (col * col_width + 8, 96, (col + 1) * col_width - 8, 750)
        matched = aligned[aligned_idx][forward[aligned_idx] <= 0.03]
        unmatched = aligned[aligned_idx][forward[aligned_idx] > 0.03]
        # Use fixed masks from deterministic full-cloud indices, matching the sampled rows.
        draw_panel(
            draw,
            box,
            [(lidar[lidar_aligned_idx], (180, 185, 194), 1), (matched, matched_color, 2), (unmatched, unmatched_color, 2)],
            (a, b, name),
            f"Candidate spatial coverage - {name}",
            cameras_points=cameras[camera_idx],
        )
    image.save(output_dir / "coverage.png", optimize=True)

    image = Image.new("RGB", (1800, 760), light)
    draw = ImageDraw.Draw(image)
    draw_header(
        draw,
        image.size,
        "Bidirectional normalized residual distributions",
        "Threshold 0.03 is the saved Engine screen. Forward and reverse use their original direction-specific bbox normalization.",
    )
    plot_top, plot_bottom = 120, 650
    hist_left, hist_right = 115, 875
    max_x = 0.20
    bins = np.linspace(0, max_x, 61)
    for panel_index, (values, label) in enumerate(((forward, "SfM to LiDAR (LiDAR bbox diagonal)"), (reverse, "LiDAR to SfM (SfM bbox diagonal)"))):
        x0 = 30 + panel_index * 890
        x1 = x0 + 850
        draw.rectangle((x0, 96, x1, 730), fill=(255, 255, 255), outline=grid)
        draw.text((x0 + 18, 108), label, font=font, fill=dark)
        hist, _ = np.histogram(values, bins=bins)
        max_count = max(int(hist.max()), 1)
        chart = (x0 + 58, plot_top, x1 - 24, plot_bottom)
        draw.line((chart[0], chart[3], chart[2], chart[3]), fill=dark, width=1)
        draw.line((chart[0], chart[1], chart[0], chart[3]), fill=dark, width=1)
        bar_width = (chart[2] - chart[0]) / len(hist)
        for i, count in enumerate(hist):
            bar_height = (int(count) / max_count) * (chart[3] - chart[1])
            left = chart[0] + i * bar_width
            draw.rectangle((left, chart[3] - bar_height, left + max(1, bar_width - 1), chart[3]), fill=(85, 137, 190))
        threshold_x = chart[0] + (0.03 / max_x) * (chart[2] - chart[0])
        draw.line((threshold_x, chart[1], threshold_x, chart[3]), fill=(205, 43, 61), width=3)
        stats = distance_statistics(values)
        draw.text((x0 + 18, 680), f"Median {_fmt(stats['median'],4)} | P95 {_fmt(stats['p95'],4)} | >0.20 fraction {_fmt(float(np.mean(values > max_x)),4)}", font=font, fill=dark)
        draw.text((threshold_x + 4, chart[1] + 4), "0.03", font=font, fill=(205, 43, 61))
    image.save(output_dir / "residuals.png", optimize=True)


def generate_viewer(metrics: dict[str, Any], arrays: dict[str, np.ndarray], output_dir: Path, max_points: int) -> None:
    try:
        import plotly.graph_objects as go
        from plotly.offline import get_plotlyjs
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "Plotly is required for the offline interactive viewer. Install later with "
            "`uv add --optional lidar-diagnostic plotly`, then rerun the diagnostic. "
            "No dependency is installed automatically."
        ) from exc

    sfm = arrays["sfm"]
    lidar = arrays["lidar"]
    aligned = arrays["sfm_in_lidar"]
    cameras = arrays["cameras_in_lidar"]
    lidar_center = np.mean(lidar, axis=0)
    lidar_diagonal = metrics["normalization_and_scale"]["lidar_raw_bbox_diagonal"]
    sfm_center = np.mean(sfm, axis=0)
    sfm_diagonal = metrics["normalization_and_scale"]["sfm_raw_bbox_diagonal"]
    forward = arrays["forward_normalized"]
    reverse = arrays["reverse_normalized"]

    sfm_idx = _sample(sfm, max_points, 8801)
    lidar_idx = _sample(lidar, max_points, 8802)
    sfm_match = forward[sfm_idx] <= 0.03
    lidar_match = reverse[lidar_idx] <= 0.03
    category_indices = [
        sfm_idx[sfm_match],
        sfm_idx[~sfm_match],
        lidar_idx[lidar_match],
        lidar_idx[~lidar_match],
        np.arange(len(cameras), dtype=np.int64),
    ]
    candidate_points = [aligned[category_indices[0]], aligned[category_indices[1]], lidar[category_indices[2]], lidar[category_indices[3]], cameras]
    raw_sfm = (sfm - sfm_center) / sfm_diagonal + np.asarray([-0.8, 0.0, 0.0])
    raw_lidar = (lidar - lidar_center) / lidar_diagonal + np.asarray([0.8, 0.0, 0.0])
    raw_camera = (cameras - sfm_center) / sfm_diagonal + np.asarray([-0.8, 0.0, 0.0])
    raw_points = [raw_sfm[category_indices[0]], raw_sfm[category_indices[1]], raw_lidar[category_indices[2]], raw_lidar[category_indices[3]], raw_camera]
    labels = [
        "SfM points ≤ 0.03",
        "SfM points > 0.03",
        "DepthEye points ≤ 0.03",
        "DepthEye points > 0.03",
        "Registered camera centers",
    ]
    colors = ["#16a085", "#d64b5e", "#ed922f", "#8e99a7", "#6953bd"]
    sizes = [2.5, 2.2, 2.0, 1.7, 5]
    traces = []
    for index, (points, label, color, size) in enumerate(zip(candidate_points, labels, colors, sizes)):
        traces.append(
            go.Scatter3d(
                x=points[:, 0] if len(points) else [],
                y=points[:, 1] if len(points) else [],
                z=points[:, 2] if len(points) else [],
                mode="markers",
                name=label,
                marker={"size": size, "color": color, "opacity": 0.72 if index < 4 else 0.95},
                hovertemplate="x=%{x:.4f}<br>y=%{y:.4f}<br>z=%{z:.4f}<extra>%{fullData.name}</extra>",
            )
        )
    figure = go.Figure(data=traces)
    figure.update_layout(
        title="REJECTED_CANDIDATE — DIAGNOSTIC_ONLY | SfM transformed into DepthEye LiDAR frame",
        scene={
            "xaxis_title": "LiDAR-frame coordinate / LiDAR bbox diagonal",
            "yaxis_title": "LiDAR-frame coordinate / LiDAR bbox diagonal",
            "zaxis_title": "LiDAR-frame coordinate / LiDAR bbox diagonal",
            "aspectmode": "data",
            "camera": {"projection": {"type": "orthographic"}},
        },
        legend={"groupclick": "togglegroup", "itemsizing": "constant"},
        margin={"l": 0, "r": 0, "t": 60, "b": 0},
        paper_bgcolor="#f7f8fa",
        plot_bgcolor="#f7f8fa",
        height=820,
    )
    plot_div = figure.to_html(full_html=False, include_plotlyjs=False, div_id="coverage-viewer", config={"responsive": True, "displaylogo": False})
    mode_data = {
        mode: [
            {axis: points[:, axis].tolist() for axis in range(3)}
            for points in point_rows
        ]
        for mode, point_rows in (("candidate", candidate_points), ("raw", raw_points))
    }
    mode_json = json.dumps(mode_data, separators=(",", ":"), allow_nan=False).replace("</", "<\\/")
    legend_controls = "".join(
        f'<label><input type="checkbox" checked data-traces="{indices}"> {caption}</label>'
        for indices, caption in (("0,1", "SfM"), ("2,3", "DepthEye"), ("4", "Camera centers"))
    )
    html_document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>LiDAR–SfM coverage diagnostic</title>
<style>
body{{font:14px system-ui,-apple-system,sans-serif;margin:0;background:#f7f8fa;color:#20242b}}header{{padding:16px 20px;background:#20242b;color:#fff}}header h1{{font-size:19px;margin:0 0 5px}}header p{{margin:0;color:#d1d8e1}}#controls{{display:flex;flex-wrap:wrap;gap:16px;align-items:center;padding:12px 18px;background:#fff;border-bottom:1px solid #d9dee7}}label{{display:inline-flex;gap:6px;align-items:center}}select,input{{accent-color:#315f9a}}#mode-note{{padding:10px 18px;background:#eef2f6;color:#333}}#coverage-viewer{{width:100%;height:calc(100vh - 190px);min-height:560px}}.warn{{color:#8b2635;font-weight:600}}
</style></head><body>
<header><h1>Nam Shan Playground — LiDAR–SfM partial coverage diagnostic</h1><p class="warn">Saved alignment remains REJECTED_CANDIDATE — DIAGNOSTIC_ONLY. No transform is accepted by this viewer.</p></header>
<div id="controls"><label>View <select id="mode"><option value="candidate">Candidate alignment (common LiDAR frame)</option><option value="raw">Raw coordinates (independently normalized and offset side-by-side)</option></select></label>
<label>Point size <input id="size" type="range" min="1" max="8" value="3"><span id="size-value">3</span></label>{legend_controls}</div>
<div id="mode-note">Candidate mode applies the saved SfM→LiDAR matrix. Coordinates are divided by the raw LiDAR bounding-box diagonal for display. The LiDAR units are unknown.</div>
<script>{get_plotlyjs()}</script>
{plot_div}
<script>
const modeData={mode_json}; const plot=document.getElementById('coverage-viewer'); const traces=[0,1,2,3,4];
document.getElementById('mode').addEventListener('change', event=>{{const mode=event.target.value;const rows=modeData[mode];Plotly.restyle(plot,{{x:rows.map(r=>r[0]),y:rows.map(r=>r[1]),z:rows.map(r=>r[2])}},traces);document.getElementById('mode-note').textContent=mode==='candidate'?'Candidate mode applies the saved SfM→LiDAR matrix. Coordinates are divided by the raw LiDAR bounding-box diagonal for display. The LiDAR units are unknown.':'Raw mode independently normalizes each cloud by its own raw bbox diagonal and offsets them side-by-side. Relative positions are not registration evidence; colors still show candidate residual categories.';}});
document.getElementById('size').addEventListener('input', event=>{{const value=Number(event.target.value);document.getElementById('size-value').textContent=value;Plotly.restyle(plot,{{'marker.size':value}},traces.slice(0,4));}});
for(const input of document.querySelectorAll('[data-traces]')){{input.addEventListener('change', event=>{{const indices=event.target.dataset.traces.split(',').map(Number);Plotly.restyle(plot,{{visible:event.target.checked?true:'legendonly'}},indices);}});}}
</script></body></html>"""
    (output_dir / "coverage-viewer.html").write_text(html_document, encoding="utf-8")


def _signature(files: dict[str, Path], report: Path) -> dict[str, Any]:
    result = {name: {"path": str(path), "sha256": sha256_file(path)} for name, path in files.items()}
    result["utility_version"] = UTILITY_VERSION
    result["source_report_path"] = str(report)
    return result


def run_diagnostic(args: argparse.Namespace) -> Path:
    output_dir = Path(args.output_dir).expanduser().resolve()
    if ".engine" in output_dir.parts:
        raise ValueError("Diagnostic output must not be placed inside an .engine directory")
    output_dir.mkdir(parents=True, exist_ok=True)

    provenance, sfm_files, summary, inspection, alignment = validate_inputs(
        args.sfm_summary, args.sfm_manifest, args.lidar_inspection, args.alignment_report
    )
    sfm_dir = Path(summary["reconstruction_directory"]).resolve()
    lidar_path = Path(inspection["selected"]["path"]).resolve()
    files = {
        "sfm_summary": Path(args.sfm_summary).resolve(),
        "sfm_manifest": Path(args.sfm_manifest).resolve(),
        "lidar_inspection": Path(args.lidar_inspection).resolve(),
        "alignment_report": Path(args.alignment_report).resolve(),
        "lidar_ply": lidar_path,
        **{f"sfm_{name}": path for name, path in sfm_files.items()},
    }
    signature = _signature(files, Path(args.alignment_report).resolve())
    cache_npz = output_dir / "diagnostic-cache.npz"
    cache_json = output_dir / "diagnostic-cache.json"
    metrics_path = output_dir / "diagnostic-metrics.json"
    use_cache = False
    if not args.force and cache_npz.is_file() and cache_json.is_file() and metrics_path.is_file():
        try:
            use_cache = read_json(cache_json).get("input_signature") == signature
        except (OSError, ValueError, json.JSONDecodeError):
            use_cache = False

    if use_cache:
        with np.load(cache_npz, allow_pickle=False) as archive:
            arrays = {key: archive[key] for key in archive.files}
        metrics = read_json(metrics_path)
    else:
        sfm = read_colmap_points3d(sfm_files["points3D.bin"])
        camera_centers, camera_names = read_colmap_camera_centers(sfm_files["images.bin"])
        lidar, ply_schema = read_ascii_ply_xyz(lidar_path)
        if len(sfm) != int(summary["sparse_point_count"]):
            raise ValueError("Loaded SfM point count differs from the validated summary")
        if len(sfm) != int(alignment.get("input_point_counts", {}).get("sfm_sample_used", -1)):
            raise ValueError("Loaded SfM point count differs from the rejected alignment report")
        if len(lidar) != int(inspection["selected"]["point_count"]):
            raise ValueError("Loaded LiDAR point count differs from inspection manifest")
        if len(lidar) != int(alignment.get("input_point_counts", {}).get("lidar_finite_points_loaded", -1)):
            raise ValueError("Loaded LiDAR point count differs from rejected alignment report")
        if len(camera_centers) != int(summary.get("camera_pose_count", -1)):
            raise ValueError("Camera-center count differs from the validated SfM summary")

        sfm_min, sfm_max = _bounds(sfm)
        lidar_min, lidar_max = _bounds(lidar)
        for label, actual, recorded in (
            ("SfM", (sfm_min, sfm_max), alignment.get("input_bounds", {}).get("sfm", {})),
            ("LiDAR", (lidar_min, lidar_max), alignment.get("input_bounds", {}).get("lidar", {})),
        ):
            if not recorded or not np.allclose(actual[0], recorded.get("minimum"), atol=1e-5, rtol=1e-7) or not np.allclose(actual[1], recorded.get("maximum"), atol=1e-5, rtol=1e-7):
                raise ValueError(f"Loaded {label} bounds differ from those recorded in the saved alignment report")

        # Validate the saved forward/inverse pair and scale before applying it.
        validate_similarity_pair(
            np.asarray(alignment["similarity_transform_sfm_to_lidar_row_major"], dtype=np.float64),
            np.asarray(alignment["similarity_transform_lidar_to_sfm_row_major"], dtype=np.float64),
            alignment.get("scale_lidar_units_per_sfm_unit"),
            alignment.get("rotation_sfm_to_lidar_row_major"),
        )

        sfm_summary = point_cloud_summary(sfm, "SfM")
        lidar_summary = point_cloud_summary(lidar, "DepthEye LiDAR")
        metrics, arrays = analyze_geometry(
            sfm,
            lidar,
            camera_centers,
            alignment,
            sfm_summary,
            lidar_summary,
        )
        metrics["camera_image_names"] = camera_names
        metrics["lidar_ply_schema"] = ply_schema
        metrics["provenance"] = provenance
        np.savez_compressed(cache_npz, **arrays)
        cache_json.write_text(json.dumps({"input_signature": signature}, indent=2) + "\n", encoding="utf-8")
        metrics_path.write_text(json.dumps(metrics, indent=2, allow_nan=False) + "\n", encoding="utf-8")

    # Preserve provenance when loading cached numeric results.
    metrics["provenance"] = provenance
    classification, classification_evidence = classify_diagnostic(metrics)
    metrics["classification"] = classification
    metrics["classification_evidence"] = classification_evidence
    metrics_path.write_text(json.dumps(metrics, indent=2, allow_nan=False) + "\n", encoding="utf-8")

    command = "\n".join(
        [
            "cd /Users/homan/repo/exhibition-archive && \\",
            "UV_CACHE_DIR=/private/tmp/exhibition-archive-uv-cache \\",
            "OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 VECLIB_MAXIMUM_THREADS=2 NUMEXPR_NUM_THREADS=2 \\",
            "uv run --offline --no-sync python tools/diagnostics/lidar_sfm_coverage.py \\",
            f"  --sfm-summary {Path(args.sfm_summary).resolve()} \\",
            f"  --sfm-manifest {Path(args.sfm_manifest).resolve()} \\",
            f"  --lidar-inspection {Path(args.lidar_inspection).resolve()} \\",
            f"  --alignment-report {Path(args.alignment_report).resolve()} \\",
            f"  --output-dir {output_dir}",
        ]
    )
    metrics["regeneration_command"] = command
    metrics_path.write_text(json.dumps(metrics, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    report = render_report(metrics, provenance, summary, inspection, alignment, output_dir, command)
    (output_dir / "lidar-partial-coverage-report.md").write_text(report, encoding="utf-8")
    generate_static_images(metrics, arrays, output_dir)
    generate_viewer(metrics, arrays, output_dir, int(args.visual_points))
    return output_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sfm-summary", required=True, help="Validated work/sfm-summary.json")
    parser.add_argument("--sfm-manifest", required=True, help="Exported COLMAP manifest.json whose hashes match the validated artifact")
    parser.add_argument("--lidar-inspection", required=True, help="Engine lidar/inspection.json")
    parser.add_argument("--alignment-report", required=True, help="Saved rejected lidar/alignment-report.json")
    parser.add_argument("--output-dir", required=True, help="Independent output directory outside .engine")
    parser.add_argument("--visual-points", type=int, default=VISUAL_SAMPLE_LIMIT, help="Maximum displayed samples per point cloud (default: 12000)")
    parser.add_argument("--force", action="store_true", help="Ignore a matching diagnostic cache and recompute")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.visual_points < 100:
        parser.error("--visual-points must be at least 100")
    try:
        output = run_diagnostic(args)
    except Exception as exc:
        print(f"Diagnostic stopped: {exc}", file=sys.stderr)
        return 2
    print(f"Diagnostic outputs: {output}")
    for name in (
        "lidar-partial-coverage-report.md",
        "coverage-viewer.html",
        "overview.png",
        "coverage.png",
        "residuals.png",
        "diagnostic-metrics.json",
    ):
        print(f"  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
