from __future__ import annotations

import math
import statistics
import struct
from pathlib import Path
from typing import Any, Iterator

from .adapters import sha256_file, write_json
from .backends import IMAGE_SUFFIXES, PLY_TYPES, RAW_IMAGE_SUFFIXES


GAUSSIAN_PROPERTIES = {
    "x", "y", "z", "opacity", "f_dc_0", "f_dc_1", "f_dc_2",
    "scale_0", "scale_1", "scale_2", "rot_0", "rot_1", "rot_2", "rot_3",
}
COLOUR_PROPERTY_SETS = (("red", "green", "blue"), ("r", "g", "b"),
                        ("diffuse_red", "diffuse_green", "diffuse_blue"))
NORMAL_PROPERTIES = ("nx", "ny", "nz")
GAUSSIAN_SIGNATURE_PREFIXES = ("f_dc_", "scale_", "rot_")
MAX_HEADER_BYTES = 1024 * 1024
MAX_INSPECTION_SAMPLE = 20_000
ALIGNMENT_VALIDATION_POLICY = {
    "minimum_sparse_sfm_points": 50,
    "minimum_correspondences": 12,
    "minimum_bidirectional_overlap": 0.20,
    "maximum_normalized_p95_residual": 0.03,
    "overlap_distance_basis": "3% of each source bounding-box diagonal after independent normalization; a conservative screening tolerance, not a survey tolerance.",
    "global_ransac_iterations": 200_000,
    "global_random_seed": 2024,
    "local_similarity_refinement_iterations": 12,
    "local_refinement_trim_fraction": 0.80,
    "maximum_refinement_correspondence_distance": 0.03,
}


class PlyInspectionError(ValueError):
    pass


def discover_input_files(directory: str | Path, *, hash_images: bool = True,
                         hash_ply: bool = True) -> dict[str, Any]:
    """Inventory supported photographs and classify PLY files without filename guesses."""
    root = Path(directory).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Input directory does not exist: {root}")
    images, ply_files, unsupported_images, ignored_files = [], [], [], []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        suffix = path.suffix.casefold()
        if suffix in IMAGE_SUFFIXES:
            images.append({"relative_path": relative, "size_bytes": path.stat().st_size,
                           "sha256": sha256_file(path) if hash_images else None,
                           "format": suffix.lstrip(".").upper()})
        elif suffix == ".ply":
            header, header_error = _read_header(path)
            properties = _vertex_property_names(header) if header else []
            if GAUSSIAN_PROPERTIES.issubset(properties):
                classification = "GAUSSIAN_SPLATTING_MODEL"
            elif {"x", "y", "z"}.issubset(properties) and any(
                    name.startswith(GAUSSIAN_SIGNATURE_PREFIXES) for name in properties):
                classification = "POSSIBLE_GAUSSIAN_SPLATTING_MODEL"
            else:
                classification = "RAW_POINT_CLOUD_CANDIDATE" if header else "UNCLASSIFIED_PLY_CANDIDATE"
            ply_files.append({"relative_path": relative, "size_bytes": path.stat().st_size,
                              "sha256": sha256_file(path) if hash_ply else None,
                              "classification": classification,
                              "format": _format_from_header(header),
                              "vertex_properties": properties,
                              "header_error": header_error})
        elif suffix in RAW_IMAGE_SUFFIXES:
            unsupported_images.append({"relative_path": relative, "format": suffix.lstrip(".").upper(),
                                       "reason": "RAW_CAMERA_FORMAT_NOT_VERIFIED_BY_INSTALLED_SPIRULA_HELP"})
        else:
            ignored_files.append(relative)
    gaussian_classes = {"GAUSSIAN_SPLATTING_MODEL", "POSSIBLE_GAUSSIAN_SPLATTING_MODEL"}
    lidar_candidates = [row for row in ply_files if row["classification"] not in gaussian_classes]
    gaussian_models = [row for row in ply_files if row["classification"] in gaussian_classes]
    if len(lidar_candidates) == 1:
        lidar_status, selected = "SELECTED_UNAMBIGUOUS", lidar_candidates[0]["relative_path"]
    elif len(lidar_candidates) > 1:
        lidar_status, selected = "AMBIGUOUS_MULTIPLE_CANDIDATES", None
    else:
        lidar_status, selected = "NOT_DETECTED", None
    warnings = []
    if gaussian_models:
        warnings.append("Gaussian-splat PLY file(s) were classified as model artifacts and excluded from raw LiDAR selection.")
    if len(lidar_candidates) > 1:
        warnings.append("Multiple non-Gaussian PLY files were found; no raw point cloud was selected.")
    return {"schema_version": "exhibition-archive.input-discovery.v1",
            "input_directory": str(root), "image_count": len(images), "images": images,
            "image_formats": {fmt: sum(row["format"] == fmt for row in images)
                              for fmt in sorted({row["format"] for row in images})},
            "ply_files": ply_files, "raw_lidar_status": lidar_status,
            "unsupported_raw_images": unsupported_images,
            "ignored_file_count": len(ignored_files), "ignored_files": ignored_files,
            "selected_raw_lidar_relative_path": selected,
            "gaussian_model_count": len(gaussian_models), "warnings": warnings,
            "minimum_photo_count_met": len(images) >= 2}


