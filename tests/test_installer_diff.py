#!/usr/bin/env python3
"""--diff shows the exact edit an apply, upgrade or reverse would write.

--check returned only a verdict, so an operator could not review the change
to a production gateway file before the backup and the replacement. --diff
runs the same decision tree and preflight as --check, prints the same first
line, and then the unified diff of the target against the bytes the write
would produce. It writes nothing. Every refusal prints exactly what --check
prints, with the same exit code.

The diff is written as UTF-8 with LF line endings on every platform. The
first line comes from print(), so Windows ends it with CRLF; the checks below
compare that line without its line ending, and the diff body byte for byte.
"""
import difflib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_patch_installers import ROOT, string_constants, unpatched_source


ROUTER = ROOT / "patches" / "apply_busy_overflow_router_patch.py"
DEBOUNCE = ROOT / "patches" / "apply_debounce_fifo_patch.py"
FIXTURE = ROOT / "tests" / "fixtures" / "router_block_1.0.0.txt"
FOREIGN = "    def operator_method(self):\n        return 'kept'\n\n"
BACKUP = {ROUTER: ".bak-pre-overflowrouter", DEBOUNCE: ".bak-pre-debouncefifo"}


def run(script, target, *options):
    return subprocess.run(
        [sys.executable, str(script), str(target), *options], check=False, capture_output=True,
        env={**os.environ, "PYTHONPYCACHEPREFIX": str(target.parent / "pycache")},
    )


def split(result):
    """(first line without its line ending, the rest as bytes)."""
    first, _, rest = result.stdout.partition(b"\n")
    return first.rstrip(b"\r").decode("utf-8"), rest


def unpatched(script):
    if script == ROUTER:
        return unpatched_source(string_constants(ROUTER), "HOOK_OLD", "_maybe_route_overflow_to_background")
    return unpatched_source(string_constants(DEBOUNCE), "OLD", "_queue_or_replace_pending_event")


def expected_diff(before, after, line_ending):
    lines = difflib.unified_diff(before.split(line_ending), after.split(line_ending),
                                 "a/target.py", "b/target.py", n=3, lineterm="")
    return ("\n".join(lines) + "\n").encode("utf-8")


def previous(script):
    """An install of an older generation that each installer upgrades."""
    if script == ROUTER:
        c = string_constants(ROUTER)
        block = FIXTURE.read_bytes().decode("utf-8")
        return (unpatched(ROUTER).replace(c["HOOK_OLD"], c["HOOK_NEW"], 1)
                .replace(c["ANCHOR"], block + FOREIGN + c["ANCHOR"], 1))
    c = string_constants(DEBOUNCE)
    guard = c["OLD"][:c["OLD"].index("        state = store.pop(session_key, None)\n")]
    return unpatched(DEBOUNCE).replace(c["OLD"], guard + c["PREVIOUS_NEW"], 1)


class DiffTests(unittest.TestCase):
    def fresh(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        return Path(holder.name)

    def target(self, text, crlf=False):
        target = self.fresh() / "target.py"
        target.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))
        return target

    def assert_diff_matches_the_write(self, script, text, options, token, crlf=False, prepare=()):
        """--diff on one copy equals difflib over a twin's bytes before and after the write."""
        shown, written = self.target(text, crlf), self.target(text, crlf)
        for setup in prepare:
            for copy in (shown, written):
                result = run(script, copy, *setup)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        before_listing = sorted(path.name for path in shown.parent.iterdir())
        before = shown.read_bytes()

        diffed = run(script, shown, *options, "--diff")
        checked = run(script, shown, *options, "--check")
        self.assertEqual(diffed.returncode, 0, diffed.stdout + diffed.stderr)
        first, body = split(diffed)
        self.assertEqual(first, token)
        self.assertEqual(first, split(checked)[0])
        self.assertEqual(shown.read_bytes(), before)
        self.assertEqual(sorted(path.name for path in shown.parent.iterdir()), before_listing)

        applied = run(script, written, *options)
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        line_ending = "\r\n" if crlf else "\n"
        self.assertEqual(body, expected_diff(before.decode("utf-8"), written.read_bytes().decode("utf-8"),
                                             line_ending))
        self.assertNotIn(b"\r", body)
        self.assertTrue(body.startswith(b"--- a/target.py\n+++ b/target.py\n@@ "), body[:80])
        return body

    def test_apply_diff_is_the_edit_the_install_writes(self):
        for script in (ROUTER, DEBOUNCE):
            for crlf in (False, True):
                with self.subTest(script=script.name, crlf=crlf):
                    self.assert_diff_matches_the_write(script, unpatched(script), (), "APPLICABLE", crlf)

    def test_upgrade_diff_is_the_edit_the_upgrade_writes(self):
        for script in (ROUTER, DEBOUNCE):
            with self.subTest(script=script.name):
                body = self.assert_diff_matches_the_write(script, previous(script), (), "UPGRADE_APPLICABLE")
                if script == ROUTER:
                    # The operator's method is context, never a removed line.
                    self.assertNotIn(b"-    def operator_method", body)

    def test_reverse_diff_is_the_edit_the_reverse_writes(self):
        for script in (ROUTER, DEBOUNCE):
            with self.subTest(script=script.name):
                self.assert_diff_matches_the_write(script, unpatched(script), ("--reverse",), "REVERSIBLE",
                                                   prepare=((),))

    def test_a_refusal_prints_exactly_what_check_prints(self):
        for script in (ROUTER, DEBOUNCE):
            unsupported = self.target("# unsupported source\n")
            full = self.target(unpatched(script))
            for ordinal in range(1, 9):
                name = str(full) + BACKUP[script] + ("" if ordinal == 1 else ".%d" % ordinal)
                Path(name).write_bytes(b"operator copy %d\n" % ordinal)
            for target, code in ((unsupported, 2), (full, 3)):
                with self.subTest(script=script.name, exit=code):
                    listing = sorted(path.name for path in target.parent.iterdir())
                    diffed = run(script, target, "--diff")
                    checked = run(script, target, "--check")
                    self.assertEqual(diffed.returncode, code, diffed.stdout + diffed.stderr)
                    self.assertEqual((diffed.returncode, diffed.stdout), (checked.returncode, checked.stdout))
                    self.assertTrue(diffed.stdout.startswith(b"ABORT:"))
                    self.assertEqual(sorted(path.name for path in target.parent.iterdir()), listing)

    def test_nothing_to_change_prints_only_the_token(self):
        for script in (ROUTER, DEBOUNCE):
            with self.subTest(script=script.name):
                target = self.target(unpatched(script))
                self.assertEqual(run(script, target).returncode, 0)
                self.assertEqual(run(script, target, "--diff").stdout.replace(b"\r\n", b"\n"),
                                 b"ALREADY_PATCHED\n")
                self.assertEqual(run(script, target, "--reverse").returncode, 0)
                self.assertEqual(run(script, target, "--diff", "--reverse").stdout.replace(b"\r\n", b"\n"),
                                 b"ALREADY_UNPATCHED\n")


if __name__ == "__main__":
    unittest.main()
