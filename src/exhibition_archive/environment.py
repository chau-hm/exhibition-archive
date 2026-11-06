"""Read-only dependency inspection and locked uv repair for local CLI runs."""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from .adapters import write_json


def _version_tuple(value: str) -> tuple[int, ...]:
    parts = []
    for item in value.split("."):
        digits = "".join(char for char in item if char.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def _package_probe(distribution: str, module: str, minimum: tuple[int, ...] | None = None) -> dict[str, Any]:
    try:
        version = importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return {"distribution": distribution, "module": module, "status": "MISSING", "version": None}
    try:
        proc = subprocess.run([sys.executable, "-c", f"import {module}"], capture_output=True,
                              text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"distribution": distribution, "module": module, "status": "IMPORT_FAILED",
                "version": version, "error": f"{type(exc).__name__}: {exc}"}
    if proc.returncode:
        return {"distribution": distribution, "module": module, "status": "IMPORT_FAILED",
                "version": version, "error": ((proc.stderr or proc.stdout).strip()[-2000:])}
    if minimum and _version_tuple(version) < minimum:
        return {"distribution": distribution, "module": module, "status": "VERSION_INCOMPATIBLE",
                "version": version, "minimum_version": ".".join(map(str, minimum))}
    return {"distribution": distribution, "module": module, "status": "AVAILABLE", "version": version}


def inspect_python_environment(project_root: str | Path, *, lidar_registration: bool,
                               package_probe: Callable[..., dict[str, Any]] = _package_probe,
                               uv_executable: str | None = None) -> dict[str, Any]:
    """Inspect the exact interpreter running the CLI; this function never installs."""
    root = Path(project_root).expanduser().resolve()
    expected_venv = (root / ".venv").resolve()
    active_prefix = Path(sys.prefix).resolve()
    project_venv = active_prefix == expected_venv
    packages = [package_probe("Pillow", "PIL", (10, 0))]
    if lidar_registration:
        packages.extend((package_probe("numpy", "numpy", (1, 18)),
                         package_probe("open3d", "open3d", (0, 20))))
        open3d = next(row for row in packages if row["distribution"] == "open3d")
        if open3d["status"] == "AVAILABLE" and _version_tuple(open3d["version"]) >= (0, 21):
            open3d.update({"status": "VERSION_INCOMPATIBLE", "maximum_exclusive_version": "0.21"})
    missing_required = [row for row in packages if row["distribution"] == "Pillow" and row["status"] != "AVAILABLE"]
    missing_optional = [row for row in packages if row["distribution"] != "Pillow" and row["status"] != "AVAILABLE"]
    repairable_optional = [row for row in missing_optional
                          if row["status"] in {"MISSING", "VERSION_INCOMPATIBLE"}]
    import_failed_optional = [row for row in missing_optional if row["status"] == "IMPORT_FAILED"]
    uv = uv_executable if uv_executable is not None else shutil.which("uv")
    report: dict[str, Any] = {
        "schema_version": "exhibition-archive.python-environment.v1",
        "python": {"executable": str(Path(sys.executable).resolve()),
                   "version": platform.python_version(), "prefix": str(active_prefix),
                   "expected_project_venv": str(expected_venv),
                   "project_local_venv": project_venv},
        "uv": {"executable": str(Path(uv).resolve()) if uv else None,
               "status": "AVAILABLE" if uv else "MISSING"},
        "packages": packages,
        "required_missing": missing_required,
        "optional_registration_missing": missing_optional,
        "optional_registration_repair_available": bool(repairable_optional and uv),
        "requested_capabilities": {"photo_pipeline": True, "lidar_registration": lidar_registration},
        "bootstrap": {"status": "NOT_NEEDED", "command": None},
        "blockers": [], "warnings": [],
    }
    if sys.version_info < (3, 11):
        report["blockers"].append("Python 3.11 or newer is required.")
    if not project_venv:
        report["blockers"].append(f"The CLI is running outside the project virtual environment. Run {expected_venv / 'bin' / 'exhibition-archive'} or use uv run --project {root}.")
    if missing_required:
        if uv:
            report["bootstrap"].update({"status": "REPAIR_REQUIRED", "command": [uv, "sync", "--locked", "--project", str(root), "--python", sys.executable, "--no-default-groups"]})
        else:
            report["blockers"].append("Required Pillow dependency is missing or incompatible, and uv is unavailable. Install uv, then rerun the Engine from its project .venv.")
    if repairable_optional:
        if uv:
            report["bootstrap"].update({"status": "OPTIONAL_REPAIR_AVAILABLE", "command": [uv, "sync", "--locked", "--project", str(root), "--python", sys.executable, "--no-default-groups", "--extra", "lidar-registration"]})
            report["warnings"].append("Optional LiDAR registration dependencies need repair; a local authorized run can install the locked extra, otherwise the photo-only pathway remains available.")
        else:
            report["warnings"].append("Optional LiDAR registration dependencies are unavailable and uv is not installed; the photo-only pathway remains available.")
    if import_failed_optional:
        names = ", ".join(row["distribution"] for row in import_failed_optional)
        report["warnings"].append(
            f"Optional LiDAR package import failed for {names}; a uv package sync cannot reliably repair native-library or import errors. "
            "Resolve the reported import diagnostic separately or continue with photo-only processing.")
    if not missing_required and not missing_optional and not report["blockers"]:
        report["status"] = "READY"
    elif missing_required and uv and project_venv and not [b for b in report["blockers"] if "Python" in b]:
        report["status"] = "READY_AFTER_BOOTSTRAP"
    elif repairable_optional and not missing_required and not report["blockers"]:
        report["status"] = "READY_AFTER_BOOTSTRAP" if uv else "READY_WITH_OPTIONAL_WARNINGS"
    elif import_failed_optional and not missing_required and not report["blockers"]:
        report["status"] = "READY_WITH_OPTIONAL_WARNINGS"
    else:
        report["status"] = "BLOCKED"
    return report


def repair_python_environment(report: dict[str, Any], *, project_root: str | Path,
                              operator_initiated: bool, auto_install: bool = True,
                              log_directory: str | Path | None = None,
                              runner: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    """Run only the repository's locked uv sync after deliberate local opt-in."""
    result = json.loads(json.dumps(report))
    if result.get("status") == "READY":
        return result
    command = result.get("bootstrap", {}).get("command")
    if not command:
        return result
    if result.get("blockers"):
        result["bootstrap"]["status"] = "BLOCKED_BY_ENVIRONMENT"
        result["status"] = "BLOCKED"
        return result
    required_missing = bool(result.get("required_missing"))
    if not auto_install:
        if required_missing:
            result["blockers"].append("Automatic environment repair is disabled (--no-auto-install). Run the recorded uv sync command explicitly.")
            result["status"] = "BLOCKED"
        else:
            result["bootstrap"]["status"] = "SKIPPED_BY_OPERATOR"
            result["status"] = "READY_WITH_OPTIONAL_WARNINGS"
        return result
    if not operator_initiated:
        result["bootstrap"]["status"] = "WAITING_FOR_EXPLICIT_LOCAL_OPERATOR_RUN"
        if required_missing:
            result["blockers"].append("Missing required dependencies can be repaired only in an explicitly authorized interactive local run. Re-run with --interactive-authorization from the project .venv, or use the recorded uv sync command.")
            result["status"] = "BLOCKED"
        else:
            result["status"] = "READY_WITH_OPTIONAL_WARNINGS"
        return result
    log_path = None
    if log_directory is not None:
        logs = Path(log_directory)
        logs.mkdir(parents=True, exist_ok=True)
        log_path = logs / "uv-environment-sync.log"
        if log_path.exists():
            # One run may repair both required and optional dependencies. Keep
            # each invocation traceable rather than truncating earlier evidence.
            suffix = 2
            while (logs / f"uv-environment-sync-{suffix:02d}.log").exists():
                suffix += 1
            log_path = logs / f"uv-environment-sync-{suffix:02d}.log"
    try:
        if log_path:
            with log_path.open("x", encoding="utf-8") as stream:
                completed = runner(command, cwd=Path(project_root).resolve(), stdout=stream,
                                   stderr=subprocess.STDOUT, text=True, timeout=900, check=False)
        else:
            completed = runner(command, cwd=Path(project_root).resolve(), capture_output=True,
                               text=True, timeout=900, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        result["bootstrap"].update({"status": "FAILED", "error": f"{type(exc).__name__}: {exc}",
                                    "log_path": str(log_path) if log_path else None})
        if required_missing:
            result["blockers"].append(f"uv could not repair the required environment: {exc}")
            result["status"] = "BLOCKED"
        else:
            result["warnings"].append(f"Optional LiDAR dependency installation failed: {exc}")
            result["status"] = "READY_WITH_OPTIONAL_WARNINGS"
        return result
    output = (getattr(completed, "stdout", "") or "") + (getattr(completed, "stderr", "") or "")
    if log_path is None and output:
        result["bootstrap"]["output_tail"] = output[-4000:]
    result["bootstrap"].update({"status": "COMMAND_SUCCEEDED" if completed.returncode == 0 else "COMMAND_FAILED",
                                "exit_code": completed.returncode,
                                "command": command, "log_path": str(log_path) if log_path else None})
    if completed.returncode != 0:
        message = f"Locked uv sync exited with code {completed.returncode}."
        if required_missing:
            result["blockers"].append(message)
            result["status"] = "BLOCKED"
        else:
            result["warnings"].append(message + " Photo-only execution remains available.")
            result["status"] = "READY_WITH_OPTIONAL_WARNINGS"
        return result
    checked = inspect_python_environment(project_root,
        lidar_registration=bool(result.get("requested_capabilities", {}).get("lidar_registration")))
    result["packages"] = checked["packages"]
    result["required_missing"] = checked["required_missing"]
    result["optional_registration_missing"] = checked["optional_registration_missing"]
    result["bootstrap"]["post_install_validation"] = checked["status"]
    if checked["required_missing"] or checked["blockers"]:
        result["blockers"].extend(checked["blockers"] or ["Required Python dependencies remain unavailable after locked uv sync."])
        result["status"] = "BLOCKED"
        result["bootstrap"]["status"] = "POST_INSTALL_VALIDATION_FAILED"
    elif checked["optional_registration_missing"]:
        result["warnings"].append("Open3D registration remains unavailable; continuing photo-only where supported.")
        result["status"] = "READY_WITH_OPTIONAL_WARNINGS"
        result["bootstrap"]["status"] = "OPTIONAL_REPAIR_INCOMPLETE"
    else:
        result["status"] = "READY"
        result["bootstrap"]["status"] = "REPAIRED_AND_VERIFIED"
    if log_path:
        result["bootstrap"]["log_path"] = str(log_path.resolve())
    return result


def persist_environment_report(report: dict[str, Any], path: str | Path) -> Path:
    target = Path(path)
    write_json(target, report)
    return target