def inspect_ply(path: str | Path, *, preview_path: str | Path | None = None,
                max_preview_points: int = 50_000) -> dict[str, Any]:
    """Inspect an ASCII or binary PLY, reading vertex geometry and preserving source bytes."""
    file = Path(path).expanduser().resolve()
    header, error = _read_header(file)
    if header is None:
        raise PlyInspectionError(error or "Could not read the PLY header")
    fmt = _format_from_header(header)
    if fmt not in {"ascii", "binary_little_endian", "binary_big_endian"}:
        raise PlyInspectionError(f"Unsupported PLY format declaration: {fmt or 'missing'}")
    vertex = next((element for element in header["elements"] if element["name"] == "vertex"), None)
    if not vertex or vertex["count"] <= 0:
        raise PlyInspectionError("PLY has no non-empty vertex element")
    props = vertex["properties"]
    names = [prop["name"] for prop in props]
    if len(names) != len(set(names)):
        raise PlyInspectionError("PLY vertex property names are duplicated")
    missing_xyz = sorted({"x", "y", "z"} - set(names))
    if missing_xyz:
        raise PlyInspectionError(f"PLY vertex element is missing coordinates: {missing_xyz}")
    vertex_index = header["elements"].index(vertex)
    if vertex_index != 0:
        raise PlyInspectionError("PLY vertex element is not first; safe generic element skipping is unavailable")
    if any(prop["list"] for prop in props):
        raise PlyInspectionError("List-valued vertex properties are unsupported")
    if any(prop["type"] not in PLY_TYPES for prop in props):
        raise PlyInspectionError("PLY contains an unsupported vertex scalar type")

    point_count = vertex["count"]
    stride = max(1, math.ceil(point_count / MAX_INSPECTION_SAMPLE))
    preview_stride = max(1, math.ceil(point_count / max(1, int(max_preview_points))))
    coordinate_samples: list[tuple[float, float, float]] = []
    preview_vertices: list[tuple[float, float, float, int, int, int]] = []
    minima = [math.inf, math.inf, math.inf]
    maxima = [-math.inf, -math.inf, -math.inf]
    invalid_coordinate_count = 0
    invalid_property_count = 0
    colour_names = next((group for group in COLOUR_PROPERTY_SETS if set(group).issubset(names)), None)
    has_normals = set(NORMAL_PROPERTIES).issubset(names)
    property_index = {name: index for index, name in enumerate(names)}
    sample_properties = [property_index[name] for name in ("x", "y", "z")]
    if colour_names:
        sample_properties.extend(property_index[name] for name in colour_names)
    record_size = sum(PLY_TYPES[prop["type"]][1] for prop in props)
    endian = "<" if fmt == "binary_little_endian" else ">"
    fmt_struct = struct.Struct(endian + "".join(PLY_TYPES[prop["type"]][0] for prop in props))
    body_offset = header["body_offset"]
    with file.open("rb") as stream:
        stream.seek(body_offset)
        for index in range(point_count):
            if fmt == "ascii":
                raw = stream.readline()
                if not raw:
                    raise PlyInspectionError(f"ASCII PLY vertex data is truncated at row {index}")
                try:
                    tokens = raw.split()
                    if len(tokens) != len(props):
                        raise PlyInspectionError(f"Malformed ASCII PLY vertex row {index}")
                    row = [float(token) for token in tokens]
                except (UnicodeDecodeError, ValueError) as exc:
                    raise PlyInspectionError(f"Malformed numeric value in ASCII PLY vertex row {index}") from exc
            else:
                raw = stream.read(record_size)
                if len(raw) != record_size:
                    raise PlyInspectionError(f"Binary PLY vertex data is truncated at row {index}")
                try:
                    row = list(fmt_struct.unpack(raw))
                except struct.error as exc:
                    raise PlyInspectionError(f"Malformed binary PLY vertex row {index}") from exc
            values = [float(row[slot]) for slot in sample_properties]
            xyz = values[:3]
            if not all(math.isfinite(value) for value in xyz):
                invalid_coordinate_count += 1
            else:
                for axis, value in enumerate(xyz):
                    minima[axis] = min(minima[axis], value)
                    maxima[axis] = max(maxima[axis], value)
                if index % stride == 0 or index == point_count - 1:
                    coordinate_samples.append((xyz[0], xyz[1], xyz[2]))
            if any(not math.isfinite(float(value)) for value in row):
                invalid_property_count += 1
            if preview_path is not None and (index % preview_stride == 0 or index == point_count - 1) and all(math.isfinite(value) for value in xyz):
                if colour_names:
                    rgb = [_to_byte(value) for value in values[3:6]]
                else:
                    rgb = [185, 198, 184]
                preview_vertices.append((xyz[0], xyz[1], xyz[2], *rgb))
    if invalid_coordinate_count:
        bounds = None
        diagonal = None
    else:
        bounds = {axis: [minima[i], maxima[i]] for i, axis in enumerate(("x", "y", "z"))}
        diagonal = math.sqrt(sum((maxima[i] - minima[i]) ** 2 for i in range(3)))
    outliers = _sample_outlier_summary(coordinate_samples)
    if preview_path is not None:
        _write_preview(Path(preview_path), preview_vertices, bool(colour_names))
    unit_comments = [comment for comment in header["comments"]
                     if any(word in comment.casefold() for word in ("unit", "meter", "metre", "millimeter", "centimeter"))]
    is_gaussian = GAUSSIAN_PROPERTIES.issubset(names)
    return {"schema_version": "exhibition-archive.lidar-inspection.v1",
            "path": str(file), "sha256": sha256_file(file), "file_bytes": file.stat().st_size,
            "format": fmt, "point_count": point_count, "vertex_properties": names,
            "point_cloud_type": "GAUSSIAN_SPLATTING_MODEL" if is_gaussian else "RAW_POINT_CLOUD",
            "rgb_available": bool(colour_names), "rgb_properties": list(colour_names) if colour_names else [],
            "normals_available": has_normals, "normal_properties": list(NORMAL_PROPERTIES) if has_normals else [],
            "bounding_box": bounds, "bounding_box_diagonal": diagonal,
            "coordinate_units": {"status": "DECLARED_IN_HEADER_COMMENT" if unit_comments else "UNKNOWN_OR_UNDECLARED",
                                 "evidence": unit_comments},
            "invalid_coordinate_count": invalid_coordinate_count,
            "non_finite_vertex_property_rows": invalid_property_count,
            "sampled_spatial_outliers": outliers,
            "inspection_sample_count": len(coordinate_samples),
            "preview": ({"path": str(Path(preview_path).resolve()),
                         "point_count": len(preview_vertices), "status": "WRITTEN"}
                        if preview_path is not None else None),
            "status": "VALID" if invalid_coordinate_count == 0 else "INVALID_NONFINITE_COORDINATES",
            "alignment": {"status": "NOT_ATTEMPTED", "reason": "Awaiting validated photo SfM geometry."},
            "downstream_consumption": {"training": False, "navigation": False}}


