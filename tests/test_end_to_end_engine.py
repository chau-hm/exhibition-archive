import json
import importlib.util
import math
import struct
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from exhibition_archive.cli import build_parser, cmd_run, main
from exhibition_archive.input_prep import materialize_image_only_dataset
from exhibition_archive.lidar import (PlyInspectionError, _alignment_validation_evidence, _inverse_similarity_transform, _similarity_fit_numpy,
                                      attempt_similarity_alignment, discover_input_files,
                                      inspect_optional_lidar, inspect_ply, propose_lidar_ground_plane,
                                      read_ply_xyz_sample)
from exhibition_archive.navigation import generate_navigation
from exhibition_archive.preview import build_first_visual_preview
from exhibition_archive.state import ProjectState
from exhibition_archive.workflow import STAGES, _navigation_source, run_workflow
from exhibition_archive.workflow import _training_resolution_request
from exhibition_archive.backends import BackendError, training_preset_configuration


GAUSSIAN_NAMES = ["x", "y", "z", "opacity", "f_dc_0", "f_dc_1", "f_dc_2",
                  "scale_0", "scale_1", "scale_2", "rot_0", "rot_1", "rot_2", "rot_3"]


def write_cloud(path: Path, points, fmt="ascii", colors=False):
    properties = ["property float x", "property float y", "property float z"]
    if colors:
        properties += ["property uchar red", "property uchar green", "property uchar blue",
                       "property float nx", "property float ny", "property float nz"]
    if fmt == "ascii":
        header = "ply\nformat ascii 1.0\nelement vertex {}\n{}\nend_header\n".format(
            len(points), "\n".join(properties))
        rows = []
        for point in points:
            row = list(point[:3]) + ([120, 130, 140, 0.0, 0.0, 1.0] if colors else [])
            rows.append(" ".join(str(value) for value in row))
        path.write_text(header + "\n".join(rows) + "\n", encoding="ascii")
    else:
        endian = ">" if fmt == "binary_big_endian" else "<"
        header = (f"ply\nformat {fmt} 1.0\nelement vertex {len(points)}\n" +
                  "\n".join(properties) + "\nend_header\n").encode("ascii")
        with path.open("wb") as stream:
            stream.write(header)
            for point in points:
                stream.write(struct.pack(endian + "3f3B3f", *point[:3], 120, 130, 140, 0.0, 0.0, 1.0))


def write_gaussian(path: Path):
    header = ["ply", "format ascii 1.0", "element vertex 1"]
    header.extend(f"property float {name}" for name in GAUSSIAN_NAMES)
    path.write_text("\n".join(header) + "\nend_header\n" + "0 0 0 0 0 0 0 0 0 0 1 0 0 0\n", encoding="ascii")


def write_partial_gaussian(path: Path):
    path.write_text("ply\nformat ascii 1.0\nelement vertex 1\n"
                    "property float x\nproperty float y\nproperty float z\n"
                    "property float f_dc_0\nproperty float scale_0\nproperty float rot_0\n"
                    "end_header\n0 0 0 0 0 0\n", encoding="ascii")


def test_mixed_discovery_and_gaussian_ply_separation(tmp_path):
    (tmp_path / "view.jpg").write_bytes(b"photo bytes")
    raw = tmp_path / "depth.ply"
    write_cloud(raw, [[0, 1, 2], [1, 2, 3]], colors=True)
    write_gaussian(tmp_path / "trained-model.ply")

    found = discover_input_files(tmp_path)

    assert found["image_count"] == 1
    assert found["raw_lidar_status"] == "SELECTED_UNAMBIGUOUS"
    assert found["selected_raw_lidar_relative_path"] == "depth.ply"
    assert found["gaussian_model_count"] == 1
    assert found["ply_files"][0]["classification"] == "RAW_POINT_CLOUD_CANDIDATE"


def test_multiple_raw_ply_candidates_are_reported_as_ambiguous(tmp_path):
    (tmp_path / "a.jpg").write_bytes(b"photo")
    write_cloud(tmp_path / "depth-a.ply", [[0, 0, 0], [1, 2, 3]])
    write_cloud(tmp_path / "depth-b.ply", [[1, 1, 1], [2, 3, 4]])

    found = discover_input_files(tmp_path)

    assert found["raw_lidar_status"] == "AMBIGUOUS_MULTIPLE_CANDIDATES"
    assert found["selected_raw_lidar_relative_path"] is None


