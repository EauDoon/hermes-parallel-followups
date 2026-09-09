"""Portable lifecycle validation using disposable sources, never installed Hermes."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_patch_installers import INSTALLERS, ROOT, string_constants, unpatched_source


def invoke(script, target, *options):
    return subprocess.run([sys.executable, str(script), str(target), *options],
                          capture_output=True, text=True, check=False)


class PatchWorkflowTests(unittest.TestCase):
    def test_check_apply_check_has_no_check_side_effects(self):
        for name, old, marker, suffix in INSTALLERS:
            with self.subTest(installer=name), tempfile.TemporaryDirectory() as td:
                script = ROOT / "patches" / name
                target = Path(td) / "target.py"
                original = unpatched_source(string_constants(script), old, marker).encode()
                target.write_bytes(original)
                check = invoke(script, target, "--check")
                self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
                self.assertEqual(check.stdout.strip(), "APPLICABLE")
                self.assertEqual(list(Path(td).iterdir()), [target])
                self.assertEqual(target.read_bytes(), original)
                applied = invoke(script, target)
                self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
                installed = target.read_bytes()
                files = set(Path(td).iterdir())
                check = invoke(script, target, "--check")
                self.assertEqual(check.stdout.strip(), "ALREADY_PATCHED")
                self.assertEqual(target.read_bytes(), installed)
                self.assertEqual(set(Path(td).iterdir()), files)

    def test_check_rejects_incompatible_input_without_writes(self):
        for name, _, _, _ in INSTALLERS:
            with self.subTest(installer=name), tempfile.TemporaryDirectory() as td:
                target = Path(td) / "target.py"
                target.write_text("# unsupported source\n")
                result = invoke(ROOT / "patches" / name, target, "--check")
                self.assertEqual(result.returncode, 2)
                self.assertEqual(list(Path(td).iterdir()), [target])


if __name__ == "__main__":
    unittest.main()