def inspect_optional_lidar(discovery: dict[str, Any], input_directory: str | Path,
                           output_directory: str | Path) -> dict[str, Any]:
    """Produce a report and preview while keeping malformed optional LiDAR recoverable."""
    root = Path(input_directory).expanduser().resolve()
    output = Path(output_directory)
    gaussian_classes = {"GAUSSIAN_SPLATTING_MODEL", "POSSIBLE_GAUSSIAN_SPLATTING_MODEL"}
    candidates = [row for row in discovery.get("ply_files", [])
                  if row.get("classification") not in gaussian_classes]
    result: dict[str, Any] = {"schema_version": "exhibition-archive.lidar-stage.v1",
                              "detected": bool(candidates), "candidate_count": len(candidates),
                              "selection_status": discovery.get("raw_lidar_status", "NOT_DETECTED"),
                              "alignment_attempted": False, "alignment_status": "NOT_ATTEMPTED",
                              "training_consumed": False, "navigation_consumed": False,
                              "candidates": [], "warnings": []}
    output.mkdir(parents=True, exist_ok=True)
    if not candidates:
        result["inspection_status"] = "NOT_DETECTED"
        result["warnings"].append("No non-Gaussian PLY point-cloud candidate was detected.")
    elif len(candidates) > 1:
        result["inspection_status"] = "AMBIGUOUS"
        result["warnings"].append("Multiple non-Gaussian PLY candidates were found; none was selected or inspected as authoritative LiDAR.")
        result["candidates"] = [{"relative_path": row["relative_path"], "classification": row["classification"],
                                  "sha256": row["sha256"]} for row in candidates]
    else:
        selected = candidates[0]
        source = root / selected["relative_path"]
        preview = output / "lidar-preview.ply"
        try:
            inspection = inspect_ply(source, preview_path=preview)
            result.update({"inspection_status": inspection["status"], "selected": inspection,
                           "preview_path": str(preview.resolve())})
            if inspection["status"] != "VALID":
                result["warnings"].append("The raw point cloud contains invalid coordinates; photo-only reconstruction may continue.")
        except (OSError, PlyInspectionError, ValueError, struct.error) as exc:
            result["inspection_status"] = "INCOMPATIBLE_OR_CORRUPT"
            result["selected"] = {"relative_path": selected["relative_path"], "sha256": selected["sha256"],
                                  "format": selected.get("format"), "inspection_error": str(exc)}
            result["warnings"].append(f"LiDAR inspection failed and will not block photo reconstruction: {exc}")
    target = output / "inspection.json"
    write_json(target, result)
    return {**result, "inspection_report_path": str(target.resolve())}


def read_ply_xyz_sample(path: str | Path, maximum: int = 20_000) -> dict[str, Any]:
    """Read a deterministic, bounded XYZ sample from a scalar-vertex PLY."""
    file = Path(path).expanduser().resolve()
    if maximum < 1:
        raise ValueError("maximum must be positive")
    header, error = _read_header(file)
    if header is None:
        raise PlyInspectionError(error or "Could not read PLY header")
    fmt = _format_from_header(header)
    if fmt not in {"ascii", "binary_little_endian", "binary_big_endian"}:
        raise PlyInspectionError(f"Unsupported PLY format: {fmt}")
    vertex = next((element for element in header["elements"] if element["name"] == "vertex"), None)
    if not vertex or header["elements"].index(vertex) != 0 or any(row["list"] for row in vertex["properties"]):
        raise PlyInspectionError("PLY must begin with a scalar-valued vertex element")
    properties = vertex["properties"]
    names = [row["name"] for row in properties]
    if len(names) != len(set(names)) or not {"x", "y", "z"}.issubset(names) or any(row["type"] not in PLY_TYPES for row in properties):
        raise PlyInspectionError("PLY vertex must provide scalar XYZ coordinates with supported types")
    count = int(vertex["count"])
    stride = max(1, math.ceil(count / maximum))
    positions = [names.index(name) for name in ("x", "y", "z")]
    endian = "<" if fmt == "binary_little_endian" else ">"
    row_struct = struct.Struct(endian + "".join(PLY_TYPES[row["type"]][0] for row in properties))
    points, invalid_count = [], 0
    minima, maxima = [math.inf] * 3, [-math.inf] * 3
    with file.open("rb") as stream:
        stream.seek(header["body_offset"])
        for index in range(count):
            if fmt == "ascii":
                raw = stream.readline()
                if not raw:
                    raise PlyInspectionError(f"PLY vertex data is truncated at row {index}")
                try:
                    values = [float(value) for value in raw.split()]
                except ValueError as exc:
                    raise PlyInspectionError(f"Invalid numeric PLY vertex at row {index}") from exc
                if len(values) != len(properties):
                    raise PlyInspectionError(f"Malformed PLY vertex row {index}")
            else:
                raw = stream.read(row_struct.size)
                if len(raw) != row_struct.size:
                    raise PlyInspectionError(f"PLY vertex data is truncated at row {index}")
                values = [float(value) for value in row_struct.unpack(raw)]
            xyz = [values[position] for position in positions]
            if not all(math.isfinite(value) for value in xyz):
                invalid_count += 1
                continue
            for axis, value in enumerate(xyz):
                minima[axis] = min(minima[axis], value)
                maxima[axis] = max(maxima[axis], value)
            if index % stride == 0 or index == count - 1:
                points.append(xyz)
    return {"path": str(file), "point_count": count, "sampled_point_count": len(points),
            "sample_stride": stride, "invalid_sampled_point_count": invalid_count,
            "points": points, "bounding_box": {axis: [minima[i], maxima[i]]
                                                  for i, axis in enumerate(("x", "y", "z"))}}


