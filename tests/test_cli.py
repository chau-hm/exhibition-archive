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


if __name__ == "__main__":
    unittest.main()
