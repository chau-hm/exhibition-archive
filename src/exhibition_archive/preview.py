from __future__ import annotations

import html
import hashlib
import json
import math
import shutil
import struct
import subprocess
from pathlib import Path
from typing import Any

from . import __version__
from .adapters import read_json, sha256_file, write_json
from .backends import validate_gaussian_ply
from .workflow import STAGES, output_path, project_path
from .viewer_orientation import estimate_y_up_transform, map_pose_to_viewer, rotate_vector_xyzw


def _next_first_visual_revision(output_root: Path, execution_id: int) -> tuple[int, Path]:
    revision_id = execution_id
    revision_root = output_root / "first-visual"
    version_dir = revision_root / f"revision-{revision_id:06d}"
    while version_dir.exists():
        revision_id += 1
        version_dir = revision_root / f"revision-{revision_id:06d}"
    return revision_id, version_dir


def build_first_visual_preview(root: Path, config: dict, state, output_dir: Path,
                               execution_id: int, *, update_current_summary: bool = True) -> dict[str, Any]:
    """Build private, versioned Reviewer and Visitor previews for the unreviewed first model."""
    training_stage = state.stage("training") or {}
    training_details = _details(training_stage)
    model_value = training_details.get("model_path") or training_details.get("path")
    if not isinstance(model_value, str) or not model_value:
        raise ValueError("The first-visual preview needs the validated training candidate path.")
    model = Path(model_value).resolve()
    if not model.is_file():
        raise ValueError(f"The first-visual training candidate is missing: {model}")
    identity = validate_gaussian_ply(model)
    revision_id, version_dir = _next_first_visual_revision(output_path(root, config), execution_id)
    diagnostics = version_dir / "diagnostics"
    reviewer = version_dir / "reviewer"
    visitor = version_dir / "visitor"
    for directory in (diagnostics, reviewer, visitor):
        directory.mkdir(parents=True, exist_ok=False)
    assets = visitor / "assets"
    config_dir = visitor / "config"
    vendor = visitor / "vendor"
    js = visitor / "js"
    css = visitor / "css"
    for directory in (assets, config_dir, vendor, js, css):
        directory.mkdir(parents=True, exist_ok=True)
    shutil.copy2(model, assets / "exhibition.ply")

    visitor_cfg, initial_camera = _build_visitor_config(root, config)
    _write_visitor_assets(visitor, config, initial_camera)
    write_json(config_dir / "settings.json", visitor_cfg)
    visitor_metadata = {
        "schema_version": "exhibition-archive.first-visual-preview.v1",
        "profile": "visitor_preview",
        "project_id": config["project_id"],
        "title": config.get("viewer", {}).get("title", config["project_id"]),
        "default_language": "en",
        "release_authorization": "NEEDS_REVIEW",
        "preview_only": True,
        "primary_format": "ply",
        "models": {"ply": {"available": True, "filename": "exhibition.ply",
                            "url": "./assets/exhibition.ply"},
                   "sog": {"available": False, "filename": "exhibition.sog", "url": None}},
        "master": {"filename": "exhibition.ply", "size_bytes": identity["file_bytes"],
                   "sha256": identity["sha256"], "gaussian_count": identity["gaussian_count"],
                   "selection": "unreviewed first-visual candidate"},
        "initial_camera": initial_camera,
        "navigation": {"status": "UNVERIFIED", "constraint_mode": "UNBOUNDED_FREE_XYZ_PREVIEW",
                       "reason": "No accepted floor-aligned enclosure was available when this first-visual preview was built. This private preview is not a public visitor release."},
    }
    write_json(config_dir / "exhibition.json", visitor_metadata)

    sfm_details = _details(state.stage("sfm") or {})
    sfm_directory_value = sfm_details.get("reconstruction_directory")
    sfm_directory = (Path(sfm_directory_value).expanduser() if sfm_directory_value else
                     project_path(root, config, config["integrations"]["sfm"]["directory"])
                     if config.get("integrations", {}).get("sfm", {}).get("directory") else None)
    camera_rows: list[dict[str, Any]] = []
    sparse_points: list[list[float]] = []
    sparse_point_tracks: list[int] = []
    sparse_count = 0
    sparse_preview = diagnostics / "sfm-sparse-points.ply"
    if sfm_directory is not None and sfm_directory.is_dir():
        from .navigation import read_colmap_reconstruction
        reconstruction = read_colmap_reconstruction(sfm_directory)
        camera_rows = reconstruction["cameras"]
        sparse_points = reconstruction["sparse_points"]
        sparse_point_tracks = reconstruction["sparse_point_tracks"]
        sparse_count = len(sparse_points)
        _write_xyz_ply(sparse_preview, sparse_points, maximum=100_000)
        write_json(diagnostics / "camera-poses.json", {
            "coordinate_system": "COLMAP arbitrary non-metric world coordinates",
            "pose_count": len(camera_rows),
            "poses": [{key: row.get(key) for key in
                       ("image_id", "image_name", "camera_id", "qvec_wxyz", "tvec", "camera_center")}
                      for row in camera_rows],
        })

    navigation_details = _details(state.stage("navigation") or {})
    navigation_proposal_doc: dict[str, Any] = {}
    navigation_proposal = _optional_path(navigation_details.get("proposal_path"))
    if navigation_proposal is not None and navigation_proposal.is_file():
        shutil.copy2(navigation_proposal, diagnostics / "navigation-proposal.json")
        navigation_proposal_doc = read_json(navigation_proposal)
    navigation_review = _optional_path(navigation_details.get("review_path"))
    if navigation_review is not None and navigation_review.is_file():
        shutil.copy2(navigation_review, diagnostics / "navigation-review.html")
    lidar_input = output_path(root, config) / "lidar"
    lidar_inspection_file = lidar_input / "inspection.json"
    lidar_report: dict[str, Any] = {}
    if lidar_inspection_file.is_file():
        lidar_report = read_json(lidar_inspection_file)
        selected = lidar_report.get("selected", {})
        if "path" in selected:
            selected["source_path"] = Path(selected.pop("path")).name
        selected.pop("inspection_error", None)
        write_json(diagnostics / "lidar-inspection.json", lidar_report)
    lidar_stage_details = _details(state.stage("lidar_registration") or {})
    alignment_source = _optional_path(lidar_stage_details.get("alignment_report_path")) or (lidar_input / "alignment-report.json")
    aligned_source = _optional_path(lidar_stage_details.get("aligned_preview_path"))
    if aligned_source is None and alignment_source.is_file():
        try:
            aligned_source = _optional_path(read_json(alignment_source).get("aligned_preview_path"))
        except (OSError, ValueError, TypeError):
            aligned_source = None
    diagnostic_sources = [(lidar_input / "lidar-preview.ply", "raw-lidar-preview.ply"),
                          (alignment_source, "lidar-alignment-report.json")]
    if aligned_source is not None:
        diagnostic_sources.append((aligned_source, "aligned-lidar-preview.ply"))
    for source, name in diagnostic_sources:
        if source.is_symlink() or not source.is_file():
            continue
        try:
            source.resolve().relative_to(lidar_input.resolve())
        except ValueError:
            continue
        shutil.copy2(source, diagnostics / name)

    navigation_coverage_summary = None
    if navigation_proposal_doc.get("schema_version") == "exhibition-archive.navigation.v1" and camera_rows:
        coverage_doc = _navigation_coverage_diagnostic(
            navigation_proposal_doc, camera_rows, sparse_points, sparse_point_tracks,
            initial_camera["orientation"]["rotation_quaternion_xyzw"], model)
        write_json(diagnostics / "navigation-coverage.json", coverage_doc)
        (diagnostics / "navigation-coverage.html").write_text(
            render_navigation_coverage_html(coverage_doc), encoding="utf-8")
        navigation_coverage_summary = coverage_doc["summary"]

    discovery_path = output_path(root, config) / "inputs" / "discovery.json"
    input_discovery: dict[str, Any] = {}
    if discovery_path.is_file():
        input_discovery = read_json(discovery_path)
        input_discovery.pop("input_directory", None)
        write_json(diagnostics / "input-discovery.json", input_discovery)

    training_doc: dict[str, Any] = {}
    training_report = _optional_path(training_details.get("training_run_path"))
    training_log = _optional_path(training_details.get("log_path"))
    if training_report is not None and training_report.is_file():
        training_doc = read_json(training_report)
        sanitized = {key: value for key, value in training_doc.items()
                     if key not in {"input_manifest", "training_dataset_workspace", "environment_overrides_private_record"}}
        write_json(diagnostics / "training-run.json", sanitized)
    if training_log is not None and training_log.is_file():
        shutil.copy2(training_log, diagnostics / "training.log")
    sfm_report = output_path(root, config) / "work" / "sfm-summary.json"
    if sfm_report.is_file():
        shutil.copy2(sfm_report, diagnostics / "sfm-summary.json")
    sfm_validation = _optional_path(sfm_details.get("sfm_validation_path"))
    if sfm_validation is not None and sfm_validation.is_file():
        shutil.copy2(sfm_validation, diagnostics / "sfm-validation.json")
    sfm_log = _optional_path(sfm_details.get("log_path"))
    if sfm_log is not None and sfm_log.is_file():
        shutil.copy2(sfm_log, diagnostics / "sfm.log")

    stage_states = state.stages()
    pipeline_summary = {
        "schema_version": "exhibition-archive.first-visual-summary.v1",
        "revision_id": revision_id, "generation_execution_id": execution_id,
        "project_id": config["project_id"], "engine_version": __version__,
        "engine_git_commit": _git_commit(Path(__file__).resolve().parents[2]),
        "source_images": _source_image_stats(root, config),
        "sfm": _public_sfm_summary(sfm_details, camera_rows, sparse_count),
        "lidar": _public_lidar_summary(lidar_report, lidar_input / "alignment-report.json", navigation_details),
        "training": {"status": training_details.get("model_status", "UNREVIEWED_CANDIDATE"),
                     "backend_version": training_details.get("backend_version"),
                     "iterations": training_details.get("training_iterations_requested"),
                     "resolution_divisor": config.get("integrations", {}).get("model", {}).get("resolution_divisor"),
                     "training_resolution": training_details.get("training_resolution", training_doc.get("training_resolution", {})),
                     "quality_preset": config.get("integrations", {}).get("model", {}).get("quality", "Spirula default; effective value recorded in command.json"),
                     "effective_configuration_status": training_doc.get("effective_training_configuration", {}).get("status", "NOT_RECORDED"),
                     "effective_configuration_sha256": hashlib.sha256(json.dumps(
                         training_doc.get("effective_training_configuration", {}), sort_keys=True,
                         separators=(",", ":")).encode("utf-8")).hexdigest(),
                     "effective_parameter_count": training_doc.get("effective_training_configuration", {}).get("effective_parameter_count"),
                     "model_format": "3DGS_GAUSSIAN_PLY", "model_size_bytes": identity["file_bytes"],
                     "model_sha256": identity["sha256"], "gaussian_count": identity["gaussian_count"],
                     "log_available": (diagnostics / "training.log").is_file(),
                     "checkpoint_directory": "../backend-runs/training/ (retained by Engine)"},
        "navigation": {"status": navigation_details.get("status", "UNVERIFIED"),
                       "review_required": True,
                       "reason": navigation_details.get("reason", "Generated navigation remains a proposal until reviewed.")},
        "navigation_coverage": navigation_coverage_summary,
        "viewer_orientation": initial_camera["orientation"],
        "pipeline_stages": {stage["id"]: (stage_states.get(stage["id"]) or {}).get("status", "PENDING")
                            for stage in STAGES},
        "warnings": _preview_warnings(sfm_details, lidar_report, navigation_details,
                                      initial_camera["orientation"]),
        "reviewer_html": "reviewer/index.html", "visitor_html": "visitor/index.html",
        "release_authorization": "NEEDS_REVIEW", "first_visual_only": True,
    }
    pipeline_summary["pipeline_stages"]["first_visual"] = "COMPLETED"
    pipeline_summary["diagnostic_files"] = [path.name for path in sorted(diagnostics.iterdir()) if path.is_file()]
    write_json(version_dir / "pipeline-status.json", pipeline_summary)
    write_json(version_dir / "manifest.json", {
        "schema_version": "exhibition-archive.first-visual-manifest.v1",
        "revision_id": revision_id, "generation_execution_id": execution_id,
        "project_id": config["project_id"], "engine_version": __version__,
        "engine_git_commit": pipeline_summary["engine_git_commit"],
        "source_photo_directory": config["source"]["photo_directory"],
        "source_is_read_only": True,
        "source_photographs": [{"relative_path": row["relative_path"], "sha256": row["sha256"],
                                 "size_bytes": row["size_bytes"]}
                                for row in _source_image_rows(root, config)],
        "source_point_clouds": [{key: row.get(key) for key in
                                  ("relative_path", "sha256", "size_bytes", "classification", "format")}
                                 for row in input_discovery.get("ply_files", [])],
        "stage_configurations": {
            "sfm": config.get("integrations", {}).get("sfm", {}),
            "training": {key: value for key, value in config.get("integrations", {}).get("model", {}).items()
                         if key != "environment_overrides"},
            "lidar": config.get("integrations", {}).get("lidar", {}),
            "navigation": config.get("integrations", {}).get("navigation", {}),
        },
        "training_backend_version": training_details.get("backend_version"),
        "effective_training_configuration": training_doc.get("effective_training_configuration"),
        "candidate_model": {key: identity[key] for key in ("path", "sha256", "file_bytes", "gaussian_count")},
        "diagnostic_artifacts": [{"path": f"diagnostics/{path.name}", "sha256": sha256_file(path),
                                  "size_bytes": path.stat().st_size}
                                 for path in sorted(diagnostics.iterdir()) if path.is_file()],
        "lidar": pipeline_summary["lidar"],
        "release_authorization": "NEEDS_REVIEW",
    })
    reviewer_html = reviewer / "index.html"
    reviewer_html.write_text(_reviewer_html(pipeline_summary), encoding="utf-8")
    report_path = (output_path(root, config) / "reports" / "first-visual-summary.md"
                   if update_current_summary else version_dir / "first-visual-summary.md")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(_summary_markdown(pipeline_summary), encoding="utf-8")
    failures = _validate_preview(version_dir, identity)
    if failures:
        raise ValueError("First-visual HTML/assets failed local validation: " + "; ".join(failures))
    artifacts = [reviewer_html, visitor / "index.html", version_dir / "manifest.json",
                 version_dir / "pipeline-status.json", report_path, assets / "exhibition.ply"]
    artifacts.extend(path for path in diagnostics.rglob("*") if path.is_file())
    return {"status": "COMPLETED", "artifacts": artifacts,
            "details": {"status": "AWAITING_VISUAL_REVIEW", "version_directory": str(version_dir),
                        "revision_id": revision_id, "generation_execution_id": execution_id,
                        "reviewer_html": str(reviewer_html), "visitor_html": str(visitor / "index.html"),
                        "summary_path": str(report_path), "validation": {"status": "PASS", "failures": []},
                        "model_sha256": identity["sha256"], "navigation_status": pipeline_summary["navigation"]["status"],
                        "release_authorization": "NEEDS_REVIEW"}}