def test_incomplete_gaussian_signature_is_not_selected_as_lidar(tmp_path):
    write_partial_gaussian(tmp_path / "truncated-model.ply")

    found = discover_input_files(tmp_path)

    assert found["raw_lidar_status"] == "NOT_DETECTED"
    assert found["ply_files"][0]["classification"] == "POSSIBLE_GAUSSIAN_SPLATTING_MODEL"


def test_photo_only_and_invalid_lidar_fallback(tmp_path):
    (tmp_path / "a.jpg").write_bytes(b"photo one")
    (tmp_path / "b.png").write_bytes(b"photo two")
    found = discover_input_files(tmp_path)
    assert found["raw_lidar_status"] == "NOT_DETECTED"
    assert found["minimum_photo_count_met"]

    bad = tmp_path / "broken.ply"
    bad.write_text("not a ply\n", encoding="ascii")
    found = discover_input_files(tmp_path)
    report = inspect_optional_lidar(found, tmp_path, tmp_path / "report")
    assert report["inspection_status"] == "INCOMPATIBLE_OR_CORRUPT"
    assert "photo reconstruction may continue" in report["warnings"][0] or "will not block photo" in report["warnings"][0]
    rejected = attempt_similarity_alignment(report, [], tmp_path / "registration")
    assert rejected["status"] == "REJECTED"
    assert rejected["training_consumed"] is False


def test_valid_lidar_without_open3d_reports_actionable_optional_fallback(tmp_path):
    # patch.dict restores sys.modules after the context. Import NumPy first so
    # restoring the module table does not remove a native extension that has
    # already been initialized and break subsequent numerical tests.
    pytest.importorskip("numpy")
    source = tmp_path / "input"
    output = tmp_path / "output"
    source.mkdir()
    points = [[float(x), float(y), float(z)] for x in range(4) for y in range(4) for z in range(4)]
    cloud = source / "depth.ply"
    write_cloud(cloud, points)
    discovery = discover_input_files(source)
    inspection = inspect_optional_lidar(discovery, source, output)

    with patch.dict("sys.modules", {"open3d": None}):
        report = attempt_similarity_alignment(inspection, points + [[0.5, 0.5, 0.5]] * 50, output / "registration")

    assert inspection["inspection_status"] == "VALID"
    assert report["status"] == "REJECTED"
    assert report["alignment_attempted"] is False
    expected_missing_dependency = "numpy" if importlib.util.find_spec("numpy") is None else "open3d"
    assert report["registration_dependency"]["name"] == expected_missing_dependency
    assert report["registration_dependency"]["status"] == "UNAVAILABLE"
    assert report["training_consumed"] is False
    assert report["navigation_consumed"] is False


def test_alignment_rejects_insufficient_or_degenerate_geometry(tmp_path):
    cloud = tmp_path / "cloud.ply"
    preview = tmp_path / "preview.ply"
    write_cloud(cloud, [[0, 0, 0], [1, 0, 0], [0, 1, 0]])
    preview.write_bytes(cloud.read_bytes())
    inspection = {"inspection_status": "VALID", "selected": {"path": str(cloud)},
                  "preview_path": str(preview)}
    result = attempt_similarity_alignment(inspection, [[float(i), 0, 0] for i in range(49)], tmp_path / "rejected")
    assert result["alignment_attempted"] is False
    assert result["status"] == "REJECTED"
    assert "Only 49 sparse SfM points" in result["reason"]


def test_alignment_acceptance_screen_records_each_evidence_check():
    result = _alignment_validation_evidence(12, 0.20, 0.21, 0.03, 0.025)
    assert result["accepted"] is True
    assert all(result["checks"].values())

    rejected = _alignment_validation_evidence(12, 0.20, 0.19, 0.031, 0.025)
    assert rejected["accepted"] is False
    assert rejected["checks"]["minimum_reverse_overlap"] is False
    assert rejected["checks"]["maximum_bidirectional_p95_residual"] is False


