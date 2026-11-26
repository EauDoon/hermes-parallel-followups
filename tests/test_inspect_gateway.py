#!/usr/bin/env python3
"""Exercise the public read-only inspection command with independent targets."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_installer_status import DEBOUNCE, ROUTER, ROOT, unpatched


class InspectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "Hermes ünicode"
        self.base = self.root / "gateway" / "platforms" / "base.py"
        self.runner = self.root / "gateway" / "run.py"
        self.base.parent.mkdir(parents=True)
        self.base.write_text(unpatched(DEBOUNCE), encoding="utf-8")
        self.runner.write_text(unpatched(ROUTER), encoding="utf-8")

    def inspect(self, *arguments):
        return subprocess.run([sys.executable, str(ROOT / "tools" / "inspect_gateway.py"),
                               str(self.root), *arguments], capture_output=True,
                              text=True, check=False)

    def snapshot(self):
        return {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}

    def test_read_only_modes_report_both_targets(self):
        before = self.snapshot()
        for mode, expected in (("status", "STATUS UNPATCHED"),
                               ("install-check", "APPLICABLE"),
                               ("reverse-check", "ALREADY_UNPATCHED")):
            with self.subTest(mode=mode):
                result = self.inspect("--mode", mode, "--json")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                report = json.loads(result.stdout)
                self.assertEqual(report["mode"], mode)
                self.assertEqual(report["schema_version"], 1)
                targets = report["targets"]
                self.assertEqual([t["patch"] for t in targets], ["debounce", "router"])
                self.assertEqual([t["result"] for t in targets], [expected, expected])
                self.assertEqual(self.snapshot(), before)
        self.assertIn("[router] gateway/run.py (exit 0)", self.inspect().stdout)

    def test_one_refusal_does_not_hide_the_other_target(self):
        self.base.unlink()
        result = self.inspect("--mode", "install-check", "--json")
        first, second = json.loads(result.stdout)["targets"]
        self.assertEqual(result.returncode, 2)
        self.assertEqual(first["exit_code"], 2)
        self.assertTrue(first["result"].startswith("ABORT"))
        self.assertEqual(second["result"], "APPLICABLE")
        self.assertFalse(self.base.exists())


if __name__ == "__main__":
    unittest.main()