def _build_visitor_config(root: Path, config: dict) -> tuple[dict, dict]:
    camera_rows = []
    # The validated SfM summary names the selected reconstruction. The preview
    # camera is selected from its registered poses, never from scene semantics.
    sfm_report = output_path(root, config) / "work" / "sfm-summary.json"
    reconstruction_value = read_json(sfm_report).get("reconstruction_directory") if sfm_report.is_file() else None
    reconstruction = (Path(reconstruction_value).expanduser() if reconstruction_value else
                      project_path(root, config, config["integrations"]["sfm"]["directory"])
                      if config.get("integrations", {}).get("sfm", {}).get("directory") else None)
    if reconstruction is not None and reconstruction.is_dir():
        from .navigation import read_colmap_camera_poses, quaternion_wxyz_to_rotation
        camera_rows = read_colmap_camera_poses(reconstruction)
        if camera_rows:
            center = [statistics_median([row["camera_center"][axis] for row in camera_rows]) for axis in range(3)]
            station = min(camera_rows, key=lambda row: math.dist(row["camera_center"], center))
            rotation = quaternion_wxyz_to_rotation(station["qvec_wxyz"])
            forward = [rotation[2][axis] for axis in range(3)]
            norm = math.sqrt(sum(value * value for value in forward)) or 1.0
            forward = [value / norm for value in forward]
            position = station["camera_center"]
            target = [position[i] + forward[i] * 2 for i in range(3)]
            camera_source = {"image_name": station["image_name"], "selection": "registered camera nearest the camera-center median"}
        else:
            position, target = [0, 0, 3], [0, 0, 0]
            camera_source = {"selection": "viewer reset-camera fallback; no registered camera row"}
    else:
        position, target = [0, 0, 3], [0, 0, 0]
        camera_source = {"selection": "viewer reset-camera fallback; no reconstruction path"}
    pose_colmap = {"position": [float(x) for x in position],
                   "target": [float(x) for x in target], "fov": 70.75}
    orientation = estimate_y_up_transform(camera_rows)
    pose = map_pose_to_viewer(pose_colmap, orientation["rotation_quaternion_xyzw"])
    coordinate_frame = (
        "COLMAP world rotated to PlayCanvas Y-up using registered-camera up consensus"
        if orientation["applied"] else
        "COLMAP world with identity transform; PlayCanvas Y-up fallback requires orientation review"
    )
    initial_camera = {
        "coordinate_frame": coordinate_frame,
        "source": camera_source,
        "pose_colmap": pose_colmap,
        "pose_supersplat": pose,
        "model_scene_transform_supersplat": {
            "translation": [0, 0, 0],
            "rotationQuaternionXYZW": orientation["rotation_quaternion_xyzw"],
            "uniformScale": 1.0,
        },
        "orientation": orientation,
        "navigation_status": "UNVERIFIED",
    }
    settings = {"version": 2, "tonemapping": "none", "highPrecisionRendering": False,
                "background": {"color": [0.012, 0.016, 0.013]},
                "postEffectSettings": {"sharpness": {"enabled": False, "amount": 0},
                                       "bloom": {"enabled": False, "intensity": 0.1, "blurLevel": 2},
                                       "grading": {"enabled": False, "brightness": 1, "contrast": 1,
                                                   "saturation": 1, "tint": [1, 1, 1]},
                                       "vignette": {"enabled": False, "intensity": 0.5, "inner": 0.3,
                                                    "outer": 0.75, "curvature": 1},
                                       "fringing": {"enabled": False, "intensity": 0.5}},
                "animTracks": [], "cameras": [{"initial": pose}], "annotations": [], "startMode": "default"}
    return settings, initial_camera


