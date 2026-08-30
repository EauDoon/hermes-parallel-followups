#!/usr/bin/env python3
"""Regression checks for patch-installer idempotency detection."""

import ast
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
INSTALLERS = (
    (
        "apply_debounce_fifo_patch.py",
        "OLD",
        "_queue_or_replace_pending_event",
        ".bak-pre-debouncefifo",
    ),
    (
        "apply_busy_overflow_router_patch.py",
        "HOOK_OLD",
        "_maybe_route_overflow_to_background",
        ".bak-pre-overflowrouter",
    ),
)


def string_constants(script: Path):
    tree = ast.parse(script.read_text(encoding="utf-8"))
    return {
        target.id: ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance((target := node.targets[0]), ast.Name)
        and isinstance(node.value, (ast.Constant, ast.Str))
    }


def unpatched_source(constants, old_name, old_marker):
    if old_name == "OLD":
        return (
            f"# {old_marker} is supplied by the runner\n"
            "class Fixture:\n"
            "    def flush(self, store, session_key):\n"
            + constants[old_name]
        )
    # In gateway/run.py the busy-handler anchor precedes the later queue-mode hook.
    return (
        "# fixture\nimport re\nimport os\nimport time\nimport asyncio\n\n"
        f"# {old_marker} is described in the release notes\n"
        "class Fixture:\n"
        + constants["ANCHOR"]
        + "\n        pass\n\n    async def route(self, event, session_key):\n"
        + constants[old_name]
    )


def crlf_bytes(text):
    return text.replace("\n", "\r\n").encode("utf-8")


def assert_crlf_only(testcase, value):
    testcase.assertIn(b"\r\n", value)
    without_valid_pairs = value.replace(b"\r\n", b"")
    testcase.assertNotIn(b"\r", without_valid_pairs)
    testcase.assertNotIn(b"\n", without_valid_pairs)


def assert_no_staging_residue(testcase, directory, target):
    testcase.assertFalse(list(directory.glob(f".{target.name}.*.tmp*")))


