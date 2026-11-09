from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "bin" / "exhibition-archive"


class CliSmokeTests(unittest.TestCase):
    def test_version_from_unrelated_working_directory(self):
        result = subprocess.run(
            [str(LAUNCHER), "--version"],
            cwd="/private/tmp",
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=True,
        )
        self.assertEqual(result.stdout.strip(), "exhibition-archive 0.1.0")

    def test_sfm_review_cli_exposes_scoped_decisions_and_limitations(self):
        result = subprocess.run(
            [str(LAUNCHER), "sfm-review", "--help"],
            cwd="/private/tmp",
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=True,
        )
        self.assertIn("accept-experimental-training", result.stdout)
        self.assertIn("--component", result.stdout)
        self.assertIn("--limitation", result.stdout)

    def test_sfm_revalidation_and_geometry_proposal_review_commands_are_available(self):
        revalidate = subprocess.run(
            [str(LAUNCHER), "sfm-revalidate", "--help"], cwd="/private/tmp",
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=True)
        geometry = subprocess.run(
            [str(LAUNCHER), "sfm-geometry-review", "--help"], cwd="/private/tmp",
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=True)
        self.assertIn("Read-only revalidation", revalidate.stdout)
        self.assertIn("an unapproved navigation proposal", geometry.stdout)
        self.assertIn("--limitation", geometry.stdout)

    def test_storage_report_cli_is_available(self):
        result = subprocess.run(
            [str(LAUNCHER), "storage-report", "--help"],
            cwd="/private/tmp",
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=True,
        )
        self.assertIn("usage: exhibition-archive storage-report", result.stdout)

    def test_external_sog_import_cli_exposes_provenance_inputs(self):
        result = subprocess.run(
            [str(LAUNCHER), "sog-import", "--help"],
            cwd="/private/tmp",
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=True,
        )
        self.assertIn("--converter-version", result.stdout)
        self.assertIn("--conversion-command", result.stdout)
        self.assertIn("--gpu-selection", result.stdout)
        self.assertIn("--sh-iterations", result.stdout)
        self.assertIn("--execution-metadata-json", result.stdout)

    def test_interrupted_execution_reconciliation_requires_explicit_process_confirmation(self):
        result = subprocess.run(
            [str(LAUNCHER), "reconcile-interrupted", "--help"],
            cwd="/private/tmp",
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=True,
        )
        self.assertIn("--run-id", result.stdout)
        self.assertIn("--execution-id", result.stdout)
        self.assertIn("--exit-code", result.stdout)
        self.assertIn("--evidence", result.stdout)
        self.assertIn("--confirm-no-active-processes", result.stdout)

    def test_training_parameter_profile_and_host_preflight_have_distinct_cli_flags(self):
        run = subprocess.run([str(LAUNCHER), "run", "--help"], cwd="/private/tmp",
                             text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=True)
        plan = subprocess.run([str(LAUNCHER), "plan", "--help"], cwd="/private/tmp",
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=True)
        resume = subprocess.run([str(LAUNCHER), "resume", "--help"], cwd="/private/tmp",
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=True)
        self.assertIn("--profile {first-visual}", run.stdout)
        self.assertIn("--training-profile", run.stdout)
        self.assertIn("--legacy-compatible-training", run.stdout)
        self.assertIn("id@revision", run.stdout)
        self.assertIn("--host-preflight", run.stdout)
        self.assertIn("--host-preflight", plan.stdout)
        self.assertIn("--host-preflight", resume.stdout)
        self.assertIn("--legacy-compatible-training", resume.stdout)


if __name__ == "__main__":
    unittest.main()
