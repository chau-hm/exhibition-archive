"""Optional, read-only host observations for an Exhibition Archive plan."""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable


CommandRunner = Callable[..., subprocess.CompletedProcess]
ExecutableFinder = Callable[[str], str | None]


def _run_probe(argv: list[str], runner: CommandRunner) -> tuple[str, str | None]:
    try:
        result = runner(argv, capture_output=True, text=True, timeout=8, check=False)
    except Exception as exc:
        return "unknown", f"Probe could not complete: {type(exc).__name__}: {exc}"
    evidence = (result.stdout or "") + "\n" + (result.stderr or "")
    if result.returncode:
        return "unknown", f"Probe exited with status {result.returncode}: {(result.stderr or result.stdout or '').strip()[:240]}"
    return "available", evidence


def _probe_acceleration(system: str, runner: CommandRunner,
                        which: ExecutableFinder) -> dict[str, Any]:
    if system == "Darwin":
        try:
            executable = which("system_profiler") or "/usr/sbin/system_profiler"
        except Exception as exc:
            return {"status": "unknown", "probe": "system_profiler SPDisplaysDataType",
                    "reason": f"Executable lookup failed: {type(exc).__name__}: {exc}"}
        status, evidence = _run_probe([executable, "SPDisplaysDataType"], runner)
        if status != "available":
            return {"status": "unknown", "probe": "system_profiler SPDisplaysDataType", "reason": evidence}
        if re.search(r"(?im)^\s*Metal(?:\s+Support)?:\s*Supported(?:\b.*)?$", evidence):
            return {"status": "available", "probe": "system_profiler SPDisplaysDataType",
                    "evidence": "Metal support reported by macOS"}
        if re.search(r"(?im)^\s*Metal(?:\s+Support)?:\s*(?:Unsupported|Unavailable|Not Supported)(?:\b.*)?$", evidence):
            return {"status": "unavailable", "probe": "system_profiler SPDisplaysDataType",
                    "evidence": "macOS reports Metal unsupported"}
        return {"status": "unknown", "probe": "system_profiler SPDisplaysDataType",
                "reason": "The report did not contain a recognized Metal support result."}

    if system == "Linux":
        probe_failures: list[str] = []
        try:
            nvidia_smi = which("nvidia-smi")
            vulkaninfo = which("vulkaninfo")
        except Exception as exc:
            return {"status": "unknown", "probe": "NVIDIA/Vulkan inventory",
                    "reason": f"Executable lookup failed: {type(exc).__name__}: {exc}"}
        if nvidia_smi:
            status, evidence = _run_probe([nvidia_smi, "-L"], runner)
            if status == "available":
                devices = [line.strip() for line in evidence.splitlines()
                           if re.match(r"^GPU\s+\d+\s*:", line.strip())]
                if devices:
                    return {"status": "available", "probe": "nvidia-smi -L",
                            "evidence": f"{len(devices)} NVIDIA device(s) inventoried"}
                probe_failures.append("nvidia-smi completed but reported no NVIDIA devices")
            else:
                probe_failures.append(f"nvidia-smi: {evidence}")

        if vulkaninfo:
            status, evidence = _run_probe([vulkaninfo, "--summary"], runner)
            if status == "available":
                names = re.findall(r"(?im)^\s*deviceName\s*=\s*(.+?)\s*$", evidence)
                types = re.findall(r"(?im)^\s*deviceType\s*=\s*([^\s]+)", evidence)
                gpu_devices = [name for index, name in enumerate(names)
                               if index < len(types) and types[index] in {
                                   "PHYSICAL_DEVICE_TYPE_DISCRETE_GPU",
                                   "PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU",
                                   "PHYSICAL_DEVICE_TYPE_VIRTUAL_GPU",
                               }]
                if gpu_devices:
                    return {"status": "available", "probe": "vulkaninfo --summary",
                            "evidence": f"{len(gpu_devices)} non-CPU Vulkan device(s) inventoried"}
                if names and len(types) >= len(names) and all(
                        device_type == "PHYSICAL_DEVICE_TYPE_CPU" for device_type in types[:len(names)]):
                    return {"status": "unavailable", "probe": "vulkaninfo --summary",
                            "evidence": "Vulkan reported CPU devices only"}
                probe_failures.append("vulkaninfo completed without a recognized device inventory")
            else:
                probe_failures.append(f"vulkaninfo: {evidence}")

        reason = ("No supported inventory command was found." if not nvidia_smi and not vulkaninfo
                  else "; ".join(probe_failures))
        return {"status": "unknown", "probe": "NVIDIA/Vulkan inventory",
                "reason": reason}

    return {"status": "unknown", "probe": None,
            "reason": f"No safe accelerator inventory probe is configured for {system or 'this platform'}."}


