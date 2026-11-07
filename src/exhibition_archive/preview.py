from __future__ import annotations

import html
import hashlib
import json
import math
import shutil
import subprocess
from pathlib import Path
from typing import Any

from . import __version__
from .adapters import read_json, sha256_file, write_json
from .backends import validate_gaussian_ply
from .workflow import STAGES, output_path, project_path
from .viewer_orientation import estimate_y_up_transform, map_pose_to_viewer


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
    version_dir = output_path(root, config) / "first-visual" / f"revision-{execution_id:06d}"
    if version_dir.exists():
        raise FileExistsError(f"First-visual revision already exists; refusing to overwrite it: {version_dir}")
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
    sparse_count = 0
    sparse_preview = diagnostics / "sfm-sparse-points.ply"
    if sfm_directory is not None and sfm_directory.is_dir():
        from .lidar import colmap_sparse_point_count, sample_colmap_sparse_points
        from .navigation import read_colmap_camera_poses
        camera_rows = read_colmap_camera_poses(sfm_directory)
        sparse_count = colmap_sparse_point_count(sfm_directory)
        sparse_points = sample_colmap_sparse_points(sfm_directory, maximum=100_000)
        _write_xyz_ply(sparse_preview, sparse_points, maximum=100_000)
        write_json(diagnostics / "camera-poses.json", {
            "coordinate_system": "COLMAP arbitrary non-metric world coordinates",
            "pose_count": len(camera_rows),
            "poses": [{key: row.get(key) for key in
                       ("image_id", "image_name", "camera_id", "qvec_wxyz", "tvec", "camera_center")}
                      for row in camera_rows],
        })

    navigation_details = _details(state.stage("navigation") or {})
    navigation_proposal = _optional_path(navigation_details.get("proposal_path"))
    if navigation_proposal is not None and navigation_proposal.is_file():
        shutil.copy2(navigation_proposal, diagnostics / "navigation-proposal.json")
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