class PatchInstallerTests(unittest.TestCase):
    def test_current_router_install_rejects_malformed_structure(self):
        script = ROOT / "patches" / "apply_busy_overflow_router_patch.py"
        constants = string_constants(script)
        source = unpatched_source(
            constants, "HOOK_OLD", "_maybe_route_overflow_to_background",
        )
        installed = source.replace(constants["HOOK_OLD"], constants["HOOK_NEW"], 1)
        installed = installed.replace(
            constants["ANCHOR"], constants["BLOCK"] + constants["ANCHOR"], 1,
        )

        with tempfile.TemporaryDirectory() as td:
            directory = Path(td)
            environment = {
                **os.environ,
                "PYTHONPYCACHEPREFIX": str(directory / "pycache"),
            }
            cases = {
                "duplicate-anchor": installed + "\n" + constants["ANCHOR"],
                "missing-anchor": installed.replace(constants["ANCHOR"], "", 1),
                "missing-import": installed.replace("\nimport re\n", "\n", 1),
            }

            for name, malformed in cases.items():
                with self.subTest(case=name):
                    target = directory / f"{name}.py"
                    original = crlf_bytes(malformed)
                    assert_crlf_only(self, original)
                    target.write_bytes(original)
                    result = subprocess.run(
                        [sys.executable, str(script), str(target)],
                        check=False,
                        capture_output=True,
                        text=True,
                        env=environment,
                    )

                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn("ABORT", result.stdout)
                    self.assertEqual(target.read_bytes(), original)
                    assert_crlf_only(self, target.read_bytes())
                    self.assertFalse(Path(str(target) + ".bak-pre-overflowrouter").exists())
                    assert_no_staging_residue(self, directory, target)

    def test_previous_router_install_upgrades_to_current_block(self):
        script = ROOT / "patches" / "apply_busy_overflow_router_patch.py"
        current_source = script.read_text(encoding="utf-8")
        new_clause = (
            '                r"|\\\\b(?:the|my|our|your)\\\\s+'
            '(?:code|config|deck|document|draft|file|page|report|sheet|slide)\\\\b"\n'
        )
        self.assertEqual(current_source.count(new_clause), 1)

        with tempfile.TemporaryDirectory() as td:
            directory = Path(td)
            previous_script = directory / "previous_installer.py"
            previous_script.write_text(
                current_source.replace(new_clause, "", 1), encoding="utf-8",
            )
            previous_constants = string_constants(previous_script)
            current_constants = string_constants(script)
            target = directory / "target.py"
            target.write_bytes(
                crlf_bytes(unpatched_source(
                    current_constants,
                    "HOOK_OLD",
                    "_maybe_route_overflow_to_background",
                )),
            )
            assert_crlf_only(self, target.read_bytes())
            environment = {
                **os.environ,
                "PYTHONPYCACHEPREFIX": str(directory / "pycache"),
            }

            for installer, expected in (
                (previous_script, "PATCHED_OK"),
                (script, "PATCHED_OK"),
                (script, "ALREADY_PATCHED"),
            ):
                before = target.read_bytes()
                backup = Path(str(target) + ".bak-pre-overflowrouter")
                upgrade_backup = Path(str(backup) + ".upgrade")
                backup_before = backup.read_bytes() if backup.exists() else None
                upgrade_before = upgrade_backup.read_bytes() if upgrade_backup.exists() else None
                result = subprocess.run(
                    [sys.executable, str(installer), str(target)],
                    check=False,
                    capture_output=True,
                    text=True,
                    env=environment,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stdout.strip(), expected)
                if expected == "PATCHED_OK" and backup_before is None:
                    self.assertEqual(backup.read_bytes(), before)
                    assert_crlf_only(self, target.read_bytes())
                elif expected == "PATCHED_OK":
                    self.assertEqual(backup.read_bytes(), backup_before)
                    self.assertEqual(upgrade_backup.read_bytes(), before)
                    assert_crlf_only(self, target.read_bytes())
                else:
                    self.assertEqual(target.read_bytes(), before)
                    self.assertEqual(backup.read_bytes(), backup_before)
                    self.assertEqual(upgrade_backup.read_bytes(), upgrade_before)
                assert_no_staging_residue(self, directory, target)

            upgraded = target.read_text(encoding="utf-8")
            self.assertIn(current_constants["BLOCK"], upgraded)
            self.assertNotIn(previous_constants["BLOCK"], upgraded)
            assert_crlf_only(self, target.read_bytes())

    def test_unrelated_marker_mention_does_not_skip_patch(self):
        for script_name, old_name, old_marker, backup_suffix in INSTALLERS:
            with self.subTest(script=script_name), tempfile.TemporaryDirectory() as td:
                script = ROOT / "patches" / script_name
                constants = string_constants(script)
                target = Path(td) / "target.py"
                source = unpatched_source(constants, old_name, old_marker)
                target.write_text(source, encoding="utf-8")
                target.chmod(0o640)
                original = target.read_bytes()

                command = [sys.executable, str(script), str(target)]
                environment = {
                    **os.environ,
                    "PYTHONPYCACHEPREFIX": str(Path(td) / "pycache"),
                }
                result = subprocess.run(
                    command, check=False, capture_output=True, text=True, env=environment,
                )

                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("PATCHED_OK", result.stdout)
                self.assertNotIn(constants[old_name], target.read_text(encoding="utf-8"))
                backup = Path(str(target) + backup_suffix)
                self.assertEqual(backup.read_bytes(), original)
                backup_after_first = backup.read_bytes()
                if os.name != "nt":
                    self.assertEqual(target.stat().st_mode & 0o777, 0o640)
                patched = target.read_bytes()

                result = subprocess.run(
                    command, check=False, capture_output=True, text=True, env=environment,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("ALREADY_PATCHED", result.stdout)
                self.assertEqual(target.read_bytes(), patched)
                self.assertEqual(backup.read_bytes(), backup_after_first)

    def test_crlf_targets_patch_idempotently_without_changing_line_endings(self):
        for script_name, old_name, old_marker, backup_suffix in INSTALLERS:
            with self.subTest(script=script_name), tempfile.TemporaryDirectory() as td:
                script = ROOT / "patches" / script_name
                constants = string_constants(script)
                target = Path(td) / "target.py"
                source = unpatched_source(constants, old_name, old_marker)
                original = crlf_bytes(source)
                assert_crlf_only(self, original)
                target.write_bytes(original)
                environment = {
                    **os.environ,
                    "PYTHONPYCACHEPREFIX": str(Path(td) / "pycache"),
                }
                command = [sys.executable, str(script), str(target)]

                first = subprocess.run(
                    command, check=False, capture_output=True, text=True, env=environment,
                )
                self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
                self.assertIn("PATCHED_OK", first.stdout)
                patched = target.read_bytes()
                assert_crlf_only(self, patched)
                backup = Path(str(target) + backup_suffix)
                self.assertEqual(backup.read_bytes(), original)
                backup_after_first = backup.read_bytes()
                assert_no_staging_residue(self, Path(td), target)

                second = subprocess.run(
                    command, check=False, capture_output=True, text=True, env=environment,
                )
                self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
                self.assertIn("ALREADY_PATCHED", second.stdout)
                self.assertEqual(target.read_bytes(), patched)
                self.assertEqual(backup.read_bytes(), backup_after_first)
                assert_crlf_only(self, target.read_bytes())
                assert_no_staging_residue(self, Path(td), target)

    def test_invalid_line_endings_fail_closed_without_filesystem_residue(self):
        for script_name, old_name, old_marker, backup_suffix in INSTALLERS:
            script = ROOT / "patches" / script_name
            constants = string_constants(script)
            source = unpatched_source(constants, old_name, old_marker)
            valid_crlf = crlf_bytes(source)
            cases = {
                "mixed-crlf-lf": (
                    valid_crlf.replace(b"\r\n", b"\n", 1),
                    "mixed line endings",
                ),
                "lone-cr": (
                    source.replace("\n", "\r").encode("utf-8"),
                    "carriage-return",
                ),
                "mixed-crlf-cr": (
                    valid_crlf.replace(b"\r\n", b"\r", 1),
                    "carriage-return",
                ),
                "doubled-crlf": (
                    valid_crlf.replace(b"\r\n", b"\r\r\n", 1),
                    "carriage-return",
                ),
            }

            for name, (original, expected_message) in cases.items():
                with self.subTest(script=script_name, case=name), tempfile.TemporaryDirectory() as td:
                    directory = Path(td)
                    target = directory / "target.py"
                    target.write_bytes(original)
                    result = subprocess.run(
                        [sys.executable, str(script), str(target)],
                        check=False,
                        capture_output=True,
                        text=True,
                        env={
                            **os.environ,
                            "PYTHONPYCACHEPREFIX": str(directory / "pycache"),
                        },
                    )

                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn(expected_message, result.stdout)
                    self.assertEqual(target.read_bytes(), original)
                    self.assertFalse(Path(str(target) + backup_suffix).exists())
                    assert_no_staging_residue(self, directory, target)

    def test_non_utf8_target_fails_closed_without_filesystem_residue(self):
        for script_name, _old_name, _old_marker, backup_suffix in INSTALLERS:
            with self.subTest(script=script_name), tempfile.TemporaryDirectory() as td:
                directory = Path(td)
                script = ROOT / "patches" / script_name
                target = directory / "target.py"
                original = b"# invalid UTF-8 follows\n\xff\n"
                target.write_bytes(original)

                result = subprocess.run(
                    [sys.executable, str(script), str(target)],
                    check=False,
                    capture_output=True,
                    text=True,
                    env={
                        **os.environ,
                        "PYTHONPYCACHEPREFIX": str(directory / "pycache"),
                    },
                )

                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(result.stdout.strip(), "ABORT: target must be readable UTF-8")
                self.assertEqual(result.stderr, "")
                self.assertEqual(target.read_bytes(), original)
                self.assertFalse(Path(str(target) + backup_suffix).exists())
                assert_no_staging_residue(self, directory, target)

    def test_unreadable_target_fails_closed_without_filesystem_residue(self):
        for script_name, _old_name, _old_marker, backup_suffix in INSTALLERS:
            with self.subTest(script=script_name), tempfile.TemporaryDirectory() as td:
                directory = Path(td)
                script = ROOT / "patches" / script_name
                target = directory / "target.py"
                original = b"# readable fixture before simulated denial\n"
                target.write_bytes(original)
                code = compile(script.read_text(encoding="utf-8"), str(script), "exec")
                output = io.StringIO()

                with patch(
                    "sys.argv",
                    [str(script), str(target)],
                ), patch(
                    "builtins.open",
                    side_effect=PermissionError("simulated read denial"),
                ), contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as raised:
                    exec(code, {"__name__": "__main__", "__file__": str(script)})

                self.assertEqual(raised.exception.code, 2)
                self.assertEqual(output.getvalue().strip(), "ABORT: target must be readable UTF-8")
                self.assertEqual(target.read_bytes(), original)
                self.assertFalse(Path(str(target) + backup_suffix).exists())
                assert_no_staging_residue(self, directory, target)

    def test_symlink_target_fails_closed(self):
        cases = [
            ("apply_debounce_fifo_patch.py", "OLD", "_queue_or_replace_pending_event"),
            ("apply_busy_overflow_router_patch.py", "HOOK_OLD", "_maybe_route_overflow_to_background"),
        ]

        for script_name, old_name, old_marker in cases:
            with self.subTest(script=script_name), tempfile.TemporaryDirectory() as td:
                script = ROOT / "patches" / script_name
                constants = string_constants(script)
                referent = Path(td) / "referent.py"
                source = unpatched_source(constants, old_name, old_marker)
                referent.write_text(source, encoding="utf-8")
                target = Path(td) / "target.py"
                try:
                    target.symlink_to(referent)
                except OSError as error:
                    if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                        self.skipTest("Windows symlink privilege is unavailable")
                    raise

                result = subprocess.run(
                    [sys.executable, str(script), str(target)],
                    check=False, capture_output=True, text=True,
                )

                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("regular file", result.stdout)
                self.assertTrue(target.is_symlink())
                self.assertEqual(referent.read_text(encoding="utf-8"), source)
                self.assertFalse(list(Path(td).glob("*.bak-pre-*")))

    def test_compile_failure_preserves_target(self):
        cases = [
            ("apply_debounce_fifo_patch.py", "OLD", "_queue_or_replace_pending_event"),
            ("apply_busy_overflow_router_patch.py", "HOOK_OLD", "_maybe_route_overflow_to_background"),
        ]

        for script_name, old_name, old_marker in cases:
            with self.subTest(script=script_name), tempfile.TemporaryDirectory() as td:
                script = ROOT / "patches" / script_name
                constants = string_constants(script)
                target = Path(td) / "target.py"
                source = unpatched_source(constants, old_name, old_marker) + "\ninvalid syntax !!!\n"
                target.write_text(source, encoding="utf-8")

                result = subprocess.run(
                    [sys.executable, str(script), str(target)],
                    check=False, capture_output=True, text=True,
                )

                self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                self.assertIn("target unchanged", result.stdout)
                self.assertEqual(target.read_text(encoding="utf-8"), source)
                self.assertFalse(Path(str(target) + ".bak-pre-debouncefifo").exists())
                self.assertFalse(Path(str(target) + ".bak-pre-overflowrouter").exists())
                self.assertFalse(list(Path(td).glob(".target.py.*.tmp*")))

    def test_existing_backup_is_never_overwritten(self):
        cases = [
            (
                "apply_debounce_fifo_patch.py",
                "OLD",
                "_queue_or_replace_pending_event",
                ".bak-pre-debouncefifo",
            ),
            (
                "apply_busy_overflow_router_patch.py",
                "HOOK_OLD",
                "_maybe_route_overflow_to_background",
                ".bak-pre-overflowrouter",
            ),
        ]

        for script_name, old_name, old_marker, backup_suffix in cases:
            with self.subTest(script=script_name), tempfile.TemporaryDirectory() as td:
                directory = Path(td)
                script = ROOT / "patches" / script_name
                constants = string_constants(script)
                target = directory / "target.py"
                source = unpatched_source(constants, old_name, old_marker)
                target.write_text(source, encoding="utf-8")
                backup = Path(str(target) + backup_suffix)
                previous_backup = b"operator-owned recovery copy\n"
                backup.write_bytes(previous_backup)

                result = subprocess.run(
                    [sys.executable, str(script), str(target)],
                    check=False,
                    capture_output=True,
                    text=True,
                    env={
                        **os.environ,
                        "PYTHONPYCACHEPREFIX": str(directory / "pycache"),
                    },
                )

                self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                self.assertIn("target unchanged", result.stdout)
                self.assertEqual(target.read_text(encoding="utf-8"), source)
                self.assertEqual(backup.read_bytes(), previous_backup)
                assert_no_staging_residue(self, directory, target)

    def test_existing_backup_symlink_is_never_followed(self):
        for script_name, old_name, old_marker, backup_suffix in INSTALLERS:
            with self.subTest(script=script_name), tempfile.TemporaryDirectory() as td:
                directory = Path(td)
                script = ROOT / "patches" / script_name
                constants = string_constants(script)
                target = directory / "target.py"
                source = unpatched_source(constants, old_name, old_marker)
                target.write_text(source, encoding="utf-8")
                recovery = directory / "operator-recovery.txt"
                recovery_bytes = b"operator-owned recovery copy\n"
                recovery.write_bytes(recovery_bytes)
                backup = Path(str(target) + backup_suffix)
                try:
                    backup.symlink_to(recovery)
                except OSError as error:
                    if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                        self.skipTest("Windows symlink privilege is unavailable")
                    raise

                result = subprocess.run(
                    [sys.executable, str(script), str(target)],
                    check=False,
                    capture_output=True,
                    text=True,
                    env={
                        **os.environ,
                        "PYTHONPYCACHEPREFIX": str(directory / "pycache"),
                    },
                )

                self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                self.assertIn("target unchanged", result.stdout)
                self.assertEqual(target.read_text(encoding="utf-8"), source)
                self.assertTrue(backup.is_symlink())
                self.assertEqual(recovery.read_bytes(), recovery_bytes)
                assert_no_staging_residue(self, directory, target)


if __name__ == "__main__":
    unittest.main()
