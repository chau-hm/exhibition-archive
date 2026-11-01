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


if __name__ == "__main__":
    unittest.main()