def _disk_observation(location: str | Path) -> dict[str, Any]:
    requested = Path(location).expanduser()
    try:
        candidate = requested
        if candidate.exists() and candidate.is_file():
            candidate = candidate.parent
        while not candidate.exists() and candidate != candidate.parent:
            candidate = candidate.parent
        usage = shutil.disk_usage(candidate)
        resolved_candidate = candidate.resolve()
    except Exception as exc:
        return {"status": "unknown", "path": str(requested),
                "reason": f"Disk capacity could not be measured: {type(exc).__name__}: {exc}"}
    return {"status": "available", "path": str(resolved_candidate),
            "total_bytes": usage.total, "free_bytes": usage.free,
            "used_bytes": usage.used}


def collect_host_capabilities(output_location: str | Path, *,
                              training_required: bool,
                              backend_required: bool,
                              backend_status: str = "unknown",
                              python_environment: dict[str, Any] | None = None,
                              device: str | None = None,
                              runner: CommandRunner = subprocess.run,
                              which: ExecutableFinder = shutil.which,
                              system: str | None = None,
                              release: str | None = None,
                              machine: str | None = None) -> dict[str, Any]:
    """Collect observations without changing config or running a backend.

    Host inventory evidence is kept separate from the selected training
    backend's ability to initialize or use that device.
    """
    selected_system = system or platform.system()
    python_report = python_environment or {}
    engine_python_status = str(python_report.get("status", "UNKNOWN"))
    if not python_environment:
        python_status = "unknown"
    elif (python_report.get("blockers") or python_report.get("required_missing") or
          engine_python_status == "BLOCKED"):
        python_status = "unavailable"
    elif engine_python_status in {"READY", "READY_WITH_OPTIONAL_WARNINGS", "READY_AFTER_BOOTSTRAP"}:
        python_status = "available"
    else:
        python_status = "unknown"

    if backend_required:
        normalized_backend = str(backend_status).casefold()
        if normalized_backend in {"available", "ready", "ready_backend_dry_run"}:
            executable_status = "available"
        elif normalized_backend in {"unavailable", "unavailable_or_unsupported"}:
            executable_status = "unavailable"
        else:
            executable_status = "unknown"
        executable = {"status": executable_status, "name": "spirula"}
        if backend_status and normalized_backend not in {"available", "ready", "ready_backend_dry_run"}:
            executable["detail"] = backend_status
    else:
        executable = {"status": "not-required", "name": "spirula"}

    selected_device = str(device or "auto")
    if not training_required:
        acceleration = {"status": "not-required", "probe": None,
                        "reason": "The current plan does not require accelerator availability."}
        backend_device = {"status": "not-required", "device": selected_device}
    elif selected_device.casefold() == "cpu":
        acceleration = {"status": "not-required", "probe": None,
                        "reason": "The selected configuration requests CPU training."}
        backend_device = {
            "status": "unknown", "device": selected_device,
            "reason": "The selected training backend's CPU initialization has not been probed.",
        }
    else:
        acceleration = _probe_acceleration(selected_system, runner, which)
        backend_device = {
            "status": "unknown", "device": selected_device,
            "reason": "Host inventory does not verify that the selected training backend can initialize this device.",
        }

    optional_registration = "unknown"
    optional_rows = python_report.get("optional_registration_missing", [])
    requested_capabilities = python_report.get("requested_capabilities", {})
    if requested_capabilities.get("lidar_registration") is False:
        optional_registration = "not-required"
    elif requested_capabilities.get("lidar_registration") is True:
        optional_registration = "unavailable" if optional_rows else "available"
    elif optional_rows:
        optional_registration = "unavailable"

    return {
        "schema_version": "exhibition-archive.host-capabilities.v1",
        "informational": True,
        "platform": {"system": selected_system, "release": release or platform.release(),
                     "architecture": machine or platform.machine()},
        "python_environment": {"status": python_status, "engine_report_status": engine_python_status},
        "required_executables": {"spirula": executable},
        "acceleration": acceleration,
        "selected_backend_device": backend_device,
        "optional_capabilities": {"lidar_registration": optional_registration},
        "disk_capacity": _disk_observation(output_location),
        "execution_blocked_by_this_report": False,
    }