def propose_lidar_ground_plane(points: list[list[float]], camera_centers: list[list[float]], *,
                               seed: int = 2024) -> dict[str, Any]:
    """Propose a dominant camera-aligned plane; never treat it as approved ground."""
    try:
        import numpy as np
    except ImportError:
        return {"status": "UNAVAILABLE", "reason": "NumPy is unavailable for bounded plane diagnostics."}
    cloud = np.asarray(points, dtype=float)
    cameras = np.asarray(camera_centers, dtype=float)
    cloud = cloud[np.isfinite(cloud).all(axis=1)] if cloud.ndim == 2 and cloud.shape[1:] == (3,) else np.empty((0, 3))
    cameras = cameras[np.isfinite(cameras).all(axis=1)] if cameras.ndim == 2 and cameras.shape[1:] == (3,) else np.empty((0, 3))
    if len(cloud) < 100 or len(cameras) < 6:
        return {"status": "UNVERIFIED", "reason": "At least 100 aligned LiDAR sample points and 6 camera centers are required for a stable plane proposal.",
                "point_count": int(len(cloud)), "camera_count": int(len(cameras))}
    centered_cameras = cameras - cameras.mean(axis=0)
    eigenvalues, eigenvectors = np.linalg.eigh(np.cov(centered_cameras.T))
    if (eigenvalues[1] <= 1e-12 or eigenvalues[2] <= 1e-12 or
            eigenvalues[0] / eigenvalues[1] > 0.15 or eigenvalues[1] / eigenvalues[2] < 0.05):
        return {"status": "UNVERIFIED", "reason": "Camera centers do not support a stable, approximately planar trajectory reference.",
                "camera_trajectory_covariance_eigenvalues": eigenvalues.tolist()}
    trajectory_normal = eigenvectors[:, 0]
    diagonal = float(np.linalg.norm(cloud.max(axis=0) - cloud.min(axis=0)))
    if not math.isfinite(diagonal) or diagonal <= 1e-9:
        return {"status": "UNVERIFIED", "reason": "Aligned LiDAR sample has degenerate spatial extent."}
    # The 0.3%-of-diagonal band screens points for a planar candidate while
    # scaling with arbitrary SfM units; it is not a survey accuracy claim.
    distance_tolerance = diagonal * 0.003
    cosine_tolerance = math.cos(math.radians(15.0))
    rng = np.random.default_rng(seed)
    best_indices = None
    best_normal = None
    best_offset = None
    for _ in range(400):
        selected = rng.choice(len(cloud), size=3, replace=False)
        first, second, third = cloud[selected]
        normal = np.cross(second - first, third - first)
        norm = float(np.linalg.norm(normal))
        if norm <= 1e-10:
            continue
        normal /= norm
        if abs(float(np.dot(normal, trajectory_normal))) < cosine_tolerance:
            continue
        offset = -float(np.dot(normal, first))
        inliers = np.flatnonzero(np.abs(cloud @ normal + offset) <= distance_tolerance)
        if best_indices is None or len(inliers) > len(best_indices):
            best_indices, best_normal, best_offset = inliers, normal, offset
    minimum_inliers = max(100, math.ceil(len(cloud) * 0.05))
    if best_indices is None or len(best_indices) < minimum_inliers:
        return {"status": "UNVERIFIED", "reason": "No camera-trajectory-aligned plane has sufficient sampled support.",
                "point_count": int(len(cloud)), "camera_count": int(len(cameras)),
                "minimum_inliers": minimum_inliers, "distance_tolerance_sfm_units": distance_tolerance}
    camera_sides = cameras @ best_normal + best_offset
    if float(np.median(camera_sides)) < 0:
        best_normal, best_offset, camera_sides = -best_normal, -best_offset, -camera_sides
    above_fraction = float(np.mean(camera_sides >= -distance_tolerance))
    if above_fraction < 0.9:
        return {"status": "UNVERIFIED", "reason": "Camera centers do not lie consistently on one side of the candidate plane.",
                "camera_side_fraction": above_fraction, "plane_inlier_count": int(len(best_indices))}
    return {"status": "PROPOSED", "classification": "PROPOSED_REQUIRES_HUMAN_GROUND_IDENTIFICATION",
            "normal": best_normal.tolist(), "offset": float(best_offset),
            "coordinate_frame": "source", "source": "accepted aligned DepthEye sample plus registered camera trajectory",
            "evidence": {"point_count": int(len(cloud)), "camera_count": int(len(cameras)),
                         "plane_inlier_count": int(len(best_indices)),
                         "plane_inlier_fraction": float(len(best_indices) / len(cloud)),
                         "camera_side_fraction": above_fraction,
                         "camera_trajectory_covariance_eigenvalues": eigenvalues.tolist(),
                         "distance_tolerance_sfm_units": distance_tolerance,
                         "normal_to_camera_trajectory_plane_degrees": math.degrees(math.acos(min(1.0, abs(float(np.dot(best_normal, trajectory_normal))))))},
            "limitations": ["A dominant plane may be a platform, canopy, equipment surface, or ground; this is not semantic or safety validation.",
                            "Plane thresholds use arbitrary SfM units and require human inspection before navigation approval."]}


