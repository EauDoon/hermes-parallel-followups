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
    return (
        "# fixture\nimport re\nimport os\nimport time\nimport asyncio\n\n"
        f"# {old_marker} is described in the release notes\n"
        "class Fixture:\n"
        "    async def route(self, event, session_key):\n"
        + constants[old_name]
        + constants["ANCHOR"]
        + "\n        pass\n"
    )


class PatchInstallerTests(unittest.TestCase):
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

                result = subprocess.run(
                    command, check=False, capture_output=True, text=True, env=environment,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("ALREADY_PATCHED", result.stdout)

    def test_compile_cache_failure_restores_target(self):
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
                (Path(td) / "__pycache__").write_text("blocks bytecode directory", encoding="utf-8")
                environment = dict(os.environ)
                environment.pop("PYTHONPYCACHEPREFIX", None)

                result = subprocess.run(
                    [sys.executable, str(script), str(target)],
                    check=False, capture_output=True, text=True, env=environment,
                )

                self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                self.assertIn("restored backup", result.stdout)
                self.assertEqual(target.read_text(encoding="utf-8"), source)


if __name__ == "__main__":
    unittest.main()
