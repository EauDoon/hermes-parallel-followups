"""Portable lifecycle validation using disposable sources, never installed Hermes."""
from pathlib import Path
import contextlib
import io
import py_compile
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

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

    def test_apply_reverse_reapply_preserves_bytes_and_backups(self):
        for name, old, marker, suffix in INSTALLERS:
            for ending in ("\n", "\r\n"):
                with self.subTest(installer=name, ending=repr(ending)), tempfile.TemporaryDirectory() as td:
                    script = ROOT / "patches" / name
                    target = Path(td) / "target.py"
                    original = unpatched_source(string_constants(script), old, marker).replace("\n", ending).encode()
                    target.write_bytes(original)
                    for options, expected in (((), "PATCHED_OK"), (("--check", "--reverse"), "REVERSIBLE"),
                                              (("--reverse",), "REVERSED_OK"), (("--reverse",), "ALREADY_UNPATCHED"),
                                              ((), "PATCHED_OK")):
                        before = target.read_bytes()
                        result = invoke(script, target, *options)
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertEqual(result.stdout.strip(), expected)
                        if expected in ("REVERSED_OK", "ALREADY_UNPATCHED"):
                            self.assertEqual(target.read_bytes(), original)
                        if expected == "REVERSIBLE":
                            self.assertEqual(target.read_bytes(), before)
                    self.assertEqual(Path(str(target) + suffix).read_bytes(), original)
                    self.assertEqual(Path(str(target) + suffix + ".reverse").read_bytes(), target.read_bytes())

    def test_reverse_preserves_unrelated_edits(self):
        for name, old, marker, _ in INSTALLERS:
            with self.subTest(installer=name), tempfile.TemporaryDirectory() as td:
                script = ROOT / "patches" / name
                target = Path(td) / "target.py"
                original = unpatched_source(string_constants(script), old, marker)
                target.write_text(original)
                self.assertEqual(invoke(script, target).returncode, 0)
                with target.open("a") as file:
                    file.write("\n# unrelated operator edit\n")
                result = invoke(script, target, "--reverse")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(target.read_text(), original + "\n# unrelated operator edit\n")

    def test_concurrent_edit_during_compilation_is_preserved(self):
        compiler = py_compile.compile
        for name, old, marker, suffix in INSTALLERS:
            with self.subTest(installer=name), tempfile.TemporaryDirectory() as td:
                script = ROOT / "patches" / name
                target = Path(td) / "target.py"
                original = unpatched_source(string_constants(script), old, marker)
                target.write_text(original)
                changed = original + "\n# concurrently edited\n"
                def concurrent_compile(*args, **kwargs):
                    result = compiler(*args, **kwargs)
                    target.write_text(changed)
                    return result
                output = io.StringIO()
                with patch.object(sys, "argv", [str(script), str(target)]), \
                        patch("py_compile.compile", side_effect=concurrent_compile), \
                        contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as raised:
                    runpy.run_path(str(script), run_name="__main__")
                self.assertEqual(raised.exception.code, 3)
                self.assertIn("target changed", output.getvalue())
                self.assertEqual(target.read_text(), changed)
                self.assertFalse(Path(str(target) + suffix).exists())
                self.assertFalse(list(Path(td).glob(".target.py.*.tmp*")))


if __name__ == "__main__":
    unittest.main()