def attempt_similarity_alignment(lidar_inspection: dict[str, Any], sfm_points: list[list[float]],
                                 output_directory: str | Path) -> dict[str, Any]:
    """Try feature-based, scale-aware global registration; reject weak evidence without failing SfM."""
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"schema_version": "exhibition-archive.lidar-alignment.v1",
                              "alignment_attempted": False, "status": "REJECTED",
                              "accepted": False, "training_consumed": False,
                              "navigation_consumed": False,
                              "input_coordinate_systems": {"sfm": "COLMAP arbitrary non-metric reconstruction units",
                                                            "lidar": "DepthEye PLY frame; units unknown unless header declares them"},
                              "method": "Global FPFH feature correspondence in normalized scale, followed by robust 7-DoF similarity fit and bidirectional overlap validation.",
                              "validation_policy": dict(ALIGNMENT_VALIDATION_POLICY),
                              "evidence": [], "warnings": []}
    if lidar_inspection.get("inspection_status") != "VALID":
        report["reason"] = "LiDAR was absent, ambiguous, corrupt, or failed point-cloud inspection."
        report["lidar_inspection_status"] = lidar_inspection.get("inspection_status", "UNKNOWN")
        write_json(output / "alignment-report.json", report)
        return report
    minimum_points = ALIGNMENT_VALIDATION_POLICY["minimum_sparse_sfm_points"]
    if len(sfm_points) < minimum_points:
        report["reason"] = f"Only {len(sfm_points)} sparse SfM points are available; at least {minimum_points} are required for the global feature registration attempt."
        report["sfm_sparse_point_count"] = len(sfm_points)
        write_json(output / "alignment-report.json", report)
        return report
    lidar_source_path = Path(lidar_inspection.get("selected", {}).get("path", ""))
    lidar_preview_path = Path(lidar_inspection.get("preview_path", ""))
    if not lidar_source_path.is_file() or not lidar_preview_path.is_file():
        report["reason"] = "The selected LiDAR file is no longer present at its inspected path."
        write_json(output / "alignment-report.json", report)
        return report
    try:
        import numpy as np
    except ImportError as exc:
        report["reason"] = "Optional NumPy geometry support is not installed; photo-only reconstruction remains available."
        report["registration_dependency"] = {"name": "numpy", "status": "UNAVAILABLE", "error": str(exc)}
        write_json(output / "alignment-report.json", report)
        return report
    try:
        import open3d as o3d
    except (ImportError, OSError) as exc:
        report["reason"] = "Optional Open3D registration support is unavailable; photo-only reconstruction remains available. Install the project extra `.[lidar-registration]` to enable it."
        report["registration_dependency"] = {"name": "open3d", "status": "UNAVAILABLE", "error": str(exc)}
        write_json(output / "alignment-report.json", report)
        return report
    report["alignment_attempted"] = True
    report["registration_dependency"] = {"name": "open3d", "version": getattr(o3d, "__version__", "unknown"),
                                         "status": "AVAILABLE"}
    try:
        lidar_cloud = o3d.io.read_point_cloud(str(lidar_source_path), remove_nan_points=False, remove_infinite_points=False)
        lidar_array = np.asarray(lidar_cloud.points, dtype=float)
        sfm_array = np.asarray(sfm_points, dtype=float)
        lidar_array = lidar_array[np.isfinite(lidar_array).all(axis=1)]
        sfm_array = sfm_array[np.isfinite(sfm_array).all(axis=1)]
        if len(lidar_array) < 50 or len(sfm_array) < 50:
            raise ValueError("Fewer than 50 finite points remain in one of the two geometries.")
        if len(lidar_array) > 500_000:
            lidar_array = lidar_array[::math.ceil(len(lidar_array) / 500_000)]
        if len(sfm_array) > 500_000:
            sfm_array = sfm_array[::math.ceil(len(sfm_array) / 500_000)]
        lidar_bounds = {"minimum": lidar_array.min(axis=0).tolist(), "maximum": lidar_array.max(axis=0).tolist()}
        sfm_bounds = {"minimum": sfm_array.min(axis=0).tolist(), "maximum": sfm_array.max(axis=0).tolist()}
        lidar_center, sfm_center = lidar_array.mean(axis=0), sfm_array.mean(axis=0)
        lidar_scale = float(np.linalg.norm(lidar_array.max(axis=0) - lidar_array.min(axis=0)))
        sfm_scale = float(np.linalg.norm(sfm_array.max(axis=0) - sfm_array.min(axis=0)))
        if not all(math.isfinite(value) and value > 1e-9 for value in (lidar_scale, sfm_scale)):
            raise ValueError("A point cloud has degenerate spatial extent; similarity scale cannot be estimated.")
        lidar_normalized = (lidar_array - lidar_center) / lidar_scale
        sfm_normalized = (sfm_array - sfm_center) / sfm_scale
        for label, cloud in (("LiDAR", lidar_normalized), ("SfM", sfm_normalized)):
            eigenvalues = np.linalg.eigvalsh(np.cov(cloud.T))
            if len(eigenvalues) != 3 or eigenvalues[0] <= max(eigenvalues[-1], 1e-12) * 1e-8:
                raise ValueError(f"{label} geometry is planar, collinear, or otherwise degenerate for 3D alignment.")
        source = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(sfm_normalized))
        target = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(lidar_normalized))
        voxel = 0.015
        source_down = source.voxel_down_sample(voxel)
        target_down = target.voxel_down_sample(voxel)
        if min(len(source_down.points), len(target_down.points)) < 50:
            raise ValueError("Voxel down-sampling left fewer than 50 points in one geometry.")
        normal_radius, feature_radius = voxel * 2.5, voxel * 5.0
        for cloud in (source_down, target_down):
            cloud.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=normal_radius, max_nn=30))
        source_features = o3d.pipelines.registration.compute_fpfh_feature(
            source_down, o3d.geometry.KDTreeSearchParamHybrid(radius=feature_radius, max_nn=100))
        target_features = o3d.pipelines.registration.compute_fpfh_feature(
            target_down, o3d.geometry.KDTreeSearchParamHybrid(radius=feature_radius, max_nn=100))
        registration = o3d.pipelines.registration
        if hasattr(o3d.utility, "random") and hasattr(o3d.utility.random, "seed"):
            o3d.utility.random.seed(ALIGNMENT_VALIDATION_POLICY["global_random_seed"])
        result = registration.registration_ransac_based_on_feature_matching(
            source_down, target_down, source_features, target_features, True,
            0.03,
            registration.TransformationEstimationPointToPoint(False), 4,
            [registration.CorrespondenceCheckerBasedOnEdgeLength(0.9),
             registration.CorrespondenceCheckerBasedOnDistance(0.03)],
            registration.RANSACConvergenceCriteria(ALIGNMENT_VALIDATION_POLICY["global_ransac_iterations"], 0.999))
        correspondences = np.asarray(result.correspondence_set, dtype=int)
        report["input_point_counts"] = {
            "sfm_sample_used": len(sfm_array),
            "lidar_source_total_reported": lidar_inspection.get("selected", {}).get("point_count"),
            "lidar_finite_points_loaded": len(lidar_array),
            "lidar_inspection_preview_points": lidar_inspection.get("selected", {}).get("preview", {}).get("point_count"),
        }
        report["input_bounds"] = {"sfm": sfm_bounds, "lidar": lidar_bounds}
        report["global_registration"] = {"source_downsampled_count": len(source_down.points),
                                         "target_downsampled_count": len(target_down.points),
                                         "feature_correspondence_count": len(correspondences),
                                         "voxel_size_normalized": voxel,
                                         "global_method": "FPFH + RANSAC with edge-length and distance checkers",
                                         "ransac_iterations": ALIGNMENT_VALIDATION_POLICY["global_ransac_iterations"],
                                         "random_seed": ALIGNMENT_VALIDATION_POLICY["global_random_seed"],
                                         "normal_radius_voxels": 2.5, "feature_radius_voxels": 5.0}
        minimum_correspondences = ALIGNMENT_VALIDATION_POLICY["minimum_correspondences"]
        if len(correspondences) < minimum_correspondences:
            raise ValueError(f"Global feature registration found only {len(correspondences)} correspondences; at least {minimum_correspondences} are required.")
        source_down_array = np.asarray(source_down.points)
        target_down_array = np.asarray(target_down.points)
        source_match = source_down_array[correspondences[:, 0]]
        target_match = target_down_array[correspondences[:, 1]]
        initial_transform_normalized, initial_fit_residuals = _similarity_fit_numpy(source_match, target_match, np)
        transform_normalized, fit_residuals, refinement = _refine_similarity_open3d(
            source_down_array, target_down_array, initial_transform_normalized,
            ALIGNMENT_VALIDATION_POLICY["maximum_refinement_correspondence_distance"], np, o3d)
        rotation = transform_normalized[:3, :3]
        normalized_scale = float(np.cbrt(np.linalg.det(rotation)))
        if not math.isfinite(normalized_scale) or normalized_scale <= 0:
            raise ValueError("Estimated similarity transform has a non-positive scale or reflection.")
        rotation = rotation / normalized_scale
        scale = normalized_scale * lidar_scale / sfm_scale
        translation = lidar_center + lidar_scale * transform_normalized[:3, 3] - scale * rotation @ sfm_center
        transform = np.eye(4)
        transform[:3, :3] = scale * rotation
        transform[:3, 3] = translation
        transformed_sfm_normalized = (sfm_normalized @ transform_normalized[:3, :3].T) + transform_normalized[:3, 3]
        overlap_distance = ALIGNMENT_VALIDATION_POLICY["maximum_normalized_p95_residual"]
        forward_overlap, forward_p95 = _nearest_overlap(transformed_sfm_normalized, target, overlap_distance, np, o3d)
        inverse_normalized = np.linalg.inv(transform_normalized)
        transformed_lidar_normalized = (lidar_normalized @ inverse_normalized[:3, :3].T) + inverse_normalized[:3, 3]
        reverse_overlap, reverse_p95 = _nearest_overlap(transformed_lidar_normalized, source, overlap_distance, np, o3d)
        p95 = max(forward_p95, reverse_p95)
        validation = _alignment_validation_evidence(
            refinement["final_correspondence_count"], forward_overlap, reverse_overlap,
            p95, fit_residuals["p95"])
        accepted = validation["accepted"]
        rotation_only = rotation / normalized_scale
        report.update({"accepted": accepted, "status": "ACCEPTED" if accepted else "REJECTED",
                       "reason": None if accepted else "Global alignment did not satisfy the configured overlap and residual evidence checks.",
                       "initial_normalized_similarity_transform_sfm_to_lidar_row_major": initial_transform_normalized.tolist(),
                       "similarity_transform_sfm_to_lidar_row_major": transform.tolist(),
                       "similarity_transform_lidar_to_sfm_row_major": _inverse_similarity_transform(transform.tolist()),
                       "scale_lidar_units_per_sfm_unit": scale,
                       "rotation_sfm_to_lidar_row_major": rotation_only.tolist(),
                       "translation_lidar_units": translation.tolist(),
                       "registration_evidence": {"forward_overlap_fraction": forward_overlap,
                                                 "reverse_overlap_fraction": reverse_overlap,
                                                 "bidirectional_p95_normalized_residual": p95,
                                                 "feature_fit_residuals": fit_residuals,
                                                 "initial_feature_fit_residuals": initial_fit_residuals,
                                                 "local_similarity_refinement": refinement,
                                                 "global_ransac_correspondence_count": len(correspondences),
                                                 "sfm_point_count_used": len(sfm_array),
                                                 "lidar_point_count_used": len(lidar_array),
                                                 "validation_checks": validation["checks"]},
                       "evidence": ["Unknown scale was handled with independent bounding-box normalization and a positive 7-DoF similarity fit.",
                                    "FPFH/RANSAC provided global initialization, followed by trimmed mutual-nearest-neighbor similarity refinement.",
                                    "Acceptance checks include correspondence support, bidirectional overlap, and normalized residuals; these are a geometry screen, not proof against every symmetric-scene ambiguity.",
                                    "No point was removed from either source; the source LiDAR file was not modified."],
                       "warnings": [] if accepted else ["The transform was not accepted for downstream use."]})
        if accepted:
            report["aligned_preview_path"] = str((output / "aligned-cloud-preview.ply").resolve())
            report["aligned_preview_status"] = _transform_preview(lidar_preview_path,
                                                                    Path(report["aligned_preview_path"]),
                                                                    np.asarray(report["similarity_transform_lidar_to_sfm_row_major"], dtype=float))
            report["aligned_preview_coordinate_system"] = "COLMAP arbitrary non-metric reconstruction coordinates"
        report["downstream_consumption"] = {
            "training": False,
            "navigation": False,
            "reason": ("Training initialization from raw LiDAR is not enabled. An accepted aligned preview is eligible for navigation geometry proposals only after a separate component-scoped geometry review."
                       if accepted else "No downstream LiDAR use is permitted because alignment was rejected."),
        }
    except Exception as exc:
        report.update({"accepted": False, "status": "REJECTED", "reason": str(exc),
                       "warnings": ["LiDAR alignment was rejected; the photo-based reconstruction remains authoritative."]})
    write_json(output / "alignment-report.json", report)
    return report


