#!/usr/bin/env python3
"""The installed-Hermes checks must refuse cleanly when no Hermes is present.

tests/test_debounce_fifo.py and tests/test_burst_fullpath.py import a real,
patched gateway, so the offline suite never runs them against one. This
proves they still start, honour HERMES_ROOT, and stop with an actionable
message instead of an ImportError traceback. Each run points HERMES_ROOT at
an empty temporary directory, never at an installed Hermes.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from run_offline import NEEDS_INSTALLED_HERMES


HERE = Path(__file__).resolve().parent


class InstalledHermesGuardTests(unittest.TestCase):
    def test_missing_hermes_is_reported_without_a_traceback(self):
        self.assertEqual(set(NEEDS_INSTALLED_HERMES), {"test_debounce_fifo.py", "test_burst_fullpath.py"})
        for name in NEEDS_INSTALLED_HERMES:
            with self.subTest(script=name), tempfile.TemporaryDirectory() as td:
                environment = {**os.environ, "HERMES_ROOT": td}
                environment.pop("PYTHONPATH", None)
                result = subprocess.run([sys.executable, str(HERE / name)], capture_output=True, text=True,
                                        env=environment, check=False, timeout=30)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("REQUIRES_HERMES: cannot import the gateway from " + td, result.stderr)
                self.assertIn("set HERMES_ROOT", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(list(Path(td).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