def _write_visitor_assets(visitor: Path, config: dict, initial_camera: dict) -> None:
    template = Path(__file__).resolve().parents[2] / "templates" / "visitor"
    vendor_source = template / "vendor"
    required = ("supersplat-viewer.css", "supersplat-viewer.js", "SUPERSPLAT-VIEWER-LICENSE.txt",
                "PLAYCANVAS-LICENSE.txt", "THIRD_PARTY-NOTICES.md", "runtime.json")
    missing = [name for name in required if not (vendor_source / name).is_file()]
    if not (vendor_source / "visitor-app.js").is_file():
        missing.append("visitor-app.js")
    if missing:
        raise FileNotFoundError(f"Bundled visitor runtime is incomplete: {missing}")
    html_text = (template / "index.html").read_text(encoding="utf-8")
    title = html.escape(str(config.get("viewer", {}).get("title", config["project_id"])), quote=True)
    html_text = html_text.replace("__EXHIBITION_TITLE__", title)
    banner = '<div class="preview-status" role="status">First visual preview · Navigation unverified · Not approved for public release</div>\n    '
    html_text = html_text.replace('<div id="viewer"', banner + '<div id="viewer"')
    (visitor / "index.html").write_text(html_text, encoding="utf-8")
    shutil.copy2(template / "style.css", visitor / "css" / "viewer.css")
    css_path = visitor / "css" / "viewer.css"
    with css_path.open("a", encoding="utf-8") as css:
        css.write("\n.preview-status{position:absolute;z-index:10;top:5rem;left:50%;transform:translateX(-50%);max-width:calc(100vw - 2rem);padding:.55rem .8rem;border:1px solid #b99758;border-radius:.55rem;background:#211e18ed;color:#f2ddb0;font:12px/1.3 system-ui,sans-serif;text-align:center;pointer-events:none}\n")
    shutil.copy2(template / "locales.json", visitor / "config" / "locales.json")
    for name in required:
        shutil.copy2(vendor_source / name, visitor / "vendor" / name)
    visitor_js = (template / "app.js").read_text(encoding="utf-8")
    visitor_js = visitor_js.replace("__EXHIBITION_ARCHIVE_VISITOR_ACCEPTANCE__", "false")
    if "__EXHIBITION_ARCHIVE_VISITOR_ACCEPTANCE__" in visitor_js:
        raise ValueError("Visitor preview retained an unresolved acceptance-build marker")
    (visitor / "js" / "viewer.js").write_text(visitor_js, encoding="utf-8")
    (visitor / "config" / "settings.json").touch(exist_ok=True)


def _write_xyz_ply(path: Path, points: list[list[float]], maximum: int) -> None:
    stride = max(1, math.ceil(len(points) / maximum))
    selected = [point for index, point in enumerate(points) if index % stride == 0]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="ascii", newline="\n") as stream:
        stream.write("ply\nformat ascii 1.0\ncomment Deterministic COLMAP sparse geometry preview\n")
        stream.write(f"element vertex {len(selected)}\nproperty float x\nproperty float y\nproperty float z\nend_header\n")
        for point in selected:
            stream.write("%.9g %.9g %.9g\n" % tuple(point[:3]))