def _refine_similarity_open3d(source, target, initial_transform, max_distance: float, np, o3d):
    """Refine a global initializer with trimmed mutual-nearest-neighbor 7-DoF fits."""
    transform = np.asarray(initial_transform, dtype=float).copy()
    source_cloud = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(source))
    target_cloud = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(target))
    target_tree = o3d.geometry.KDTreeFlann(target_cloud)
    iterations = int(ALIGNMENT_VALIDATION_POLICY["local_similarity_refinement_iterations"])
    trim_fraction = float(ALIGNMENT_VALIDATION_POLICY["local_refinement_trim_fraction"])
    sample_stride = max(1, math.ceil(len(source) / 30_000))
    final_count, final_residual = 0, math.inf
    completed_iterations = 0
    converged = False
    for iteration in range(iterations):
        transformed = source @ transform[:3, :3].T + transform[:3, 3]
        transformed_cloud = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(transformed))
        transformed_tree = o3d.geometry.KDTreeFlann(transformed_cloud)
        source_indices, target_indices, distances = [], [], []
        for source_index in range(0, len(source), sample_stride):
            count, nearest_target, squared = target_tree.search_knn_vector_3d(transformed[source_index].tolist(), 1)
            if count != 1 or not squared or squared[0] > max_distance * max_distance:
                continue
            target_index = int(nearest_target[0])
            reverse_count, nearest_source, _reverse_squared = transformed_tree.search_knn_vector_3d(
                target[target_index].tolist(), 1)
            if reverse_count != 1 or int(nearest_source[0]) != source_index:
                continue
            source_indices.append(source_index)
            target_indices.append(target_index)
            distances.append(math.sqrt(float(squared[0])))
        if len(source_indices) < ALIGNMENT_VALIDATION_POLICY["minimum_correspondences"]:
            raise ValueError(f"Mutual-neighbor refinement retained only {len(source_indices)} correspondences.")
        cutoff = float(np.percentile(np.asarray(distances), trim_fraction * 100.0))
        keep = [index for index, distance in enumerate(distances) if distance <= min(max_distance, cutoff)]
        if len(keep) < ALIGNMENT_VALIDATION_POLICY["minimum_correspondences"]:
            raise ValueError("Trimmed mutual-neighbor refinement retained too few correspondences.")
        source_match = source[np.asarray([source_indices[index] for index in keep], dtype=int)]
        target_match = target[np.asarray([target_indices[index] for index in keep], dtype=int)]
        refined, fit = _similarity_fit_numpy(source_match, target_match, np)
        delta = float(np.max(np.abs(refined - transform)))
        transform = refined
        final_count, final_residual = len(keep), fit["p95"]
        completed_iterations = iteration + 1
        if delta <= 1e-8:
            converged = True
            break
    fit = {**fit, "p95": final_residual}
    return transform, fit, {
                "method": "trimmed mutual-nearest-neighbor positive 7-DoF similarity fit",
                "iterations": completed_iterations, "maximum_iterations": iterations,
                "trim_fraction": trim_fraction, "maximum_correspondence_distance_normalized": max_distance,
                "final_correspondence_count": final_count,
                "sample_stride": sample_stride,
                "converged_by_parameter_delta": converged,
            }