def test_aligned_lidar_preview_transform_maps_lidar_back_into_sfm_frame():
    sfm_to_lidar = [[0.0, -2.0, 0.0, 4.0],
                    [2.0, 0.0, 0.0, -2.0],
                    [0.0, 0.0, 2.0, 7.0],
                    [0.0, 0.0, 0.0, 1.0]]
    lidar_to_sfm = _inverse_similarity_transform(sfm_to_lidar)
    point_sfm = [1.0, 2.0, 3.0]
    point_lidar = [sum(sfm_to_lidar[row][column] * point_sfm[column] for column in range(3)) + sfm_to_lidar[row][3]
                   for row in range(3)]
    recovered = [sum(lidar_to_sfm[row][column] * point_lidar[column] for column in range(3)) + lidar_to_sfm[row][3]
                 for row in range(3)]
    assert recovered == pytest.approx(point_sfm)


def test_accepted_versioned_alignment_is_handed_to_navigation_as_a_proposal(tmp_path):
    root = tmp_path / "project"
    output = root / "output"
    lidar_dir = output / "lidar" / "registrations" / "alignment-execution-000042"
    lidar_dir.mkdir(parents=True)
    aligned = lidar_dir / "aligned-cloud-preview.ply"
    write_cloud(aligned, [[float(x), float(y), 0.0]
                          for x in range(12) for y in range(10)])
    alignment = lidar_dir / "alignment-report.json"
    alignment.write_text(json.dumps({
        "status": "ACCEPTED", "accepted": True, "aligned_preview_status": "WRITTEN",
        "aligned_preview_path": str(aligned),
    }), encoding="utf-8")
    reconstruction_dir = root / "sfm" / "sparse" / "0"
    reconstruction_dir.mkdir(parents=True)
    camera_centers = [{"camera_center": [float(x), float(y), 1.5]}
                      for x, y in ((0, 0), (2, 0), (4, 0), (0, 2), (2, 2), (4, 2))]

    class FixtureState:
        def stage(self, stage_id):
            if stage_id == "lidar_registration":
                return {"details_json": json.dumps({
                    "alignment_report_path": str(alignment),
                    "aligned_preview_path": str(aligned),
                })}
            return None

    config = {"output_directory": "output", "project_id": "alignment-handoff-fixture",
              "integrations": {"sfm": {"directory": "sfm/sparse/0"},
                               "navigation": {"mode": "generate_from_sfm"}}}
    reconstructed = {
        "cameras": camera_centers,
        "sparse_points": [[0.0, 0.0, 0.0]],
        "camera_models": [],
        "source_reconstruction": {"coordinate_frame": {"up_axis": "z"}},
    }

    plane_proposal = {"status": "PROPOSED", "classification": "PROPOSED_REQUIRES_HUMAN_GROUND_IDENTIFICATION",
                      "normal": [0.0, 0.0, 1.0], "offset": 0.0, "source": "synthetic proposal",
                      "evidence": {"fixture": True}}
    with patch("exhibition_archive.workflow.read_colmap_reconstruction", return_value=reconstructed), \
            patch("exhibition_archive.workflow.propose_lidar_ground_plane", return_value=plane_proposal):
        source, _settings = _navigation_source(root, config, FixtureState())

    assert source["aligned_lidar_geometry_used"] is True, source["aligned_lidar_diagnostics"]
    assert source["aligned_lidar_diagnostics"]["status"] == "ALIGNED_GEOMETRY_CONSUMED_FOR_NAVIGATION_DIAGNOSTICS"
    assert source["aligned_lidar_diagnostics"]["ground_plane_candidate"]["status"] == "PROPOSED"
    assert source["floor_plane"]["classification"] == "PROPOSED_REQUIRES_HUMAN_GROUND_IDENTIFICATION"
    assert str(alignment.resolve()) in source["source_files"]
    assert str(aligned.resolve()) in source["source_files"]


def test_training_preset_configuration_captures_defaults_and_command_overrides():
    help_output = ("Usage: spirula train 3dgs [OPTIONS]\n"
                   "  --quality  [high]  Quality preset\n"
                   "  --num-iterations  [30000]  Training iterations\n"
                   "  --train-resolution-divisor  [0]  Resolution divisor\n")
    capture = {"argv": ["spirula", "train", "3dgs", "--quality", "high", "--help-all"],
               "exit_code": 0, "stdout": help_output, "stderr": ""}
    with patch("exhibition_archive.backends._capture", return_value=capture):
        report = training_preset_configuration(Path("/fake/spirula"), "3dgs", "high",
                                               ["spirula", "train", "3dgs", "--quality", "high",
                                                "--num-iterations", "30000", "--train-resolution-divisor", "1"])
    assert report["status"] == "CAPTURED"
    assert report["backend_defaults"]["--train-resolution-divisor"] == "0"
    assert report["effective_parameters"]["--train-resolution-divisor"] == "1"
    assert report["effective_parameter_sources"]["--train-resolution-divisor"] == "EXPLICIT_ENGINE_COMMAND"


