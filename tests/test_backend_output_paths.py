import json
from pathlib import Path
from unittest.mock import patch

import pytest

from exhibition_archive import backends
from exhibition_archive.backends import BackendError, create_attempt_dir
from exhibition_archive.state import ProjectState
from exhibition_archive.workflow import output_path, run_workflow


def test_backend_attempt_output_under_configured_output_root_is_allowed(tmp_path):
    output_root = tmp_path / "project-output"
    engine_root = output_root / ".engine"
    engine_root.mkdir(parents=True)

    attempt = create_attempt_dir(engine_root, output_root, "sfm", 7)

    assert attempt.is_dir()
    assert attempt.parent == output_root / "backend-runs" / "sfm"
    assert backends._safe_child((engine_root, output_root), attempt)


def test_internal_engine_and_revision_viewer_paths_are_authorized(tmp_path):
    output_root = tmp_path / "project-output"
    engine_root = output_root / ".engine"
    engine_root.mkdir(parents=True)
    roots = (engine_root, output_root)

    assert backends._safe_child(roots, engine_root / "derived" / "sfm.json")
    revision = output_root / "first-visual" / "revision-000001"
    assert backends._safe_child(roots, revision / "reviewer" / "index.html")
    assert backends._safe_child(roots, revision / "visitor" / "index.html")


def test_external_and_traversal_paths_are_rejected(tmp_path):
    output_root = tmp_path / "project-output"
    engine_root = output_root / ".engine"
    external = tmp_path / "external"
    engine_root.mkdir(parents=True)

    assert not backends._safe_child((engine_root, output_root), external / "backend-runs" / "sfm")
    assert not backends._safe_child((engine_root, output_root), output_root / ".." / "external" / "report.json")


def test_attempt_directory_rejects_symlink_escape(tmp_path):
    output_root = tmp_path / "project-output"
    engine_root = output_root / ".engine"
    external = tmp_path / "external"
    engine_root.mkdir(parents=True)
    external.mkdir()
    try:
        (output_root / "backend-runs").symlink_to(external, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    with pytest.raises(BackendError, match="escapes the configured"):
        create_attempt_dir(engine_root, output_root, "sfm", 8)

    assert list(external.iterdir()) == []


def test_resume_after_old_workspace_guard_failure_reuses_completed_upstream(tmp_path):
    import exhibition_archive.workflow as workflow

    output_root = tmp_path / "nam-shan-output"
    engine_root = output_root / ".engine"
    source = tmp_path / "photos"
    engine_root.mkdir(parents=True)
    source.mkdir()
    (source / "one.jpg").write_bytes(b"synthetic photo")
    config = {
        "project_id": "path-resume-fixture",
        "source": {"photo_directory": str(source)},
        "output_directory": "..",
        "integrations": {"sfm": {"mode": "adopt_existing"}},
    }
    stages = [{"id": "photo_inventory", "deps": []}, {"id": "sfm", "deps": ["photo_inventory"]}]
    state = ProjectState(engine_root)
    calls = {"photo_inventory": 0, "sfm": 0}

    def fake_execute(root, current_config, _state, stage_id, output_dir, execution_id, *_args):
        calls[stage_id] += 1
        if stage_id == "sfm":
            create_attempt_dir(root, output_path(root, current_config), "sfm", execution_id)
        output_dir.mkdir(parents=True, exist_ok=True)
        artifact = output_dir / f"{stage_id}.json"
        artifact.write_text(json.dumps({"stage": stage_id}), encoding="utf-8")
        return {"status": "COMPLETED", "artifacts": [artifact], "details": {"synthetic_test": True}}

    original_safe_child = backends._safe_child

    def old_workspace_only_guard(authorized_roots, path):
        # Reproduce the prior implementation: only the .engine workspace was
        # considered authorized, even when a separate output root was set.
        engine_only = authorized_roots[0] if isinstance(authorized_roots, tuple) else authorized_roots
        return original_safe_child(engine_only, path)

    try:
        with patch.object(workflow, "STAGES", stages), patch.object(workflow, "_execute_stage", side_effect=fake_execute):
            with patch.object(backends, "_safe_child", side_effect=old_workspace_only_guard):
                with pytest.raises(BackendError, match="Backend output path escapes"):
                    run_workflow(engine_root, config, state)

            assert state.stage("photo_inventory")["status"] == "COMPLETED"
            assert state.stage("sfm")["status"] == "FAILED"

            resumed = run_workflow(engine_root, config, state, resume=True)

        assert resumed["status"] == "COMPLETED"
        assert resumed["stages"]["photo_inventory"] == "SKIPPED_VALID_CACHE"
        assert resumed["stages"]["sfm"] == "COMPLETED"
        assert calls == {"photo_inventory": 1, "sfm": 2}
        assert list((output_root / "backend-runs" / "sfm").iterdir())
    finally:
        state.close()
