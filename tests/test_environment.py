from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from exhibition_archive import environment


def _package(distribution, status="AVAILABLE", version="10.1.0"):
    return {"distribution": distribution, "module": distribution.casefold(),
            "status": status, "version": version if status == "AVAILABLE" else None}


def _probe(photo="AVAILABLE", numpy="AVAILABLE", open3d="AVAILABLE"):
    statuses = {"Pillow": photo, "numpy": numpy, "open3d": open3d}
    return lambda distribution, module, minimum=None: {
        **_package(distribution, statuses[distribution],
                   "0.20.0" if distribution == "open3d" else "1.26.0" if distribution == "numpy" else "10.1.0"),
        "module": module,
    }


def test_photo_only_environment_does_not_require_or_probe_lidar_packages(tmp_path):
    root = tmp_path / "repo"
    (root / ".venv").mkdir(parents=True)
    calls = []

    def probe(distribution, module, minimum=None):
        calls.append(distribution)
        return _package(distribution)

    with patch.object(environment.sys, "prefix", str(root / ".venv")):
        report = environment.inspect_python_environment(root, lidar_registration=False,
                                                       package_probe=probe, uv_executable="/usr/bin/uv")

    assert calls == ["Pillow"]
    assert report["status"] == "READY"
    assert report["requested_capabilities"]["lidar_registration"] is False


def test_missing_required_package_is_planned_for_locked_uv_repair(tmp_path):
    root = tmp_path / "repo"
    (root / ".venv").mkdir(parents=True)
    with patch.object(environment.sys, "prefix", str(root / ".venv")):
        report = environment.inspect_python_environment(root, lidar_registration=False,
                                                       package_probe=_probe(photo="MISSING"),
                                                       uv_executable="/usr/bin/uv")

    assert report["status"] == "READY_AFTER_BOOTSTRAP"
    assert report["bootstrap"]["command"] == ["/usr/bin/uv", "sync", "--locked", "--project",
                                                str(root.resolve()), "--python", environment.sys.executable,
                                                "--no-default-groups"]


def test_optional_lidar_dependency_plan_uses_declared_extra_only_when_requested(tmp_path):
    root = tmp_path / "repo"
    (root / ".venv").mkdir(parents=True)
    with patch.object(environment.sys, "prefix", str(root / ".venv")):
        report = environment.inspect_python_environment(root, lidar_registration=True,
                                                       package_probe=_probe(numpy="IMPORT_FAILED", open3d="MISSING"),
                                                       uv_executable="/usr/bin/uv")

    assert report["status"] == "READY_AFTER_BOOTSTRAP"
    assert report["required_missing"] == []
    assert {row["distribution"] for row in report["optional_registration_missing"]} == {"numpy", "open3d"}
    assert report["bootstrap"]["command"][-2:] == ["--extra", "lidar-registration"]


def test_optional_dependency_without_uv_retains_photo_only_readiness(tmp_path):
    root = tmp_path / "repo"
    (root / ".venv").mkdir(parents=True)
    with patch.object(environment.sys, "prefix", str(root / ".venv")), \
         patch.object(environment.shutil, "which", return_value=None):
        report = environment.inspect_python_environment(root, lidar_registration=True,
                                                       package_probe=_probe(open3d="MISSING"))

    assert report["status"] == "READY_WITH_OPTIONAL_WARNINGS"
    assert not report["blockers"]
    assert report["warnings"]


def test_native_import_failure_does_not_schedule_uv_package_repair(tmp_path):
    root = tmp_path / "repo"
    (root / ".venv").mkdir(parents=True)
    probe = _probe(open3d="IMPORT_FAILED")
    with patch.object(environment.sys, "prefix", str(root / ".venv")):
        report = environment.inspect_python_environment(root, lidar_registration=True,
                                                       package_probe=probe, uv_executable="/usr/bin/uv")

    assert report["status"] == "READY_WITH_OPTIONAL_WARNINGS"
    assert report["optional_registration_repair_available"] is False
    assert report["bootstrap"]["command"] is None
    assert "native-library" in report["warnings"][-1]