@pytest.mark.parametrize("format_name", ["ascii", "binary_little_endian", "binary_big_endian"])
def test_ply_inspection_records_bounds_colors_normals_and_units(tmp_path, format_name):
    path = tmp_path / "depth.ply"
    write_cloud(path, [[-1, 0, 2], [3, 4, 5]], fmt=format_name, colors=True)
    report = inspect_ply(path)
    assert report["format"] == format_name
    assert report["point_count"] == 2
    assert report["bounding_box"]["x"] == [-1.0, 3.0]
    assert report["rgb_available"] is True
    assert report["normals_available"] is True
    assert report["coordinate_units"]["status"] == "UNKNOWN_OR_UNDECLARED"


@pytest.mark.parametrize("format_name", ["ascii", "binary_little_endian", "binary_big_endian"])
def test_bounded_ply_xyz_sample_supports_ascii_and_binary(tmp_path, format_name):
    path = tmp_path / "cloud.ply"
    write_cloud(path, [[-1, 0, 2], [3, 4, 5]], fmt=format_name, colors=True)

    sample = read_ply_xyz_sample(path, maximum=2)

    assert sample["point_count"] == 2
    assert sample["sampled_point_count"] == 2
    assert sample["bounding_box"]["x"] == [-1.0, 3.0]
    assert sample["points"] == [[-1.0, 0.0, 2.0], [3.0, 4.0, 5.0]]


def test_invalid_nonfinite_ply_is_reported_without_consumption(tmp_path):
    path = tmp_path / "invalid.ply"
    write_cloud(path, [[0, 0, 0], [float("nan"), 1, 2]])
    report = inspect_ply(path)
    assert report["status"] == "INVALID_NONFINITE_COORDINATES"
    assert report["invalid_coordinate_count"] == 1


def test_similarity_fit_recovers_uniform_scale_rotation_and_translation():
    np = pytest.importorskip("numpy")
    source = np.array([[0, 0, 0], [1, 0, 0], [0, 2, 0], [0, 0, 3], [1, 2, 3]], dtype=float)
    rotation = np.array([[0, -1, 0], [1, 0, 0], [0, 0, 1]], dtype=float)
    expected = 2.5 * source @ rotation.T + np.array([4, -2, 7])
    transform, evidence = _similarity_fit_numpy(source, expected, np)
    assert math.isclose(np.cbrt(np.linalg.det(transform[:3, :3])), 2.5, rel_tol=1e-9)
    assert np.allclose(transform[:3, 3], [4, -2, 7], atol=1e-9)
    assert evidence["p95"] < 1e-9


def test_lidar_plane_is_only_proposed_when_camera_trajectory_supports_it():
    np = pytest.importorskip("numpy")
    cloud = [[float(x), float(y), 0.0] for x in range(-10, 11) for y in range(-10, 11)]
    cameras = [[2.0 * math.cos(angle), 2.0 * math.sin(angle), 1.0]
               for angle in np.linspace(0.0, 2.0 * math.pi, 12, endpoint=False)]

    candidate = propose_lidar_ground_plane(cloud, cameras)

    assert candidate["status"] == "PROPOSED"
    assert candidate["classification"] == "PROPOSED_REQUIRES_HUMAN_GROUND_IDENTIFICATION"
    assert candidate["evidence"]["plane_inlier_count"] == len(cloud)
    assert candidate["limitations"]


def test_lidar_ground_plane_falls_back_for_degenerate_camera_trajectory():
    pytest.importorskip("numpy")
    cloud = [[float(x), float(y), 0.0] for x in range(-10, 11) for y in range(-10, 11)]
    cameras = [[float(index), 0.0, 1.0] for index in range(8)]

    candidate = propose_lidar_ground_plane(cloud, cameras)

    assert candidate["status"] == "UNVERIFIED"
    assert "camera centers" in candidate["reason"].casefold()


