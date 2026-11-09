from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from exhibition_archive.cli import _collect_host_capabilities_safe, _host_capabilities_for_plan
from exhibition_archive.host_capabilities import collect_host_capabilities


class HostCapabilityTests(unittest.TestCase):
    def test_darwin_metal_inventory_is_available_but_backend_initialization_stays_unknown(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0,
                                               "Metal Support: Supported, feature set macOS GPUFamily2 v1\n", "")

        report = collect_host_capabilities("/tmp/output", training_required=True,
                                           backend_required=True, backend_status="available",
                                           runner=runner, which=lambda _: None, system="Darwin",
                                           release="test-release", machine="arm64")
        self.assertEqual(report["acceleration"]["status"], "available")
        self.assertEqual(report["selected_backend_device"]["status"], "unknown")
        self.assertEqual(report["required_executables"]["spirula"]["status"], "available")
        self.assertEqual(calls, [["/usr/sbin/system_profiler", "SPDisplaysDataType"]])

    def test_linux_nvidia_inventory_confirms_a_host_device(self):
        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, "GPU 0: Test Adapter (UUID: GPU-test)\n", "")

        report = collect_host_capabilities("/tmp/output", training_required=True,
                                           backend_required=True, backend_status="unknown",
                                           runner=runner, which=lambda name: "/usr/bin/nvidia-smi" if name == "nvidia-smi" else None,
                                           system="Linux")
        self.assertEqual(report["acceleration"]["status"], "available")
        self.assertEqual(report["selected_backend_device"]["status"], "unknown")

    def test_linux_vulkan_inventory_requires_a_recognized_gpu_device_type(self):
        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(
                argv, 0,
                "deviceName = Integrated Adapter\ndeviceType = PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU\n", "")

        report = collect_host_capabilities(
            "/tmp/output", training_required=True, backend_required=True,
            runner=runner, which=lambda name: "/usr/bin/vulkaninfo" if name == "vulkaninfo" else None,
            system="Linux")
        self.assertEqual(report["acceleration"]["status"], "available")
        self.assertEqual(report["acceleration"]["probe"], "vulkaninfo --summary")

    def test_backend_machine_status_and_reason_are_kept_separate(self):
        report = collect_host_capabilities(
            "/tmp/output", training_required=True, backend_required=True,
            backend_status="UNAVAILABLE_OR_UNSUPPORTED",
            backend_reason="Selected profile flag is unsupported by this Spirula version.")
        self.assertEqual(report["required_executables"]["spirula"]["status"], "unavailable")
        self.assertEqual(report["required_executables"]["spirula"]["detail"],
                         "Selected profile flag is unsupported by this Spirula version.")

    def test_missing_selected_backend_is_reported_without_blocking_host_preflight(self):
        report = collect_host_capabilities("/tmp/output", training_required=True,
                                           backend_required=True, backend_status="unavailable")
        self.assertEqual(report["required_executables"]["spirula"]["status"], "unavailable")
        self.assertFalse(report["execution_blocked_by_this_report"])

    def test_missing_probe_is_unknown_not_unavailable(self):
        report = collect_host_capabilities("/tmp/output", training_required=True,
                                           backend_required=True, which=lambda _: None,
                                           system="Linux")
        self.assertEqual(report["acceleration"]["status"], "unknown")
        self.assertIn("No supported inventory command", report["acceleration"]["reason"])

    def test_explicitly_unsupported_macos_acceleration_is_unavailable(self):
        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, "Metal Support: Unsupported\n", "")

        report = collect_host_capabilities("/tmp/output", training_required=True,
                                           backend_required=True, runner=runner,
                                           which=lambda _: "/usr/bin/system_profiler",
                                           system="Darwin")
        self.assertEqual(report["acceleration"]["status"], "unavailable")

    def test_probe_failure_is_reported_as_unknown_without_raising(self):
        def runner(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 8)

        report = collect_host_capabilities("/tmp/output", training_required=True,
                                           backend_required=True, runner=runner,
                                           which=lambda _: "/usr/bin/system_profiler",
                                           system="Darwin")
        self.assertEqual(report["acceleration"]["status"], "unknown")
        self.assertIn("TimeoutExpired", report["acceleration"]["reason"])

    def test_nonzero_probe_exit_remains_unknown(self):
        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1, "", "access denied")

        report = collect_host_capabilities("/tmp/output", training_required=True,
                                           backend_required=True, runner=runner,
                                           which=lambda _: "/usr/bin/system_profiler",
                                           system="Darwin")
        self.assertEqual(report["acceleration"]["status"], "unknown")
        self.assertIn("access denied", report["acceleration"]["reason"])

    def test_acceleration_is_not_required_when_training_is_not_planned(self):
        def unexpected_runner(*args, **kwargs):
            raise AssertionError("no accelerator probe should run")

        report = collect_host_capabilities("/tmp/output", training_required=False,
                                           backend_required=False, runner=unexpected_runner)
        self.assertEqual(report["acceleration"]["status"], "not-required")
        self.assertEqual(report["required_executables"]["spirula"]["status"], "not-required")

    def test_cpu_selection_does_not_probe_gpu_or_claim_backend_compatibility(self):
        def unexpected_runner(*args, **kwargs):
            raise AssertionError("CPU training should not probe for a GPU")

        report = collect_host_capabilities("/tmp/output", training_required=True,
                                           backend_required=True, device="cpu",
                                           runner=unexpected_runner)
        self.assertEqual(report["acceleration"]["status"], "not-required")
        self.assertEqual(report["selected_backend_device"]["status"], "unknown")

    def test_disk_measurement_is_reported_or_unknown_and_never_blocks(self):
        with tempfile.TemporaryDirectory() as temporary:
            before = sorted(Path(temporary).iterdir())
            report = collect_host_capabilities(temporary, training_required=False,
                                               backend_required=False)
            self.assertEqual(report["disk_capacity"]["status"], "available")
            self.assertGreaterEqual(report["disk_capacity"]["free_bytes"], 0)
            self.assertEqual(before, sorted(Path(temporary).iterdir()))
            self.assertFalse(report["execution_blocked_by_this_report"])

        with patch("exhibition_archive.host_capabilities.shutil.disk_usage",
                   side_effect=OSError("measurement denied")):
            report = collect_host_capabilities("/tmp/output", training_required=False,
                                               backend_required=False)
        self.assertEqual(report["disk_capacity"]["status"], "unknown")
        self.assertFalse(report["execution_blocked_by_this_report"])

    def test_optional_capability_warning_does_not_block_or_change_python_status(self):
        report = collect_host_capabilities(
            "/tmp/output", training_required=False, backend_required=False,
            python_environment={"status": "READY_WITH_OPTIONAL_WARNINGS", "blockers": [],
                                "optional_registration_missing": [{"distribution": "open3d"}]})
        self.assertEqual(report["optional_capabilities"]["lidar_registration"], "unavailable")
        self.assertEqual(report["python_environment"]["status"], "available")
        self.assertFalse(report["execution_blocked_by_this_report"])

    def test_cli_wrapper_keeps_host_probe_exceptions_non_blocking(self):
        with patch("exhibition_archive.host_capabilities.collect_host_capabilities",
                   side_effect=RuntimeError("fixture probe failure")):
            report = _collect_host_capabilities_safe(
                "/tmp/output", training_required=True, backend_required=True)
        self.assertEqual(report["acceleration"]["status"], "unknown")
        self.assertIn("fixture probe failure", report["preflight_error"])
        self.assertFalse(report["execution_blocked_by_this_report"])

    def test_preflight_requirements_follow_pending_fresh_stages_in_the_plan(self):
        root = Path("/tmp/host-plan-fixture")
        config = {"output_directory": "output", "integrations": {"model": {"device": "auto"}}}
        rows = [{"id": "sfm", "execution_mode": "SPIRULA_FRESH", "current_status": "PENDING",
                 "backend_probe_status": "available"},
                {"id": "training", "execution_mode": "SPIRULA_FRESH", "current_status": "INVALIDATED",
                 "backend_probe_status": "available"}]
        with patch("exhibition_archive.cli._collect_host_capabilities_safe", return_value={}) as collect:
            _host_capabilities_for_plan(root, config, rows, {"status": "READY"})
        self.assertEqual(collect.call_args.args[0], (root / "output").resolve())
        self.assertTrue(collect.call_args.kwargs["backend_required"])
        self.assertTrue(collect.call_args.kwargs["training_required"])
        self.assertEqual(collect.call_args.kwargs["backend_status"], "available")

        completed = [{"id": "training", "execution_mode": "SPIRULA_FRESH", "current_status": "COMPLETED",
                      "backend_probe_status": "available"}]
        with patch("exhibition_archive.cli._collect_host_capabilities_safe", return_value={}) as collect:
            _host_capabilities_for_plan(root, config, completed, {"status": "READY"})
        self.assertFalse(collect.call_args.kwargs["backend_required"])
        self.assertFalse(collect.call_args.kwargs["training_required"])
        self.assertEqual(collect.call_args.kwargs["backend_status"], "not-required")


if __name__ == "__main__":
    unittest.main()
