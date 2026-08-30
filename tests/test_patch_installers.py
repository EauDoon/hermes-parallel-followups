#!/usr/bin/env python3
"""Regression checks for patch-installer idempotency detection."""

import ast
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


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
                    target.write_text(malformed, encoding="utf-8")
                    result = subprocess.run(
                        [sys.executable, str(script), str(target)],
                        check=False,
                        capture_output=True,
                        text=True,
                        env=environment,
                    )

                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn("ABORT", result.stdout)
                    self.assertEqual(target.read_text(encoding="utf-8"), malformed)
                    self.assertFalse(Path(str(target) + ".bak-pre-overflowrouter").exists())

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
            target.write_text(
                unpatched_source(
                    current_constants,
                    "HOOK_OLD",
                    "_maybe_route_overflow_to_background",
                ),
                encoding="utf-8",
            )
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
                result = subprocess.run(
                    [sys.executable, str(installer), str(target)],
                    check=False,
                    capture_output=True,
                    text=True,
                    env=environment,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stdout.strip(), expected)
                if expected == "ALREADY_PATCHED":
                    self.assertEqual(target.read_bytes(), before)

            upgraded = target.read_text(encoding="utf-8")
            self.assertIn(current_constants["BLOCK"], upgraded)
            self.assertNotIn(previous_constants["BLOCK"], upgraded)

    def test_unrelated_marker_mention_does_not_skip_patch(self):
        cases = [
            ("apply_debounce_fifo_patch.py", "OLD", "_queue_or_replace_pending_event"),
            ("apply_busy_overflow_router_patch.py", "HOOK_OLD", "_maybe_route_overflow_to_background"),
        ]

        for script_name, old_name, old_marker in cases:
            with self.subTest(script=script_name), tempfile.TemporaryDirectory() as td:
                script = ROOT / "patches" / script_name
                constants = string_constants(script)
                target = Path(td) / "target.py"
                source = unpatched_source(constants, old_name, old_marker)
                target.write_text(source, encoding="utf-8")
                target.chmod(0o640)

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
                self.assertEqual(target.stat().st_mode & 0o777, 0o640)
                patched = target.read_bytes()

                result = subprocess.run(
                    command, check=False, capture_output=True, text=True, env=environment,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("ALREADY_PATCHED", result.stdout)
                self.assertEqual(target.read_bytes(), patched)

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
                target.symlink_to(referent)

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


if __name__ == "__main__":
    unittest.main()