def test_image_only_dataset_preserves_source_bytes_and_manifest(tmp_path):
    source = tmp_path / "source"
    root = tmp_path / "workspace"
    source.mkdir()
    original = source / "view.jpg"
    original.write_bytes(b"original photo bytes including metadata")
    config = {"project_id": "playground-fixture", "source": {"photo_directory": str(source)},
              "integrations": {"sfm": {"image_directory": "derived/images",
                                         "derived_image_manifest": "derived/images.json"}}}
    inventory = {"images": [{"relative_path": "view.jpg", "sha256": __import__("hashlib").sha256(original.read_bytes()).hexdigest()}]}

    result = materialize_image_only_dataset(root, config, inventory)

    assert Path(result["dataset_directory"], "view.jpg").read_bytes() == original.read_bytes()
    assert original.read_bytes() == b"original photo bytes including metadata"
    manifest = json.loads(Path(result["manifest_path"]).read_text())
    assert manifest["source_photographs_modified"] is False
    assert manifest["image_mappings"][0]["normalization"].startswith("BYTE_IDENTICAL_COPY")


def test_training_resolution_records_source_pixels_and_divisor_one(tmp_path):
    from PIL import Image
    images = tmp_path / "images"
    images.mkdir()
    Image.new("RGB", (640, 480)).save(images / "view.png")

    report = _training_resolution_request(images, 1)

    assert report["configured_resolution_policy"] == "full source pixels"
    assert report["source_image_dimensions"][0]["configured_effective_training_dimensions"] == [640, 480]
    assert report["backend_report_status"] == "PENDING_TRAINING_LOG_INSPECTION"


def test_dry_run_validates_and_does_not_create_output(tmp_path, capsys):
    source = tmp_path / "input"
    output = tmp_path / "new-output"
    source.mkdir()
    (source / "one.jpg").write_bytes(b"one")
    (source / "two.jpg").write_bytes(b"two")
    capabilities = {"tool": {"name": "fixture Spirula", "executable": "/tmp/spirula", "version": "test"}}
    with patch("exhibition_archive.backends.discover_environment", return_value=capabilities), \
         patch("exhibition_archive.backends.image_inventory", return_value={"image_count": 2, "formats": {".jpg": 2}, "duplicates": {}}), \
         patch("exhibition_archive.backends.build_sfm_command", return_value=["spirula", "sfm", "--quality", "high"]), \
         patch("exhibition_archive.backends.build_training_command", return_value=["spirula", "train", "3dgs", "--train-resolution-divisor", "1"]), \
         patch("exhibition_archive.backends.training_preset_configuration", return_value={"status": "CAPTURED", "effective_parameters": {"--train-resolution-divisor": "1"}}), \
         patch("exhibition_archive.environment.repair_python_environment", side_effect=AssertionError("dry-run must not install")):
        args = build_parser().parse_args(["run", "--input", str(source), "--output", str(output),
                                          "--project-id", "playground-fixture", "--dry-run", "--json"])
        code = cmd_run(args)
    result = json.loads(capsys.readouterr().out)
    assert code == 0
    assert result["status"].startswith("READY")
    assert result["backend"]["training_process_launched"] is False
    assert result["backend"]["training_effective_configuration"]["status"] == "CAPTURED"
    assert not output.exists()


def test_dry_run_reports_photo_blocker_with_nonzero_exit_code(tmp_path, capsys):
    source = tmp_path / "empty-input"
    output = tmp_path / "not-created"
    source.mkdir()
    capabilities = {"tool": {"name": "fixture Spirula", "executable": "/tmp/spirula", "version": "test"}}
    with patch("exhibition_archive.backends.discover_environment", return_value=capabilities), \
         patch("exhibition_archive.backends.image_inventory", side_effect=BackendError("At least two supported images are required", failure_kind="INVALID_INPUT")), \
         patch("exhibition_archive.backends.build_sfm_command", return_value=["spirula", "sfm"]), \
         patch("exhibition_archive.backends.build_training_command", return_value=["spirula", "train"]), \
         patch("exhibition_archive.backends.training_preset_configuration", return_value={"status": "CAPTURED", "effective_parameters": {}}):
        args = build_parser().parse_args(["run", "--input", str(source), "--output", str(output),
                                          "--project-id", "empty-fixture", "--dry-run", "--json"])
        code = cmd_run(args)
    report = json.loads(capsys.readouterr().out)
    assert code == 2
    assert report["status"] == "BLOCKED"
    assert not output.exists()


