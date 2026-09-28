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
        and isinstance(node.value, ast.Constant)
    }


def unpatched_source(constants, old_name, old_marker):
    if old_name == "OLD":
        # Synthetic base.py stand-in. `logger` and `MessageType` are the two
        # names the injected flush body calls at runtime; the installer now
        # refuses a target that does not bind them.
        return (
            f"# {old_marker} is supplied by the runner\n"
            "import logging\n"
            "\n"
            "logger = logging.getLogger(__name__)\n"
            "\n"
            "\n"
            "class MessageType:\n"
            "    TEXT = 'text'\n"
            "    PHOTO = 'photo'\n"
            "\n"
            "\n"
            "class Fixture:\n"
            "    async def _flush_text_debounce_now(self, session_key):\n"
            + constants[old_name]
        )
    # In gateway/run.py the busy-handler anchor precedes the later queue-mode hook.
    return (
        "# fixture\nimport re\nimport os\nimport time\nimport asyncio\nfrom hermes_cli.config import _load_gateway_runtime_config, cfg_get\n\n"
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


def entries(directory):
    """Directory listing without the bytecode prefix the fixtures redirect to."""
    return sorted(path.name for path in directory.iterdir() if path.name != "pycache")


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
                    "os.open",
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

    def test_current_debounce_install_rejects_malformed_structure(self):
        script = ROOT / "patches" / "apply_debounce_fifo_patch.py"
        constants = string_constants(script)
        unpatched = unpatched_source(
            constants,
            "OLD",
            "_queue_or_replace_pending_event",
        )
        patched = unpatched.replace(constants["OLD"], constants["NEW"], 1)
        cases = {
            "old-and-new": unpatched + constants["NEW"],
            "duplicate-new": patched + constants["NEW"],
        }

        for name, source in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as td:
                directory = Path(td)
                target = directory / "target.py"
                target.write_text(source, encoding="utf-8")
                original = target.read_bytes()

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
                self.assertIn("malformed current install", result.stdout)
                self.assertEqual(target.read_bytes(), original)
                self.assertFalse(Path(str(target) + ".bak-pre-debouncefifo").exists())
                assert_no_staging_residue(self, directory, target)

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

    def run_installer(self, script, target, directory, *options):
        return subprocess.run(
            [sys.executable, str(script), str(target), *options],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPYCACHEPREFIX": str(directory / "pycache")},
        )

    def test_check_agrees_with_the_write_about_a_conflicting_recovery_copy(self):
        # The README promises a --check verdict always matches the install
        # that follows it. The recovery copy is written after the verdict, so
        # a check that ignored it approved a write that then aborted on exit 3.
        # The verdict must be the same for the real write and for --check.
        for script_name, old_name, old_marker, backup_suffix in INSTALLERS:
            for direction, write_options, extra_suffix in (
                ("apply", (), ""), ("reverse", ("--reverse",), ".reverse"),
            ):
                with self.subTest(script=script_name, direction=direction), \
                        tempfile.TemporaryDirectory() as td:
                    directory = Path(td)
                    script = ROOT / "patches" / script_name
                    target = directory / "target.py"
                    target.write_text(
                        unpatched_source(string_constants(script), old_name, old_marker),
                        encoding="utf-8",
                    )
                    if write_options:
                        applied = self.run_installer(script, target, directory)
                        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
                    conflicting = Path(str(target) + backup_suffix + extra_suffix)
                    conflicting.write_bytes(b"operator-owned recovery copy\n")
                    installed = target.read_bytes()

                    for extra in (write_options, (*write_options, "--check")):
                        with self.subTest(options=extra):
                            before = entries(directory)
                            result = self.run_installer(script, target, directory, *extra)

                            self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                            self.assertIn(
                                "existing recovery backup differs", result.stdout,
                            )
                            self.assertEqual(target.read_bytes(), installed)
                            self.assertEqual(
                                conflicting.read_bytes(), b"operator-owned recovery copy\n",
                            )
                            self.assertEqual(entries(directory), before)
                            assert_no_staging_residue(self, directory, target)

    def test_check_still_approves_a_recovery_copy_the_write_may_reuse(self):
        # The other side of the same contract: after a reverse and a reapply
        # the copy on disk is byte-identical, the write reuses it, and --check
        # must keep saying the install is safe to run.
        for script_name, old_name, old_marker, backup_suffix in INSTALLERS:
            with self.subTest(script=script_name), tempfile.TemporaryDirectory() as td:
                directory = Path(td)
                script = ROOT / "patches" / script_name
                target = directory / "target.py"
                target.write_text(
                    unpatched_source(string_constants(script), old_name, old_marker),
                    encoding="utf-8",
                )
                original = target.read_bytes()
                reusable = Path(str(target) + backup_suffix)
                reusable.write_bytes(original)

                result = self.run_installer(script, target, directory, "--check")

                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stdout.strip(), "APPLICABLE")
                self.assertEqual(target.read_bytes(), original)
                self.assertEqual(reusable.read_bytes(), original)
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

    def test_debounce_patch_refuses_a_block_that_left_the_flush_method(self):
        # The OLD block is ten plain lines with no signature. If upstream moves
        # the flush and a copy of the same ten lines survives in another method,
        # a count of one is not proof of the right site. The installer must
        # refuse rather than rewrite an unrelated function.
        script = ROOT / "patches" / "apply_debounce_fifo_patch.py"
        constants = string_constants(script)
        prefix = unpatched_source(constants, "OLD", "_queue_or_replace_pending_event")
        head = prefix[:prefix.index("    async def _flush_text_debounce_now")]
        cases = {
            "block-elsewhere": head + "    def other_helper(self, store, session_key):\n"
                           + constants["OLD"] + "\n    async def _flush_text_debounce_now(self, session_key):\n        return False\n",
            "method-renamed": prefix.replace("_flush_text_debounce_now", "_flush_text_debounce", 1),
        }

        for name, source in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as td:
                directory = Path(td)
                target = directory / "target.py"
                target.write_text(source, encoding="utf-8")
                original = target.read_bytes()

                for options in ((), ("--check",)):
                    with self.subTest(case=name, options=options):
                        result = subprocess.run(
                            [sys.executable, str(script), str(target), *options],
                            check=False,
                            capture_output=True,
                            text=True,
                            env={**os.environ, "PYTHONPYCACHEPREFIX": str(directory / "pycache")},
                        )
                        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                        self.assertIn("ABORT", result.stdout)
                        self.assertEqual(target.read_bytes(), original)
                        self.assertFalse(Path(str(target) + ".bak-pre-debouncefifo").exists())
                        assert_no_staging_residue(self, directory, target)

    def test_one_line_upstream_drift_aborts_instead_of_half_applying(self):
        # The most common way a patch repo breaks is upstream editing one line
        # inside the anchor. The target is then 95% identical and still
        # parses, so only exact matching can catch it. Proving the refusal here
        # means the offline suite covers the drift contract CI cannot, because
        # CI has no upstream fixture.
        cases = (
            ("apply_debounce_fifo_patch.py", "OLD", "_queue_or_replace_pending_event",
             "            merge_text=True,\n", "            merge_text = True,\n",
             "expected exactly 1 flush site"),
            ("apply_busy_overflow_router_patch.py", "HOOK_OLD", "_maybe_route_overflow_to_background",
             '            and effective_mode != "steer"\n', '            and effective_mode not in ("steer", "pause")\n',
             "expected exactly 1 hook site"),
        )

        for script_name, old_name, old_marker, before, after, message in cases:
            with self.subTest(script=script_name), tempfile.TemporaryDirectory() as td:
                directory = Path(td)
                script = ROOT / "patches" / script_name
                source = unpatched_source(string_constants(script), old_name, old_marker)
                self.assertEqual(source.count(before), 1, "drift fixture no longer matches the anchor")
                drifted = source.replace(before, after, 1)
                compile(drifted, str(script), "exec")  # still valid Python
                target = directory / "target.py"
                target.write_text(drifted, encoding="utf-8")
                original = target.read_bytes()

                for options in ((), ("--check",)):
                    with self.subTest(options=options):
                        result = subprocess.run(
                            [sys.executable, str(script), str(target), *options],
                            check=False,
                            capture_output=True,
                            text=True,
                            env={**os.environ, "PYTHONPYCACHEPREFIX": str(directory / "pycache")},
                        )
                        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                        self.assertIn(message, result.stdout)
                        self.assertEqual(target.read_bytes(), original)
                        self.assertFalse(list(directory.glob("target.py.bak-pre-*")))
                        assert_no_staging_residue(self, directory, target)

    def test_bom_target_check_agrees_with_apply_and_round_trips(self):
        # Regression: the in-memory syntax check compiled the decoded str, so a
        # byte order mark read as a SyntaxError. --check refused a file that the
        # very next apply wrote successfully, and refused the installed file
        # too, so the operator could never confirm a real install.
        for script_name, old_name, old_marker in (
            ("apply_debounce_fifo_patch.py", "OLD", "_queue_or_replace_pending_event"),
            ("apply_busy_overflow_router_patch.py", "HOOK_OLD", "_maybe_route_overflow_to_background"),
        ):
            with self.subTest(script=script_name), tempfile.TemporaryDirectory() as td:
                directory = Path(td)
                script = ROOT / "patches" / script_name
                target = directory / "target.py"
                original = b"\xef\xbb\xbf" + unpatched_source(
                    string_constants(script), old_name, old_marker).encode("utf-8")
                target.write_bytes(original)
                environment = {**os.environ, "PYTHONPYCACHEPREFIX": str(directory / "pycache")}
                command = [sys.executable, str(script), str(target)]

                for options, expected in ((("--check",), "APPLICABLE"), ((), "PATCHED_OK"),
                                          (("--check",), "ALREADY_PATCHED"),
                                          (("--check", "--reverse"), "REVERSIBLE"),
                                          (("--reverse",), "REVERSED_OK")):
                    with self.subTest(options=options):
                        result = subprocess.run(
                            [*command, *options], check=False, capture_output=True,
                            text=True, env=environment,
                        )
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertEqual(result.stdout.strip(), expected)
                        if expected == "PATCHED_OK":
                            patched = target.read_bytes()
                            self.assertTrue(patched.startswith(b"\xef\xbb\xbf"))
                            compile(patched, str(target), "exec")

                self.assertEqual(target.read_bytes(), original)
                assert_no_staging_residue(self, directory, target)

    def test_debounce_patch_aborts_when_injected_symbols_are_missing(self):
        # Regression: the injected flush body calls `logger` and `MessageType`.
        # If the target does not bind them, the flush raises NameError AFTER
        # the burst has left the debounce store, so the follow-up is dropped
        # instead of falling back to the pending-slot merge.
        script = ROOT / "patches" / "apply_debounce_fifo_patch.py"
        constants = string_constants(script)
        source = unpatched_source(constants, "OLD", "_queue_or_replace_pending_event")
        cases = {
            # Both variants still parse; only the required name is unbound, so
            # the installer must be the thing that refuses them.
            "logger-renamed": source.replace(
                "logger = logging.getLogger(__name__)", "log = logging.getLogger(__name__)", 1),
            "message-type-renamed": source.replace("class MessageType:", "class MessageKind:", 1),
        }

        for name, incomplete in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as td:
                directory = Path(td)
                target = directory / "target.py"
                target.write_text(incomplete, encoding="utf-8")
                original = target.read_bytes()
                result = subprocess.run(
                    [sys.executable, str(script), str(target)],
                    check=False,
                    capture_output=True,
                    text=True,
                    env={**os.environ, "PYTHONPYCACHEPREFIX": str(directory / "pycache")},
                )

                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("ABORT: base-platform symbol", result.stdout)
                self.assertEqual(target.read_bytes(), original)
                self.assertFalse(Path(str(target) + ".bak-pre-debouncefifo").exists())
                assert_no_staging_residue(self, directory, target)

    def test_router_patch_aborts_when_gateway_runtime_symbols_are_missing(self):
        # Regression: if a Hermes build lacks _load_gateway_runtime_config or
        # cfg_get, the patched method raises NameError at first call and the
        # broad `except Exception: return "off"` silently disables the router.
        # The installer must abort loudly so the operator notices.
        script = ROOT / "patches" / "apply_busy_overflow_router_patch.py"
        constants = string_constants(script)
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td)
            target = directory / "target.py"
            target.write_text(unpatched_source(constants, "HOOK_OLD", "_maybe_route_overflow_to_background"), encoding="utf-8")
            stripped = target.read_text(encoding="utf-8").replace(
                "from hermes_cli.config import _load_gateway_runtime_config, cfg_get\n", "", 1
            )
            target.write_text(stripped, encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(script), str(target)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("ABORT: gateway runtime symbol", result.stdout)


if __name__ == "__main__":
    unittest.main()