def test_required_repair_runs_locked_uv_and_revalidates(tmp_path):
    root = tmp_path / "repo"
    (root / ".venv").mkdir(parents=True)
    before = {"status": "READY_AFTER_BOOTSTRAP", "required_missing": [_package("Pillow", "MISSING")],
              "optional_registration_missing": [], "requested_capabilities": {"lidar_registration": False},
              "bootstrap": {"command": ["/usr/bin/uv", "sync", "--locked", "--project", str(root),
                                          "--python", environment.sys.executable, "--no-default-groups"]},
              "blockers": [], "warnings": []}
    after = {"status": "READY", "packages": [_package("Pillow")], "required_missing": [],
             "optional_registration_missing": [], "blockers": [], "warnings": []}
    runner = Mock(return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
    with patch.object(environment, "inspect_python_environment", return_value=after):
        report = environment.repair_python_environment(before, project_root=root,
            operator_initiated=True, log_directory=tmp_path / "logs", runner=runner)

    assert report["status"] == "READY"
    assert report["bootstrap"]["status"] == "REPAIRED_AND_VERIFIED"
    assert report["bootstrap"]["log_path"].endswith("uv-environment-sync.log")
    runner.assert_called_once()
    assert "--locked" in runner.call_args.args[0]
    assert runner.call_args.kwargs["timeout"] == 900


def test_repair_requires_explicit_local_operator_and_no_auto_install_is_honored(tmp_path):
    report = {"status": "READY_AFTER_BOOTSTRAP", "required_missing": [_package("Pillow", "MISSING")],
              "optional_registration_missing": [], "requested_capabilities": {"lidar_registration": False},
              "bootstrap": {"command": ["uv", "sync", "--locked"]}, "blockers": [], "warnings": []}
    runner = Mock()

    waiting = environment.repair_python_environment(report, project_root=tmp_path,
        operator_initiated=False, runner=runner)
    disabled = environment.repair_python_environment(report, project_root=tmp_path,
        operator_initiated=True, auto_install=False, runner=runner)

    assert waiting["status"] == "BLOCKED"
    assert "explicitly authorized" in waiting["blockers"][0]
    assert disabled["status"] == "BLOCKED"
    assert "--no-auto-install" in disabled["blockers"][0]
    runner.assert_not_called()


def test_repair_never_targets_an_unsupported_or_non_project_interpreter(tmp_path):
    report = {"status": "BLOCKED", "blockers": ["The CLI is running outside the project virtual environment."],
              "required_missing": [_package("Pillow", "MISSING")],
              "bootstrap": {"status": "REPAIR_REQUIRED", "command": ["uv", "sync", "--locked"]},
              "warnings": []}
    runner = Mock()

    result = environment.repair_python_environment(report, project_root=tmp_path,
        operator_initiated=True, runner=runner)

    assert result["status"] == "BLOCKED"
    assert result["bootstrap"]["status"] == "BLOCKED_BY_ENVIRONMENT"
    runner.assert_not_called()


def test_failed_optional_sync_keeps_photo_only_pipeline_available(tmp_path):
    report = {"status": "READY_AFTER_BOOTSTRAP", "required_missing": [],
              "optional_registration_missing": [_package("open3d", "MISSING")],
              "requested_capabilities": {"lidar_registration": True},
              "bootstrap": {"command": ["uv", "sync", "--locked", "--extra", "lidar-registration"]},
              "blockers": [], "warnings": []}
    runner = Mock(return_value=SimpleNamespace(returncode=1, stdout="resolution failed", stderr=""))

    result = environment.repair_python_environment(report, project_root=tmp_path,
        operator_initiated=True, runner=runner)

    assert result["status"] == "READY_WITH_OPTIONAL_WARNINGS"
    assert result["bootstrap"]["status"] == "COMMAND_FAILED"
    assert "photo-only" in result["warnings"][-1].casefold()


def test_incompatible_package_version_is_not_reported_ready(tmp_path):
    root = tmp_path / "repo"
    (root / ".venv").mkdir(parents=True)
    probe = lambda distribution, module, minimum=None: {
        **_package(distribution, version="9.0.0" if distribution == "Pillow" else "0.18.0"),
        "module": module,
        "status": "VERSION_INCOMPATIBLE" if distribution == "Pillow" else "AVAILABLE",
    }
    with patch.object(environment.sys, "prefix", str(root / ".venv")):
        report = environment.inspect_python_environment(root, lidar_registration=False,
                                                        package_probe=probe, uv_executable="/usr/bin/uv")

    assert report["required_missing"][0]["status"] == "VERSION_INCOMPATIBLE"
    assert report["status"] == "READY_AFTER_BOOTSTRAP"


def test_persist_environment_report_is_json_and_does_not_reinterpret_status(tmp_path):
    source = {"status": "READY_WITH_OPTIONAL_WARNINGS", "warnings": ["Open3D missing"]}
    target = environment.persist_environment_report(source, tmp_path / "environment.json")

    assert target.is_file()
    assert __import__("json").loads(target.read_text()) == source