def _gaussian_xyz_sample(path: Path, maximum: int = 20_000) -> dict[str, Any]:
    """Read exact model extents and a deterministic XYZ sample without decoding Gaussian attributes."""
    from .backends import PLY_TYPES
    from .lidar import _format_from_header, _read_header

    header, error = _read_header(path)
    if header is None:
        raise ValueError(error or "Could not read Gaussian PLY header")
    vertex = next((element for element in header["elements"] if element["name"] == "vertex"), None)
    if vertex is None or header["elements"].index(vertex) != 0:
        raise ValueError("Gaussian PLY must begin with a vertex element")
    properties = vertex["properties"]
    if any(row.get("list") for row in properties):
        raise ValueError("Gaussian PLY vertex has list-valued properties")
    indices = {row["name"]: index for index, row in enumerate(properties)}
    if not {"x", "y", "z"}.issubset(indices):
        raise ValueError("Gaussian PLY is missing XYZ properties")
    record_bytes = sum(PLY_TYPES[row["type"]][1] for row in properties)
    offsets, offset = {}, 0
    for row in properties:
        offsets[row["name"]] = (offset, PLY_TYPES[row["type"]][0])
        offset += PLY_TYPES[row["type"]][1]
    fmt = _format_from_header(header)
    if fmt not in {"binary_little_endian", "binary_big_endian", "ascii"}:
        raise ValueError(f"Unsupported Gaussian PLY format {fmt}")
    endian = ">" if fmt == "binary_big_endian" else "<"
    count = int(vertex["count"])
    stride = max(1, math.ceil(count / max(1, maximum)))
    samples: list[list[float]] = []
    minimum = [math.inf, math.inf, math.inf]
    maximum_xyz = [-math.inf, -math.inf, -math.inf]
    with path.open("rb") as stream:
        stream.seek(header["body_offset"])
        if fmt == "ascii":
            for index in range(count):
                raw = stream.readline()
                values = raw.split()
                if len(values) != len(properties):
                    raise ValueError(f"Malformed Gaussian PLY row {index}")
                xyz = [float(values[indices[name]]) for name in ("x", "y", "z")]
                if not all(math.isfinite(value) for value in xyz):
                    continue
                for axis, value in enumerate(xyz):
                    minimum[axis] = min(minimum[axis], value)
                    maximum_xyz[axis] = max(maximum_xyz[axis], value)
                if index % stride == 0 or index == count - 1:
                    samples.append(xyz)
        else:
            chunk_records = max(1, (4 * 1024 * 1024) // record_bytes)
            base = 0
            while base < count:
                chunk_count = min(chunk_records, count - base)
                raw = stream.read(chunk_count * record_bytes)
                if len(raw) != chunk_count * record_bytes:
                    raise ValueError(f"Truncated Gaussian PLY at vertex {base}")
                for local_index in range(chunk_count):
                    row_offset = local_index * record_bytes
                    xyz = [struct.unpack_from(endian + offsets[name][1], raw,
                                              row_offset + offsets[name][0])[0]
                           for name in ("x", "y", "z")]
                    if not all(math.isfinite(value) for value in xyz):
                        continue
                    for axis, value in enumerate(xyz):
                        minimum[axis] = min(minimum[axis], value)
                        maximum_xyz[axis] = max(maximum_xyz[axis], value)
                    index = base + local_index
                    if index % stride == 0 or index == count - 1:
                        samples.append(xyz)
                base += chunk_count
    if not samples or not all(math.isfinite(value) for value in (*minimum, *maximum_xyz)):
        raise ValueError("Gaussian PLY has no finite sampled geometry")
    return {"vertex_count": count, "sample_count": len(samples),
            "raw_exact_bounds": {"minimum": minimum, "maximum": maximum_xyz},
            "sampled_points": samples}


def _navigation_coverage_diagnostic(artifact: dict[str, Any], camera_rows: list[dict[str, Any]],
                                   sparse_points: list[list[float]], sparse_tracks: list[int],
                                   model_rotation: list[float], model_path: Path) -> dict[str, Any]:
    from .navigation import _convex_hull, _signed_area

    def q(values: list[float], fraction: float) -> float:
        ordered = sorted(values)
        if not ordered:
            return 0.0
        position = (len(ordered) - 1) * fraction
        low, high = math.floor(position), math.ceil(position)
        return ordered[low] + (ordered[high] - ordered[low]) * (position - low)

    def dot(a, b):
        return sum(x * y for x, y in zip(a, b))

    def add_scaled(origin, axis, factor):
        return [origin[index] + axis[index] * factor for index in range(3)]

    frame = artifact["coordinate_frame"]
    local = frame["viewer_to_navigation"]
    origin, axis_u, axis_v, axis_up = (local["origin_viewer"], local["u_viewer"],
                                      local["v_viewer"], local["up_viewer"])
    coverage = artifact.get("coverage_diagnostics", {})
    trajectory = coverage.get("camera_trajectory", [])
    spacing_by_component = {row["component_id"]: row["median_nearest_neighbor_spacing_units"]
                            for row in coverage.get("component_footprints", [])}
    cameras = []
    for row in trajectory:
        center = row.get("viewer_center")
        forward = row.get("viewer_forward")
        if not (isinstance(center, list) and isinstance(forward, list)):
            continue
        length = max(0.0, float(spacing_by_component.get(row.get("component_id"), 0.0)) * 0.7)
        cameras.append({**{key: row.get(key) for key in
                           ("station_id", "image_name", "component_id", "coverage_status", "pose_trust")},
                        "center": center,
                        "forward_end": [center[axis] + forward[axis] * length for axis in range(3)]})
    sparse_stride = max(1, math.ceil(len(sparse_points) / 12_000))
    strong_points, weak_points = [], []
    for index in range(0, min(len(sparse_points), len(sparse_tracks)), sparse_stride):
        transformed = rotate_vector_xyzw(model_rotation, sparse_points[index])
        record = [*transformed, int(sparse_tracks[index])]
        (strong_points if sparse_tracks[index] >= 3 else weak_points).append(record)
    model_geometry = _gaussian_xyz_sample(model_path)
    sampled_viewer = [rotate_vector_xyzw(model_rotation, point)
                      for point in model_geometry["sampled_points"]]
    robust_min = [q([point[axis] for point in sampled_viewer], 0.01) for axis in range(3)]
    robust_max = [q([point[axis] for point in sampled_viewer], 0.99) for axis in range(3)]
    raw_min, raw_max = model_geometry["raw_exact_bounds"]["minimum"], model_geometry["raw_exact_bounds"]["maximum"]
    raw_corners = [[raw_max[0] if bits & 1 else raw_min[0],
                    raw_max[1] if bits & 2 else raw_min[1],
                    raw_max[2] if bits & 4 else raw_min[2]] for bits in range(8)]
    model_outer_corners = [rotate_vector_xyzw(model_rotation, point) for point in raw_corners]
    robust_corners = [[robust_max[0] if bits & 1 else robust_min[0],
                       robust_max[1] if bits & 2 else robust_min[1],
                       robust_max[2] if bits & 4 else robust_min[2]] for bits in range(8)]
    projected_model = [[dot([point[axis] - origin[axis] for axis in range(3)], axis_u),
                        dot([point[axis] - origin[axis] for axis in range(3)], axis_v)]
                       for point in sampled_viewer]
    model_u = [point[0] for point in projected_model]
    model_v = [point[1] for point in projected_model]
    model_area = max(0.0, q(model_u, 0.99) - q(model_u, 0.01)) * max(0.0, q(model_v, 0.99) - q(model_v, 0.01))

    active_regions = []
    for region in artifact.get("regions", []):
        limits = region.get("exploration_limits") or {}
        minimum = (limits.get("minimum_height") or {}).get("value")
        maximum = (limits.get("maximum_height") or {}).get("value")
        polygon = region.get("polygon_uv", [])
        lower = ([add_scaled(add_scaled(add_scaled(origin, axis_u, point[0]), axis_v, point[1]),
                             axis_up, float(minimum)) for point in polygon]
                 if minimum is not None else [])
        upper = [add_scaled(point, axis_up, float(maximum) - float(minimum))
                 for point in lower] if minimum is not None and maximum is not None else []
        ground = (region.get("geometry_candidates", {}).get("ground_plane") or {})
        ground_loop = []
        if ground.get("status") == "PROPOSED":
            normal = ground["normal"]
            offset = float(ground["offset"])
            denominator = dot(normal, axis_up)
            if abs(denominator) > 1e-6:
                for point in polygon:
                    base_point = add_scaled(add_scaled(origin, axis_u, point[0]), axis_v, point[1])
                    height = -(offset + dot(normal, base_point)) / denominator
                    ground_loop.append(add_scaled(base_point, axis_up, height))
        hull_area = abs(_signed_area(polygon))
        local_support = region.get("evidence", {}).get("sparse_geometry_observation", {}).get(
            "points_with_track_length_minimum_inside_candidate", 0)
        ground_evidence = ground.get("evidence", {})
        active_regions.append({"region_id": region["id"], "polygon_uv": polygon,
                               "lower_loop": lower, "upper_loop": upper, "ground_loop": ground_loop,
                               "horizontal_area_units2": hull_area,
                               "camera_count": region.get("evidence", {}).get("candidate_station_count", 0),
                               "local_multiview_point_count": local_support,
                               "ground_candidate_inlier_count": ground_evidence.get("inlier_count", 0),
                               "ground_candidate_coverage_fraction": ground_evidence.get("footprint_grid_occupied_fraction"),
                               "ground_candidate_residual_p90_units": ground_evidence.get("residual_p90_units"),
                               "ground_candidate_tilt_degrees": ground_evidence.get("tilt_from_view_up_degrees"),
                               "minimum_height": minimum, "maximum_height": maximum,
                               "upper_limit_is_physical_ceiling": False})
    excluded_regions = []
    for component in coverage.get("excluded_camera_components", []):
        members = [row for row in trajectory if row.get("component_id") and
                   row.get("station_id") in component.get("station_ids", [])]
        heights = [row["navigation_local_uvh"][2] for row in members]
        level = q(heights, 0.5) if heights else 0.0
        poly = component.get("polygon_uv", [])
        outline = [add_scaled(add_scaled(origin, axis_u, point[0]), axis_v, point[1])
                   for point in poly]
        outline = [add_scaled(point, axis_up, level) for point in outline]
        excluded_regions.append({"component_id": component_for_id(members),
                                 "outline": outline, "camera_count": len(component.get("station_ids", [])),
                                 "reason": component.get("reason")})

    camera_area = sum(region["horizontal_area_units2"] for region in active_regions)
    track_ge_3 = sum(1 for value in sparse_tracks if value >= 3)
    local_track_ge_3 = sum(region["local_multiview_point_count"] for region in active_regions)
    summary = {"status": artifact.get("status"),
               "status_summary": (f"{len(active_regions)} camera-supported region proposed; "
                                  f"{coverage.get('camera_used_count', 0)} of {coverage.get('camera_candidate_count', 0)} camera poses included; "
                                  f"{coverage.get('camera_excluded_count', 0)} camera poses excluded or unassigned. "
                                  f"Ground candidate and virtual vertical limits require review."),
               "registered_camera_count": len(camera_rows),
               "camera_candidate_count": coverage.get("camera_candidate_count", 0),
               "camera_used_count": coverage.get("camera_used_count", 0),
               "camera_excluded_count": coverage.get("camera_excluded_count", 0),
               "camera_component_count": coverage.get("camera_component_count", 0),
               "component_footprints": coverage.get("component_footprints", []),
               "candidate_region_count": len(active_regions),
               "candidate_camera_footprint_area_units2": camera_area,
               "gaussian_robust_footprint_area_units2": model_area,
               "camera_to_gaussian_robust_area_fraction": camera_area / model_area if model_area > 0 else None,
               "sparse_point_count": len(sparse_points), "sparse_track_length_ge_3_count": track_ge_3,
               "local_track_length_ge_3_count": local_track_ge_3,
               "local_track_length_ge_3_fraction": local_track_ge_3 / track_ge_3 if track_ge_3 else 0.0,
               "lidar_consumed": artifact.get("coverage_assumptions", {}).get("aligned_lidar_used", False),
               "navigation_up_status": (artifact.get("coordinate_frame", {}).get("navigation_up_evidence") or {}).get("status", "NOT_AVAILABLE"),
               "navigation_up_interpretation": "Camera orientation proposal; not physical gravity.",
               "human_review_required": True}
    return {"summary": summary, "navigation_basis": {"origin": origin, "u": axis_u, "v": axis_v, "up": axis_up},
            "cameras": cameras, "supported_sparse_points": strong_points, "weak_sparse_points": weak_points,
            "regions": active_regions, "excluded_components": excluded_regions,
            "gaussian_bounds": {"raw_exact_minimum": raw_min, "raw_exact_maximum": raw_max,
                                "outer_oriented_box_corners_viewer": model_outer_corners,
                                "sampled_1_99_aabb_viewer": {"minimum": robust_min, "maximum": robust_max},
                                "sampled_points": model_geometry["sample_count"], "vertex_count": model_geometry["vertex_count"]},
            "limitations": ["Camera centers establish capture coverage only; they do not prove every point inside the footprint is traversable.",
                            "The excluded camera component has too little local multi-view SfM support for a bounded vertical envelope.",
                            "The Gaussian model bounds include distant background; the proposed camera boundary deliberately does not expand to those bounds.",
                            "The lower plane is a photo-SfM candidate requiring review. The upper value is a virtual exploration limit, not a ceiling.",
                            "The camera-up transform provides viewer orientation evidence only and is not a gravity measurement."]}


def component_for_id(members: list[dict[str, Any]]) -> str | None:
    return members[0].get("component_id") if members else None


def render_navigation_coverage_html(document: dict[str, Any]) -> str:
    payload = json.dumps(document, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    page = r'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="dark"><title>Navigation coverage diagnostic</title>
<style>*{box-sizing:border-box}body{margin:0;background:#101511;color:#e8eee7;font:14px/1.45 system-ui,sans-serif}header{padding:12px 16px;background:#1a241d;border-bottom:1px solid #39443b}h1{font-size:18px;margin:0 0 4px}p{margin:4px 0;color:#b5c0b3}.layout{display:grid;grid-template-columns:minmax(0,1fr) 260px;min-height:680px;height:calc(100vh - 75px)}canvas{width:100%;height:100%;background:#0b100d;touch-action:none}.side{padding:12px;overflow:auto;background:#151d17;border-left:1px solid #39443b}.side label{display:block;margin:8px 0}.side input{margin-right:7px}.metric{display:flex;justify-content:space-between;border-bottom:1px solid #344037;padding:5px 0;gap:12px}.metric b{text-align:right}.note{color:#d9bd76;font-size:12px;margin-top:12px}button{background:#324c3a;border:1px solid #536b58;color:#e8eee7;border-radius:4px;padding:5px 8px;margin:3px;cursor:pointer}.legend{font-size:12px;color:#bcc8ba}@media(max-width:780px){.layout{grid-template-columns:1fr;height:auto}.side{border:0;border-top:1px solid #39443b}canvas{height:62vh;min-height:420px}}</style>
<header><h1>Camera coverage and navigation proposal</h1><p>Drag to orbit · scroll to zoom. Coordinates use the corrected Viewer frame. A proposal is not an approval.</p></header><div class="layout"><canvas id="scene"></canvas><aside class="side"><div id="summary"></div><hr><b>Layers</b><label><input type="checkbox" data-layer="camera" checked>Camera positions and viewing directions</label><label><input type="checkbox" data-layer="strong" checked>Tracked SfM points (track ≥ 3)</label><label><input type="checkbox" data-layer="weak">Weakly tracked SfM points</label><label><input type="checkbox" data-layer="boundary" checked>Candidate exploration envelope</label><label><input type="checkbox" data-layer="ground" checked>Photo-SfM lower plane candidate</label><label><input type="checkbox" data-layer="excluded" checked>Excluded camera component</label><label><input type="checkbox" data-layer="model" checked>Gaussian model bounds</label><label><input type="checkbox" data-layer="axes" checked>Navigation axes</label><p class="legend">Blue: camera poses in proposal · orange: excluded or unassigned · green: multi-view points / lower-plane candidate · magenta: exploration envelope · grey: model extent.</p><div id="regionStats"></div><p class="note">Viewer-up is derived from camera orientations. The plane is a photo-SfM ground candidate and requires review. The upper bound is a virtual exploration limit, not a physical ceiling. Distant model background is visible as bounds only and does not define the navigation perimeter. LiDAR is not consumed.</p></aside></div>
<script>const d=DATA;const canvas=document.querySelector('#scene'),ctx=canvas.getContext('2d'),layers=Object.fromEntries([...document.querySelectorAll('[data-layer]')].map(x=>[x.dataset.layer,x]));let yaw=.7,pitch=.48,zoom=1,drag=null,viewWidth=1,viewHeight=1;
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}function metric(k,v){return `<div class="metric"><span>${esc(k)}</span><b>${esc(v)}</b></div>`}const s=d.summary,fmt=(n,digits=2)=>Number.isFinite(n)?n.toFixed(digits):'n/a';document.querySelector('#summary').innerHTML=metric('Proposal',s.status)+metric('Registered cameras',`${s.camera_used_count} / ${s.registered_camera_count}`)+metric('Camera components',s.camera_component_count)+metric('Candidate regions',s.candidate_region_count)+metric('Camera footprint',`${fmt(s.candidate_camera_footprint_area_units2)} scene units²`)+metric('Gaussian q1–q99 footprint',`${fmt(s.gaussian_robust_footprint_area_units2)} scene units²`)+metric('Camera / model area',`${fmt((s.camera_to_gaussian_robust_area_fraction??0)*100,1)}%`)+metric('Local track ≥3 points',`${s.local_track_length_ge_3_count} / ${s.sparse_track_length_ge_3_count} (${fmt((s.local_track_length_ge_3_fraction??0)*100,1)}%)`)+metric('LiDAR consumed',String(s.lidar_consumed))+metric('Camera-up',s.navigation_up_status);document.querySelector('#regionStats').innerHTML='<hr><b>Candidate regions</b>'+d.regions.map(r=>`<p>${esc(r.region_id)} · ${r.camera_count} cameras · ${fmt(r.horizontal_area_units2)} units²<br>Ground candidate: ${r.ground_candidate_inlier_count} inliers, ${fmt((r.ground_candidate_coverage_fraction??0)*100,0)}% grid coverage<br>Virtual height: ${fmt(r.minimum_height)} to ${fmt(r.maximum_height)} units</p>`).join('')+'<hr><b>Camera components</b>'+s.component_footprints.map(c=>`<p>${esc(c.component_id)} · ${c.camera_count} cameras · ${fmt(c.convex_hull_area_square_units)} units² · ${esc(c.proposal_status)}</p>`).join('');
const all=[...d.cameras.map(x=>x.center),...d.supported_sparse_points.map(x=>x.slice(0,3)),...d.regions.flatMap(r=>[...r.lower_loop,...r.upper_loop]),...d.gaussian_bounds.outer_oriented_box_corners_viewer];let min=[0,0,0],max=[0,0,0];if(all.length){min=[0,1,2].map(a=>Math.min(...all.map(p=>p[a])));max=[0,1,2].map(a=>Math.max(...all.map(p=>p[a])))}const center=min.map((x,i)=>(x+max[i])/2),span=Math.max(...max.map((x,i)=>x-min[i]),1e-4);function project(p){let x=p[0]-center[0],y=p[1]-center[1],z=p[2]-center[2],cx=Math.cos(yaw),sx=Math.sin(yaw),cp=Math.cos(pitch),sp=Math.sin(pitch),xx=cx*x+sx*z,zz=-sx*x+cx*z,yy=cp*y-sp*zz;let scale=Math.min(viewWidth,viewHeight)*.8/span*zoom;return [viewWidth/2+xx*scale,viewHeight/2-yy*scale,zz]}function line(a,b,color,width=1){let p=project(a),q=project(b);ctx.strokeStyle=color;ctx.lineWidth=width;ctx.beginPath();ctx.moveTo(p[0],p[1]);ctx.lineTo(q[0],q[1]);ctx.stroke()}function loop(points,color,width=1,close=true){for(let i=0;i<points.length-(close?0:1);i++)line(points[i],points[(i+1)%points.length],color,width)}function box(corners,color){for(let i=0;i<8;i++)for(let bit of [1,2,4])if(!(i&bit))line(corners[i],corners[i|bit],color,1)}
function render(){let rect=canvas.getBoundingClientRect(),ratio=devicePixelRatio||1;viewWidth=rect.width;viewHeight=rect.height;canvas.width=Math.round(rect.width*ratio);canvas.height=Math.round(rect.height*ratio);ctx.setTransform(ratio,0,0,ratio,0,0);if(layers.axes.checked){let o=d.navigation_basis.origin,scale=span*.12;line(o,[o[0]+d.navigation_basis.u[0]*scale,o[1]+d.navigation_basis.u[1]*scale,o[2]+d.navigation_basis.u[2]*scale],'#57b7ff',2);line(o,[o[0]+d.navigation_basis.v[0]*scale,o[1]+d.navigation_basis.v[1]*scale,o[2]+d.navigation_basis.v[2]*scale],'#dfbd5b',2);line(o,[o[0]+d.navigation_basis.up[0]*scale,o[1]+d.navigation_basis.up[1]*scale,o[2]+d.navigation_basis.up[2]*scale],'#69df8a',2)}
if(layers.model.checked){box(d.gaussian_bounds.outer_oriented_box_corners_viewer,'#718079');let lo=d.gaussian_bounds.sampled_1_99_aabb_viewer.minimum,hi=d.gaussian_bounds.sampled_1_99_aabb_viewer.maximum,cs=[...Array(8)].map((_,i)=>[(i&1)?hi[0]:lo[0],(i&2)?hi[1]:lo[1],(i&4)?hi[2]:lo[2]]);box(cs,'#a0aaa0')}
if(layers.ground.checked)for(let r of d.regions)if(r.ground_loop.length)loop(r.ground_loop,'#58d486',2);
if(layers.boundary.checked)for(let r of d.regions){loop(r.lower_loop,'#ed62cc',2);loop(r.upper_loop,'#ed62cc',1);for(let i=0;i<r.lower_loop.length;i++)line(r.lower_loop[i],r.upper_loop[i],'#ed62cc',1)}
if(layers.excluded.checked)for(let r of d.excluded_components)loop(r.outline,'#ff9d62',2);
function points(rows,color,radius){for(let p of rows){let q=project(p),size=Math.max(1,radius);ctx.fillStyle=color;ctx.beginPath();ctx.arc(q[0],q[1],size,0,Math.PI*2);ctx.fill()}}if(layers.strong.checked)points(d.supported_sparse_points,'#50bf76',1.2);if(layers.weak.checked)points(d.weak_sparse_points,'#6b7770',1);if(layers.camera.checked){for(let c of d.cameras){let color=c.coverage_status==='INCLUDED_PROPOSAL'?'#6fcaff':'#ff9d62';line(c.center,c.forward_end,color,1);points([c.center],color,3)}}}
for(let checkbox of Object.values(layers))checkbox.onchange=render;canvas.addEventListener('pointerdown',e=>{drag=[e.clientX,e.clientY];canvas.setPointerCapture(e.pointerId)});canvas.addEventListener('pointermove',e=>{if(!drag)return;let dx=e.clientX-drag[0],dy=e.clientY-drag[1];yaw+=dx*.008;pitch=Math.max(-1.45,Math.min(1.45,pitch+dy*.008));drag=[e.clientX,e.clientY];render()});canvas.addEventListener('pointerup',()=>drag=null);canvas.addEventListener('wheel',e=>{e.preventDefault();zoom=Math.max(.15,Math.min(8,zoom*Math.exp(-e.deltaY*.001)));render()},{passive:false});new ResizeObserver(render).observe(canvas);render();</script></html>'''
    return page.replace("DATA", payload)


def _reviewer_html(summary: dict[str, Any]) -> str:
    warning_items = "".join(f"<li>{html.escape(str(item))}</li>" for item in summary["warnings"])
    diagnostics = []
    labels = {"sfm-sparse-points.ply": "COLMAP sparse points preview",
              "input-discovery.json": "Input discovery manifest",
              "camera-poses.json": "Camera poses", "raw-lidar-preview.ply": "Raw LiDAR preview",
              "aligned-lidar-preview.ply": "Aligned LiDAR preview",
              "lidar-alignment-report.json": "LiDAR alignment report",
              "lidar-inspection.json": "LiDAR inspection report",
              "navigation-proposal.json": "Navigation proposal",
              "navigation-review.html": "Navigation review page",
              "navigation-coverage.html": "Interactive 3D camera, geometry, and boundary overlay",
              "navigation-coverage.json": "Camera and geometry coverage measurements",
              "sfm-summary.json": "SfM metrics", "sfm-validation.json": "SfM validation",
              "sfm.log": "SfM log", "training.log": "Training log",
              "training-run.json": "Training run settings"}
    for filename in summary.get("diagnostic_files", []):
        diagnostics.append(f'<a href="../diagnostics/{html.escape(filename, quote=True)}">{html.escape(labels.get(filename, filename))}</a>')
    sources = summary["source_images"]
    sfm = summary["sfm"]
    lidar = summary["lidar"]
    training = summary["training"]
    viewer_orientation = summary.get("viewer_orientation", {})
    coverage = summary.get("navigation_coverage") or {}
    coverage_panel = (f"<section><h2>Navigation coverage proposal</h2><p>{html.escape(str(coverage.get('status_summary', 'No coverage proposal is available.')))}</p><iframe title=\"Interactive navigation coverage overlay\" src=\"../diagnostics/navigation-coverage.html\"></iframe><p class=\"muted\">Camera-up sets the display frame only. The photo-SfM lower-plane candidate and upper exploration limit require review; the upper limit is not a ceiling. The preview remains unbounded until navigation is approved.</p></section>"
                      if coverage else "")
    orientation_status = viewer_orientation.get("status", "ORIENTATION_REVIEW_REQUIRED")
    orientation_label = {
        "CAMERA_UP_PROPOSAL_APPLIED_REVIEW_REQUIRED": "Proposal applied; review required",
        "ORIENTATION_REVIEW_REQUIRED": "Review required; identity fallback",
    }.get(orientation_status, str(orientation_status).replace("_", " ").title())
    training_resolution = training.get("training_resolution", {})
    observed_size = training_resolution.get("backend_reported_training_resolution")
    observed_size_text = (f"{observed_size[0]} × {observed_size[1]} px"
                          if isinstance(observed_size, list) and len(observed_size) == 2 else "not reported")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="dark"><title>Reviewer · {html.escape(summary['project_id'])}</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#111512;color:#e7ebe3;font:15px/1.5 system-ui,sans-serif}}header{{padding:1rem 1.4rem;background:#1d241f;border-bottom:1px solid #38433a}}h1,h2{{margin:.2rem 0 .8rem}}header p,.muted{{color:#abb6aa}}main{{display:grid;grid-template-columns:minmax(0,1.7fr) minmax(300px,.8fr);gap:1rem;padding:1rem;max-width:1600px;margin:auto}}section{{background:#1a201b;border:1px solid #344037;border-radius:.7rem;padding:1rem;min-width:0}}iframe{{width:100%;height:min(72vh,850px);border:0;border-radius:.45rem;background:#080a08}}a{{color:#c4d5b6}}.links{{display:grid;gap:.45rem}}.metric{{display:grid;grid-template-columns:1fr auto;gap:.6rem;border-bottom:1px solid #344037;padding:.4rem 0}}.metric b{{min-width:0;text-align:right;overflow-wrap:anywhere}}.warning{{background:#33291b;border:1px solid #755c34;padding:.7rem;border-radius:.5rem}}@media(max-width:850px){{main{{grid-template-columns:1fr}}iframe{{height:65vh}}}}</style></head><body>
<header><h1>Technical Reviewer · {html.escape(summary['project_id'])}</h1><p>First visual candidate · release authorization: NEEDS_REVIEW · no source photographs are included</p></header><main><section><h2>Candidate model preview</h2><iframe title="Unreviewed Visitor preview" src="../visitor/index.html"></iframe></section><aside>
<section><h2>Evidence</h2><div class="metric"><span>Source photos</span><b>{sources['image_count']}</b></div><div class="metric"><span>Registered views</span><b>{sfm.get('registered_image_count','unknown')} / {sfm.get('source_image_count', sources['image_count'])}</b></div><div class="metric"><span>Sparse SfM points</span><b>{sfm.get('sparse_point_count',0)}</b></div><div class="metric"><span>Gaussian splats</span><b>{training.get('gaussian_count','unknown')}</b></div><div class="metric"><span>Model size</span><b>{training.get('model_size_bytes',0):,} bytes</b></div><div class="metric"><span>Training divisor</span><b>{html.escape(str(training.get('resolution_divisor','unknown')))}</b></div><div class="metric"><span>Backend image size</span><b>{html.escape(observed_size_text)}</b></div><div class="metric"><span>Resolution evidence</span><b>{html.escape(str(training_resolution.get('backend_report_status','NOT_AVAILABLE')))}</b></div><div class="metric"><span>LiDAR</span><b>{html.escape(str(lidar.get('inspection_status','NOT_DETECTED')))}</b></div><div class="metric"><span>LiDAR alignment</span><b>{html.escape(str(lidar.get('alignment_status','NOT_ATTEMPTED')))}</b></div><div class="metric"><span>Navigation</span><b>{html.escape(str(summary['navigation']['status']))}</b></div><div class="metric"><span>Viewer orientation</span><b>{html.escape(orientation_label)}</b></div></section>
<section><h2>Warnings and limitations</h2><ul class="warning">{warning_items or '<li>No additional warning recorded.</li>'}</ul><p class="muted">SfM coordinates are arbitrary scale. The unbounded Visitor preview is for technical review only; navigation and public release remain unapproved.</p></section>
{coverage_panel}
<section><h2>Diagnostics</h2><div class="links">{''.join(diagnostics)}</div></section></aside></main></body></html>"""


def _validate_preview(root: Path, identity: dict) -> list[str]:
    errors = []
    visitor = root / "visitor"
    reviewer = root / "reviewer"
    required = [reviewer / "index.html", visitor / "index.html", visitor / "js" / "viewer.js",
                visitor / "css" / "viewer.css", visitor / "vendor" / "supersplat-viewer.js",
                visitor / "vendor" / "supersplat-viewer.css", visitor / "config" / "settings.json",
                visitor / "config" / "exhibition.json", visitor / "config" / "locales.json",
                visitor / "assets" / "exhibition.ply"]
    for path in required:
        if not path.is_file():
            errors.append(f"missing {path.relative_to(root)}")
    model = visitor / "assets" / "exhibition.ply"
    if model.is_file() and (sha256_file(model) != identity["sha256"] or model.stat().st_size != identity["file_bytes"]):
        errors.append("visitor candidate model failed full-hash identity validation")
    import re
    for page in (reviewer / "index.html", visitor / "index.html"):
        if not page.is_file():
            continue
        text = page.read_text(encoding="utf-8")
        for match in re.findall(r"(?:href|src)=\"([^\"]+)\"", text):
            if match.startswith(("#", "data:", "mailto:", "http://", "https://")):
                continue
            target = (page.parent / match.split("?", 1)[0].split("#", 1)[0]).resolve()
            if root.resolve() not in target.parents and target != root.resolve():
                errors.append(f"path escapes preview package: {page.relative_to(root)} -> {match}")
            elif target.is_dir():
                target = target / "index.html"
                if not target.is_file():
                    errors.append(f"missing referenced directory index: {page.relative_to(root)} -> {match}")
            elif not target.is_file():
                errors.append(f"missing referenced asset: {page.relative_to(root)} -> {match}")
    return errors


def _source_image_rows(root: Path, config: dict) -> list[dict[str, Any]]:
    index = output_path(root, config) / "work" / "source-index.json"
    return read_json(index).get("images", []) if index.is_file() else []


def _source_image_stats(root: Path, config: dict) -> dict[str, Any]:
    rows = _source_image_rows(root, config)
    return {"image_count": len(rows), "total_bytes": sum(row.get("size_bytes", 0) for row in rows),
            "formats": {fmt: sum(str(row.get("format", "")).upper() == fmt for row in rows)
                        for fmt in sorted({str(row.get("format", "")).upper() for row in rows})}}


def _public_sfm_summary(details: dict, cameras: list[dict], sparse_count: int) -> dict[str, Any]:
    return {"status": details.get("quality_gate", "UNKNOWN"),
            "registered_image_count": details.get("registered_image_count", len(cameras)),
            "source_image_count": details.get("source_image_count"),
            "registered_image_ratio": details.get("registered_image_ratio"),
            "camera_pose_count": len(cameras), "sparse_point_count": sparse_count,
            "mean_point_reprojection_error_px": details.get("mean_point_reprojection_error_px"),
            "reprojection_metrics": details.get("reprojection_metrics"),
            "backend_observation_reprojection_summary": details.get("backend_observation_reprojection_summary"),
            "warnings": details.get("warnings", [])}


def _public_lidar_summary(report: dict, alignment_path: Path,
                          navigation_details: dict | None = None) -> dict[str, Any]:
    alignment = read_json(alignment_path) if alignment_path.is_file() else {}
    navigation_details = navigation_details or {}
    selected = report.get("selected", {})
    return {"detected": report.get("detected", False),
            "inspection_status": report.get("inspection_status", "NOT_DETECTED"),
            "point_count": selected.get("point_count"), "format": selected.get("format"),
            "coordinate_units": selected.get("coordinate_units", {}).get("status"),
            "alignment_attempted": alignment.get("alignment_attempted", False),
            "alignment_status": alignment.get("status", report.get("alignment_status", "NOT_ATTEMPTED")),
            "alignment_accepted": alignment.get("accepted", False),
            "training_consumed": alignment.get("training_consumed", False),
            "navigation_consumed": navigation_details.get(
                "aligned_lidar_navigation_consumed", alignment.get("navigation_consumed", False)),
            "navigation_use_status": navigation_details.get("ground_plane_candidate_status", "NOT_USED"),
            "warnings": report.get("warnings", []) + alignment.get("warnings", [])}


def _preview_warnings(sfm: dict, lidar: dict, navigation: dict,
                      orientation: dict | None = None) -> list[str]:
    warnings = []
    warnings.extend(str(value) for value in sfm.get("warnings", []))
    warnings.extend(str(value) for value in lidar.get("warnings", []))
    if navigation.get("status") not in {"COMPLETED", "ADOPTED"}:
        warnings.append("Navigation geometry is a proposal or remains unverified; the Visitor preview has no enforced scene boundary.")
    if orientation and orientation.get("applied"):
        warnings.append("Viewer Y-up is an unreviewed proposal from registered camera orientations; it does not establish physical gravity or validate navigation.")
    else:
        warnings.append("Camera-up evidence was insufficient; the viewer keeps the raw pose with an identity transform and requires orientation review.")
    warnings.append("The first-visual model is an unreviewed candidate. Do not treat this preview as an approved public release.")
    return warnings


def _summary_markdown(summary: dict) -> str:
    return (f"# First visual summary — {summary['project_id']}\n\n"
            f"- Engine: {summary['engine_version']} ({summary['engine_git_commit']})\n"
            f"- Photos: {summary['source_images']['image_count']} across {summary['source_images']['formats']}\n"
            f"- SfM: {summary['sfm']['status']}; {summary['sfm']['registered_image_count']} registered views; {summary['sfm']['sparse_point_count']} sparse points\n"
            f"- LiDAR: inspected={summary['lidar']['inspection_status']}; alignment={summary['lidar']['alignment_status']}; training consumed={summary['lidar']['training_consumed']}; navigation consumed={summary['lidar']['navigation_consumed']}\n"
            f"- Training: {summary['training']['status']}; {summary['training']['iterations']} iterations; quality={summary['training']['quality_preset']}; resolution divisor={summary['training']['resolution_divisor']}\n"
            f"- Effective training settings: {summary['training'].get('effective_configuration_status', 'NOT_RECORDED')}; {summary['training'].get('effective_parameter_count', 0)} backend defaults/overrides (full record: Reviewer diagnostics/training-run.json)\n"
            f"- Training resolution observation: {summary['training'].get('training_resolution', {}).get('backend_report_status', 'NOT_AVAILABLE')}\n"
            f"- Model: {summary['training']['model_format']}, {summary['training']['gaussian_count']} Gaussians, {summary['training']['model_size_bytes']} bytes, SHA-256 `{summary['training']['model_sha256']}`\n"
            f"- Navigation: {summary['navigation']['status']} (review required)\n"
            f"- Viewer orientation: {summary['viewer_orientation']['status']} (camera-pose proposal; physical up and navigation remain unverified)\n"
            f"- Reviewer: `{summary['reviewer_html']}`; Visitor preview: `{summary['visitor_html']}`\n"
            f"- Release authorization: {summary['release_authorization']}\n\n"
            "## Warnings\n\n" + "\n".join(f"- {item}" for item in summary["warnings"]) + "\n")


def _details(stage: dict) -> dict[str, Any]:
    try:
        return json.loads(stage.get("details_json") or "{}")
    except (TypeError, ValueError):
        return {}


def _optional_path(value: Any) -> Path | None:
    return Path(value).expanduser() if isinstance(value, str) and value else None


def _git_commit(product_root: Path) -> str | None:
    try:
        proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=product_root, capture_output=True,
                              text=True, timeout=3, check=True)
        return proc.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def statistics_median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (float(ordered[middle - 1]) + float(ordered[middle])) / 2