def _alignment_validation_evidence(correspondence_count: int, forward_overlap: float,
                                   reverse_overlap: float, bidirectional_p95: float,
                                   feature_fit_p95: float) -> dict[str, Any]:
    """Apply the documented screen and return each measurable check for review."""
    policy = ALIGNMENT_VALIDATION_POLICY
    checks = {
        "minimum_correspondences": correspondence_count >= policy["minimum_correspondences"],
        "minimum_forward_overlap": forward_overlap >= policy["minimum_bidirectional_overlap"],
        "minimum_reverse_overlap": reverse_overlap >= policy["minimum_bidirectional_overlap"],
        "maximum_bidirectional_p95_residual": bidirectional_p95 <= policy["maximum_normalized_p95_residual"],
        "maximum_feature_fit_p95_residual": feature_fit_p95 <= policy["maximum_normalized_p95_residual"],
    }
    return {"accepted": all(checks.values()), "checks": checks}


def sample_colmap_sparse_points(directory: str | Path, maximum: int = 100_000) -> list[list[float]]:
    """Read a deterministic sparse-geometry sample without retaining all COLMAP points."""
    root = Path(directory)
    path = root / "points3D.bin"
    if maximum < 1:
        raise ValueError("maximum must be positive")
    with path.open("rb") as stream:
        prefix = stream.read(8)
        if len(prefix) != 8:
            raise PlyInspectionError(f"COLMAP point file is truncated: {path}")
        count = struct.unpack("<Q", prefix)[0]
        stride = max(1, math.ceil(count / maximum))
        points = []
        for index in range(count):
            header = stream.read(51)
            if len(header) != 51:
                raise PlyInspectionError(f"COLMAP point file is truncated at record {index}")
            point_id, x, y, z, red, green, blue, error, track_length = struct.unpack("<Q3d3BdQ", header)
            if index % stride == 0 and all(math.isfinite(value) for value in (x, y, z)):
                points.append([x, y, z])
            skip_bytes = track_length * 8
            if skip_bytes:
                stream.seek(skip_bytes, 1)
        if stream.read(1):
            raise PlyInspectionError(f"COLMAP point file has trailing bytes: {path}")
    return points


def colmap_sparse_point_count(directory: str | Path) -> int:
    path = Path(directory) / "points3D.bin"
    with path.open("rb") as stream:
        raw_count = stream.read(8)
    if len(raw_count) != 8:
        raise PlyInspectionError(f"COLMAP point file is truncated: {path}")
    return struct.unpack("<Q", raw_count)[0]


def _similarity_fit_numpy(source, target, np):
    source_center, target_center = source.mean(axis=0), target.mean(axis=0)
    source_zero, target_zero = source - source_center, target - target_center
    covariance = target_zero.T @ source_zero / len(source)
    u, singular, vt = np.linalg.svd(covariance)
    signs = np.ones(3)
    if np.linalg.det(u @ vt) < 0:
        signs[-1] = -1
    rotation = u @ np.diag(signs) @ vt
    variance = float(np.sum(source_zero ** 2) / len(source))
    if variance <= 1e-12:
        raise ValueError("Global correspondence geometry has insufficient variance.")
    scale = float(np.sum(singular * signs) / variance)
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError("Global correspondence geometry produced a non-positive similarity scale.")
    translation = target_center - scale * rotation @ source_center
    matrix = np.eye(4)
    matrix[:3, :3] = scale * rotation
    matrix[:3, 3] = translation
    distances = np.linalg.norm((source @ matrix[:3, :3].T + translation) - target, axis=1)
    return matrix, {"median": float(np.median(distances)), "p95": float(np.percentile(distances, 95)),
                    "max": float(np.max(distances))}