def test_cli_returns_nonzero_for_invalid_input_directory(tmp_path, capsys):
    output = tmp_path / "output"
    code = main(["run", "--input", str(tmp_path / "missing"), "--output", str(output),
                 "--project-id", "missing-fixture", "--dry-run"])

    assert code == 1
    assert "Input directory does not exist" in capsys.readouterr().err
    assert not output.exists()


def test_guided_sfm_review_records_only_the_explicit_component_decision(tmp_path, monkeypatch, capsys):
    import exhibition_archive.sfm_review as sfm_review

    validation = tmp_path / "sfm-validation.json"
    validation.write_text(json.dumps({"components": [{"component_id": "0", "registered_image_count": 77}]}))

    class State:
        def stage(self, stage_id):
            assert stage_id == "sfm"
            return {"details_json": json.dumps({"registered_image_count": 77,
                                                "source_image_count": 77,
                                                "sparse_point_count": 63824,
                                                "quality_gate": "SFM_VALID_WITH_WARNINGS",
                                                "sfm_validation_path": str(validation)})}

    calls = []
    monkeypatch.setattr(sfm_review, "review_sfm_component",
                        lambda *args, **kwargs: calls.append(kwargs) or {"status": "ACCEPTED"})
    answers = iter(["d", "a", "Reviewer", "Reviewed component coverage", "Visual fidelity still needs review"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    from exhibition_archive.cli import _guided_sfm_component_review
    accepted = _guided_sfm_component_review(tmp_path, {"project_id": "guided-fixture"}, State(),
                                            {"status": "NEEDS_REVIEW", "needs_review": ["sfm"]})

    assert accepted is True
    assert calls[0]["component_id"] == "0"
    assert calls[0]["decision"] == "accept-experimental-training"
    assert calls[0]["limitations"] == ["Visual fidelity still needs review"]
    assert "63824" in capsys.readouterr().err


def test_outdoor_navigation_emits_camera_coverage_proposal_without_fake_ceiling():
    source = {"source_reconstruction": {"identity": {"reconstruction_id": "open-air-fixture"}},
              "coordinate_frame": {"coordinates_are_viewer": True, "up_axis": "UNSPECIFIED",
                                   "source_to_viewer": {"uniform_scale": 1, "rotation": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                                                        "translation": [0, 0, 0], "verified": True}},
              "stations": [{"stationId": str(i), "centerCommonViewer": [x, 2 + (i % 2) * 0.1, y], "trusted": True}
                           for i, (x, y) in enumerate([(0, 0), (1, 0), (2, 0), (2, 1), (1, 2), (0, 2), (0, 1)])]}
    proposal = generate_navigation(source, {"scene_type": "outdoor", "camera_clearance_units": 0})
    assert proposal["space_assumption"]["scene_type"] == "outdoor"
    assert proposal["coverage_assumptions"]["physical_walls_or_ceiling_inferred"] is False
    assert proposal["regions"]
    assert proposal["regions"][0]["vertical_bounds"]["ceiling"] is None
    assert proposal["status"] == "NAVIGATION_INSUFFICIENT_GEOMETRY"


def test_workflow_resumes_after_stage_failure_and_stops_at_first_visual(tmp_path):
    import exhibition_archive.workflow as workflow

    source = tmp_path / "source"
    source.mkdir()
    (source / "photo.jpg").write_bytes(b"photo")
    root = tmp_path / "project"
    config = {"project_id": "resume-fixture", "source": {"photo_directory": str(source)},
              "output_directory": "output", "integrations": {"model": {"mode": "adopt_existing"}},
              "resource_policy": {}}
    stage_defs = [{"id": "photo_inventory", "deps": []},
                  {"id": "training", "deps": ["photo_inventory"]},
                  {"id": "first_visual", "deps": ["training"]}]
    state = ProjectState(root)
    calls = {"training": 0}

    def fake_execute(_root, _config, _state, stage_id, output_dir, _execution_id, *_args):
        output_dir.mkdir(parents=True, exist_ok=True)
        if stage_id == "training":
            calls["training"] += 1
            if calls["training"] == 1:
                raise RuntimeError("synthetic one-time interruption")
        target = output_dir / f"{stage_id}.json"
        target.write_text(json.dumps({"stage": stage_id}))
        return {"status": "COMPLETED", "artifacts": [target], "details": {"stage": stage_id}}

    try:
        with patch.object(workflow, "STAGES", stage_defs), patch.object(workflow, "_execute_stage", side_effect=fake_execute):
            with pytest.raises(RuntimeError, match="synthetic one-time interruption"):
                run_workflow(root, config, state, stop_after_stage="first_visual")
            resumed = run_workflow(root, config, state, resume=True, stop_after_stage="first_visual")
        assert resumed["status"] == "AWAITING_VISUAL_REVIEW"
        assert resumed["stages"]["first_visual"] == "COMPLETED"
        assert calls["training"] == 2
    finally:
        state.close()


def test_resume_after_export_failure_reuses_completed_sfm_cache(tmp_path):
    import exhibition_archive.workflow as workflow

    source = tmp_path / "source"
    source.mkdir()
    (source / "photo.jpg").write_bytes(b"photo bytes")
    root = tmp_path / "project"
    config = {"project_id": "export-resume-fixture", "source": {"photo_directory": str(source)},
              "output_directory": "output",
              "integrations": {"sfm": {"mode": "adopt_existing"},
                               "model": {"mode": "adopt_existing"}},
              "resource_policy": {}, "retry_policy": {"max_failed_attempts": 2}}
    stage_defs = [{"id": "photo_inventory", "deps": []},
                  {"id": "sfm", "deps": ["photo_inventory"]},
                  {"id": "sfm_studio_export", "deps": ["photo_inventory", "sfm"]},
                  {"id": "training", "deps": ["sfm"]},
                  {"id": "first_visual", "deps": ["training"]}]
    state = ProjectState(root)
    calls = {stage_id: 0 for stage_id in ("photo_inventory", "sfm", "sfm_studio_export", "training", "first_visual")}

    def fake_execute(_root, _config, _state, stage_id, output_dir, _execution_id, *_args):
        calls[stage_id] += 1
        output_dir.mkdir(parents=True, exist_ok=True)
        if stage_id == "sfm_studio_export" and calls[stage_id] == 1:
            # Simulates the prior report serialization failure after the SfM cache is durable.
            raise TypeError("Object of type PosixPath is not JSON serializable")
        target = output_dir / f"{stage_id}.json"
        target.write_text(json.dumps({"stage": stage_id}))
        return {"status": "COMPLETED", "artifacts": [target], "details": {"stage": stage_id}}

    try:
        with patch.object(workflow, "STAGES", stage_defs), patch.object(workflow, "_execute_stage", side_effect=fake_execute):
            with pytest.raises(TypeError, match="PosixPath is not JSON serializable"):
                run_workflow(root, config, state, stop_after_stage="first_visual")
            assert state.stage("sfm")["status"] == "COMPLETED"
            assert state.stage("sfm_studio_export")["status"] == "FAILED"

            resumed = run_workflow(root, config, state, resume=True, stop_after_stage="first_visual")

        assert resumed["status"] == "AWAITING_VISUAL_REVIEW"
        assert calls["sfm"] == 1
        assert calls["sfm_studio_export"] == 2
        assert resumed["stages"]["sfm"] == "SKIPPED_VALID_CACHE"
        assert resumed["stages"]["sfm_studio_export"] == "COMPLETED"
    finally:
        state.close()


def test_end_to_end_stage_dependencies_keep_photos_primary_and_preview_last():
    stages = {stage["id"]: stage for stage in STAGES}
    assert stages["sfm"]["deps"] == ["photo_inventory"]
    assert "sfm" in stages["lidar_registration"]["deps"]
    assert "lidar_inspection" in stages["lidar_registration"]["deps"]
    assert "lidar_registration" not in stages["training"]["deps"]
    assert stages["first_visual"]["deps"] == ["training"]
    assert [stage["id"] for stage in STAGES].index("first_visual") > [stage["id"] for stage in STAGES].index("training")


def test_lidar_changes_invalidate_lidar_inspection_without_invalidating_photo_inventory(tmp_path):
    from exhibition_archive.workflow import stage_fingerprint

    root = tmp_path / "engine"
    source = tmp_path / "source"
    source.mkdir()
    (source / "one.jpg").write_bytes(b"photo")
    cloud = source / "depth.ply"
    write_cloud(cloud, [[0, 0, 0], [1, 2, 3]])
    config = {"project_id": "fingerprint-fixture", "source": {"photo_directory": str(source)},
              "output_directory": "output", "integrations": {"lidar": {"enabled": True}}}
    state = ProjectState(root)
    try:
        photo_before = stage_fingerprint(root, config, state, "photo_inventory")
        lidar_before = stage_fingerprint(root, config, state, "lidar_inspection")
        write_cloud(cloud, [[0, 0, 0], [3, 2, 1]])
        photo_after = stage_fingerprint(root, config, state, "photo_inventory")
        lidar_after = stage_fingerprint(root, config, state, "lidar_inspection")
        assert photo_before == photo_after
        assert lidar_before != lidar_after
    finally:
        state.close()


def test_reviewer_and_visitor_preview_reference_real_fixture_assets(tmp_path):
    root = tmp_path / "workspace"
    output = root / "output"
    model = tmp_path / "candidate.ply"
    write_gaussian(model)
    work = output / "work"
    work.mkdir(parents=True)
    (work / "source-index.json").write_text(json.dumps({"images": [], "image_count": 0}))
    training_report = work / "training-run.json"
    effective_training_configuration = {"status": "CAPTURED", "parsed_default_parameter_count": 2,
                                        "effective_parameters": {"--quality": "high", "--num-iterations": "30000"}}
    training_report.write_text(json.dumps({"training_resolution": {"backend_report_status": "NOT_REPORTED_BY_BACKEND"},
                                           "effective_training_configuration": effective_training_configuration}))
    (output / "inputs").mkdir(parents=True)
    (output / "inputs" / "discovery.json").write_text(json.dumps({
        "input_directory": str(tmp_path / "photos"),
        "ply_files": [{"relative_path": "depth.ply", "sha256": "fixture-sha", "size_bytes": 12,
                       "classification": "RAW_POINT_CLOUD_CANDIDATE", "format": "binary_little_endian"}],
    }))
    config = {"project_id": "viewer-fixture", "source": {"photo_directory": str(tmp_path / "photos")},
              "output_directory": "output", "viewer": {"title": "Fixture"},
              "integrations": {"model": {"mode": "adopt_existing", "quality": "high", "resolution_divisor": 1}}}
    state = ProjectState(root)
    try:
        state.set_stage("training", "COMPLETED", "fingerprint", "digest", None,
                        {"model_path": str(model), "model_status": "UNREVIEWED_CANDIDATE",
                         "training_iterations_requested": 30000,
                         "training_run_path": str(training_report),
                         "training_resolution": {"configured_resolution_divisor": 1,
                                                  "backend_report_status": "NOT_REPORTED_BY_BACKEND",
                                                  "source_image_dimensions": []}})
        output_dir = work / "first-visual-stage"
        result = build_first_visual_preview(root, config, state, output_dir, 1)
        assert result["details"]["validation"]["status"] == "PASS"
        version = Path(result["details"]["version_directory"])
        assert (version / "reviewer" / "index.html").is_file()
        assert (version / "visitor" / "assets" / "exhibition.ply").is_file()
        pipeline_status = json.loads((version / "pipeline-status.json").read_text())
        assert "training-run.json" in pipeline_status["diagnostic_files"]
        assert pipeline_status["pipeline_stages"]["first_visual"] == "COMPLETED"
        assert pipeline_status["pipeline_stages"]["viewer_package"] == "PENDING"
        assert pipeline_status["training"]["effective_configuration_status"] == "CAPTURED"
        reviewer_html = (version / "reviewer" / "index.html").read_text()
        assert "Resolution evidence" in reviewer_html
        assert "NOT_REPORTED_BY_BACKEND" in reviewer_html
        metadata = json.loads((version / "visitor" / "config" / "exhibition.json").read_text())
        assert metadata["release_authorization"] == "NEEDS_REVIEW"
        assert metadata["navigation"]["status"] == "UNVERIFIED"
        manifest = json.loads((version / "manifest.json").read_text())
        assert manifest["source_point_clouds"][0]["sha256"] == "fixture-sha"
        assert manifest["effective_training_configuration"] == effective_training_configuration
        assert manifest["diagnostic_artifacts"]
    finally:
        state.close()