def _inverse_similarity_transform(transform: list[list[float]]) -> list[list[float]]:
    """Invert a 4x4 positive uniform-scale rigid transform using scalar math."""
    if len(transform) != 4 or any(len(row) != 4 for row in transform):
        raise ValueError("Similarity transform must be 4 x 4")
    linear = [[float(transform[row][column]) for column in range(3)] for row in range(3)]
    scale = math.sqrt(sum(value * value for value in linear[0]))
    if not math.isfinite(scale) or scale <= 1e-12:
        raise ValueError("Similarity transform has a degenerate scale")
    rotation = [[value / scale for value in row] for row in linear]
    inverse_linear = [[rotation[column][row] / scale for column in range(3)] for row in range(3)]
    translation = [float(transform[row][3]) for row in range(3)]
    inverse_translation = [-sum(inverse_linear[row][column] * translation[column] for column in range(3))
                           for row in range(3)]
    return [inverse_linear[row] + [inverse_translation[row]] for row in range(3)] + [[0.0, 0.0, 0.0, 1.0]]


def _nearest_overlap(source, target_cloud, threshold, np, o3d):
    tree = o3d.geometry.KDTreeFlann(target_cloud)
    indices = range(0, len(source), max(1, math.ceil(len(source) / 10_000)))
    distances = []
    for index in indices:
        _count, _ids, squared = tree.search_knn_vector_3d(source[index].tolist(), 1)
        if squared:
            distances.append(math.sqrt(float(squared[0])))
    if not distances:
        return 0.0, math.inf
    return sum(distance <= threshold for distance in distances) / len(distances), float(np.percentile(distances, 95))


def _transform_preview(source: Path, target: Path, transform) -> str:
    try:
        import numpy as np
        import open3d as o3d
        cloud = o3d.io.read_point_cloud(str(source))
        points = np.asarray(cloud.points)
        points = points @ transform[:3, :3].T + transform[:3, 3]
        cloud.points = o3d.utility.Vector3dVector(points)
        if not o3d.io.write_point_cloud(str(target), cloud, write_ascii=True):
            return "FAILED_TO_WRITE"
        return "WRITTEN"
    except Exception as exc:
        return f"FAILED:{exc}"


def _read_header(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        with path.open("rb") as stream:
            header = bytearray()
            while len(header) <= MAX_HEADER_BYTES:
                line = stream.readline()
                if not line:
                    return None, "PLY header ended before end_header"
                header.extend(line)
                if line.strip() == b"end_header":
                    break
            else:
                return None, "PLY header exceeds 1 MiB"
            lines = header.decode("ascii").splitlines()
            if not lines or lines[0].strip() != "ply":
                return None, "PLY magic header is missing"
            elements: list[dict[str, Any]] = []
            current = None
            comments = []
            formats = []
            for line in lines[1:]:
                fields = line.split()
                if not fields:
                    continue
                if fields[0] in {"comment", "obj_info"}:
                    comments.append(" ".join(fields[1:]))
                elif fields[0] == "format" and len(fields) >= 3:
                    formats.append(fields[1])
                elif fields[0] == "element" and len(fields) == 3:
                    try:
                        count = int(fields[2])
                    except ValueError:
                        return None, f"Invalid PLY element count: {line}"
                    if count < 0:
                        return None, f"Negative PLY element count: {line}"
                    current = {"name": fields[1], "count": count, "properties": []}
                    elements.append(current)
                elif fields[0] == "property" and current:
                    if len(fields) == 3 and fields[1] in PLY_TYPES:
                        current["properties"].append({"name": fields[2], "type": fields[1], "list": False})
                    elif len(fields) == 5 and fields[1] == "list" and fields[2] in PLY_TYPES and fields[3] in PLY_TYPES:
                        current["properties"].append({"name": fields[4], "count_type": fields[2],
                                                       "type": fields[3], "list": True})
                    else:
                        return None, f"Unsupported PLY property declaration: {line}"
            if len(formats) != 1:
                return None, "PLY must declare exactly one format"
            return {"format": formats[0], "elements": elements, "comments": comments,
                    "body_offset": stream.tell()}, None
    except (OSError, UnicodeDecodeError) as exc:
        return None, str(exc)


def _vertex_property_names(header: dict[str, Any] | None) -> list[str]:
    if not header:
        return []
    vertex = next((element for element in header["elements"] if element["name"] == "vertex"), None)
    return [prop["name"] for prop in vertex["properties"]] if vertex else []


def _format_from_header(header: dict[str, Any] | None) -> str | None:
    return header.get("format") if header else None


def _to_byte(value: float) -> int:
    if not math.isfinite(value):
        return 0
    if 0 <= value <= 1:
        value *= 255
    return max(0, min(255, round(value)))


def _sample_outlier_summary(points: list[tuple[float, float, float]]) -> dict[str, Any]:
    if len(points) < 8:
        return {"status": "INSUFFICIENT_SAMPLE", "sample_count": len(points), "candidate_count": 0}
    center = [statistics.median(point[axis] for point in points) for axis in range(3)]
    distances = [math.dist(point, center) for point in points]
    median = statistics.median(distances)
    mad = statistics.median(abs(distance - median) for distance in distances)
    threshold = median + 10 * max(mad, 1e-12)
    candidates = sum(distance > threshold for distance in distances)
    return {"status": "ROBUST_SAMPLE_SCREEN_ONLY", "method": "median radial distance + 10 * MAD",
            "sample_count": len(points), "median_radial_distance": median,
            "median_absolute_deviation": mad, "candidate_threshold": threshold,
            "candidate_count": candidates,
            "note": "Candidates are retained and reported; no points were removed."}


def _write_preview(path: Path, points: list[tuple[float, float, float, int, int, int]], rgb: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    colour_properties = "property uchar red\nproperty uchar green\nproperty uchar blue\n" if rgb else ""
    with path.open("w", encoding="ascii", newline="\n") as stream:
        stream.write("ply\nformat ascii 1.0\n")
        stream.write("comment Exhibition Archive deterministic LiDAR preview; not a reconstruction input\n")
        stream.write(f"element vertex {len(points)}\nproperty float x\nproperty float y\nproperty float z\n{colour_properties}end_header\n")
        for x, y, z, red, green, blue in points:
            suffix = f" {red} {green} {blue}" if rgb else ""
            stream.write(f"{x:.9g} {y:.9g} {z:.9g}{suffix}\n")
