#!/usr/bin/env python3
"""Router installs from earlier releases upgrade and reverse without losing code.

The router upgrade replaced everything from the injected block marker to the
busy-handler anchor. A method that sat between an older block and that anchor
(an operator's, another patch's, or an upstream merge's) was deleted while the
installer printed PATCHED_OK. Reverse refused every block but the current one,
so a 1.0.0 install had to be upgraded before it could be removed.

The installer now lists every block that shipped on main (PREVIOUS_BLOCKS). A
listed block is upgraded or reversed exactly, and whatever follows it is kept.
An unlisted region is still upgraded when all of it is router code, and is
refused when it is not. tests/fixtures/router_block_1.0.0.txt is the block
v1.0.0 installs, as LF bytes.
"""
import ast
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_patch_installers import (
    ROOT, assert_crlf_only, assert_no_staging_residue, entries, string_constants, unpatched_source,
)


SCRIPT = ROOT / "patches" / "apply_busy_overflow_router_patch.py"
FIXTURE = ROOT / "tests" / "fixtures" / "router_block_1.0.0.txt"
HISTORY_PATHS = ("patches/apply_busy_overflow_router_patch.py", "apply_busy_overflow_router_patch.py")
FOREIGN = "    def operator_method(self):\n        return 'kept'\n\n"
REPEATED = "    @classmethod\n    def _classify_busy_followup(cls, text):\n        return True\n\n"
DECORATOR = "    @traced\n"
BACKUP = ".bak-pre-overflowrouter"


def previous_blocks():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    return next(ast.literal_eval(node.value) for node in tree.body
                if isinstance(node, ast.Assign) and len(node.targets) == 1
                and getattr(node.targets[0], "id", None) == "PREVIOUS_BLOCKS")


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def run(target, directory, *options):
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(target), *options],
        check=False, capture_output=True, text=True,
        env={**os.environ, "PYTHONPYCACHEPREFIX": str(directory / "pycache")},
    )


def git(*arguments, data=None):
    return subprocess.run(["git", "-C", str(ROOT), *arguments], input=data,
                          capture_output=True, check=True, timeout=30).stdout


def history_blocks():
    """(length, sha256) of every marker-bearing BLOCK on first-parent origin/main.

    Returns a string naming why the history is unavailable instead.
    """
    try:
        if git("rev-parse", "--is-shallow-repository").strip() != b"false":
            return "the clone is shallow; fetch full history (fetch-depth: 0) to run this check"
        commits = git("rev-list", "--first-parent", "origin/main").decode("ascii").split()
    except (OSError, subprocess.SubprocessError) as error:
        return "git history or origin/main is unavailable (%s)" % type(error).__name__
    requests = "".join("%s:%s\n" % (commit, path) for commit in commits for path in HISTORY_PATHS)
    output = git("cat-file", "--batch", data=requests.encode("ascii"))
    marker = string_constants(SCRIPT)["BLOCK_MARKER"]
    blobs, found, position = set(), set(), 0
    while position < len(output):
        end = output.index(b"\n", position)
        header = output[position:end].split()
        position = end + 1
        if header[-1] == b"missing":
            continue
        size = int(header[2])
        blob, position = output[position:position + size], position + size + 1
        if header[0] in blobs:
            continue
        blobs.add(header[0])
        try:
            tree = ast.parse(blob.decode("utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in tree.body:
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and getattr(node.targets[0], "id", None) == "BLOCK"
                    and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
                    and node.value.value.startswith(marker)):
                found.add((len(node.value.value), digest(node.value.value)))
    return found


class GenerationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.constants = string_constants(SCRIPT)
        cls.fixture = FIXTURE.read_bytes().decode("utf-8")
        cls.unpatched = unpatched_source(cls.constants, "HOOK_OLD", "_maybe_route_overflow_to_background")

    def fresh(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        return Path(holder.name)

    def installed(self, block, between=""):
        """The unpatched fixture with ``block`` installed the way 1.0.0 did."""
        c = self.constants
        return (self.unpatched.replace(c["HOOK_OLD"], c["HOOK_NEW"], 1)
                .replace(c["ANCHOR"], block + between + c["ANCHOR"], 1))

    def write(self, directory, text, crlf=False):
        target = directory / "run.py"
        target.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))
        return target

    def expect(self, target, directory, options, returncode, first_line):
        result = run(target, directory, *options)
        self.assertEqual(result.returncode, returncode, result.stdout + result.stderr)
        self.assertEqual(result.stdout.splitlines()[0], first_line)
        return result

    def edited_fixture(self):
        # One character changed inside a router member: still router code,
        # but no longer a block that shipped.
        self.assertEqual(self.fixture.count("_OVR_MIN_CHARS = 25"), 1)
        return self.fixture.replace("_OVR_MIN_CHARS = 25", "_OVR_MIN_CHARS = 26", 1)

    def test_fixture_is_the_listed_1_0_0_block(self):
        listed = [entry for entry in previous_blocks() if entry[2] == "1.0.0"]
        self.assertEqual(listed, [(len(self.fixture), digest(self.fixture), "1.0.0")])
        self.assertNotIn("\r", self.fixture)
        self.assertTrue(self.fixture.startswith(self.constants["BLOCK_MARKER"]))

    def test_previous_blocks_are_unique_longest_first_and_exclude_the_current_block(self):
        blocks = previous_blocks()
        digests = [entry[1] for entry in blocks]
        self.assertEqual(len(set(digests)), len(digests))
        self.assertEqual([entry[0] for entry in blocks], sorted((entry[0] for entry in blocks), reverse=True))
        self.assertNotIn(digest(self.constants["BLOCK"]), digests)
        self.assertEqual({entry[2] for entry in blocks}, {"1.0.0", "untagged main build"})

    def test_previous_blocks_match_the_first_parent_history_of_main(self):
        found = history_blocks()
        if isinstance(found, str):
            print("PREVIOUS_BLOCKS_HISTORY_SKIPPED: " + found)
            self.skipTest(found)
        current = (len(self.constants["BLOCK"]), digest(self.constants["BLOCK"]))
        self.assertEqual(found - {current}, {(length, sha) for length, sha, _label in previous_blocks()})
        print("PREVIOUS_BLOCKS_HISTORY_OK n=%d" % len(found - {current}))

    def test_released_block_upgrade_keeps_the_code_after_it(self):
        c = self.constants
        for crlf in (False, True):
            with self.subTest(crlf=crlf):
                directory = self.fresh()
                target = self.write(directory, self.installed(self.fixture, FOREIGN), crlf)
                # The copy the 1.0.0 install took, so the upgrade has to
                # write beside it.
                base = Path(str(target) + BACKUP)
                original = self.unpatched.replace("\n", "\r\n") if crlf else self.unpatched
                base.write_bytes(original.encode("utf-8"))
                prior = target.read_bytes()
                before = entries(directory)
                self.expect(target, directory, ("--check",), 0, "UPGRADE_APPLICABLE")
                self.assertEqual(entries(directory), before)
                self.expect(target, directory, (), 0, "PATCHED_OK")

                text = target.read_bytes().decode("utf-8").replace("\r\n", "\n")
                self.assertIn(c["BLOCK"] + FOREIGN + c["ANCHOR"], text)
                self.assertNotIn(self.fixture, text)
                self.assertEqual(text.count("def operator_method(self):"), 1)
                self.assertEqual(Path(str(base) + ".upgrade").read_bytes(), prior)
                if crlf:
                    assert_crlf_only(self, target.read_bytes())
                assert_no_staging_residue(self, directory, target)
                # The current block then reverses to the source plus the
                # operator's method, which was never part of the patch.
                self.expect(target, directory, ("--reverse",), 0, "REVERSED_OK")
                self.assertEqual(target.read_bytes().decode("utf-8").replace("\r\n", "\n"),
                                 self.unpatched.replace(c["ANCHOR"], FOREIGN + c["ANCHOR"], 1))

    def test_released_block_reverses_in_one_step(self):
        c = self.constants
        for crlf, between in ((False, ""), (True, ""), (False, FOREIGN), (True, FOREIGN)):
            with self.subTest(crlf=crlf, foreign=bool(between)):
                directory = self.fresh()
                target = self.write(directory, self.installed(self.fixture, between), crlf)
                installed = target.read_bytes()
                before = entries(directory)
                self.expect(target, directory, ("--check", "--reverse"), 0, "REVERSIBLE")
                self.assertEqual(entries(directory), before)
                self.expect(target, directory, ("--reverse",), 0, "REVERSED_OK")

                expected = self.unpatched.replace(c["ANCHOR"], between + c["ANCHOR"], 1)
                if crlf:
                    expected = expected.replace("\n", "\r\n")
                    assert_crlf_only(self, target.read_bytes())
                self.assertEqual(target.read_bytes(), expected.encode("utf-8"))
                self.assertEqual(Path(str(target) + BACKUP + ".reverse").read_bytes(), installed)
                self.expect(target, directory, ("--reverse",), 0, "ALREADY_UNPATCHED")
                assert_no_staging_residue(self, directory, target)

    def test_unknown_router_only_region_upgrades_but_does_not_reverse(self):
        c = self.constants
        directory = self.fresh()
        target = self.write(directory, self.installed(self.edited_fixture()))
        installed = target.read_bytes()
        before = entries(directory)
        result = run(target, directory, "--reverse")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("only the current or a released router block can be reversed", result.stdout)
        self.assertEqual(target.read_bytes(), installed)
        self.assertEqual(entries(directory), before)

        self.expect(target, directory, (), 0, "PATCHED_OK")
        self.assertIn(c["BLOCK"] + c["ANCHOR"], target.read_text(encoding="utf-8"))

    def test_unknown_region_with_foreign_code_is_refused(self):
        directory = self.fresh()
        text = self.installed(self.edited_fixture(), FOREIGN)
        target = self.write(directory, text)
        line = text[:text.index("    def operator_method")].count("\n") + 1
        before = entries(directory)
        for options in (("--check",), ()):
            with self.subTest(options=options):
                result = run(target, directory, *options)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertTrue(result.stdout.startswith("ABORT: code that is not part of the router"),
                                result.stdout)
                self.assertIn("'operator_method' on line %d" % line, result.stdout)
                self.assertEqual(target.read_text(encoding="utf-8"), text)
                self.assertEqual(entries(directory), before)

    def test_router_members_repeated_after_a_released_block_are_refused(self):
        directory = self.fresh()
        text = self.installed(self.fixture, REPEATED)
        target = self.write(directory, text)
        before = entries(directory)
        for options in (("--check",), (), ("--reverse",)):
            with self.subTest(options=options):
                result = run(target, directory, *options)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("router members repeated after the block (_classify_busy_followup)",
                              result.stdout)
                self.assertEqual(target.read_text(encoding="utf-8"), text)
                self.assertEqual(entries(directory), before)

    def test_a_decorator_on_the_anchor_is_kept_or_refused(self):
        c = self.constants
        # After a released block, the decorator belongs to the busy handler
        # and survives the upgrade directly above it.
        directory = self.fresh()
        target = self.write(directory, self.installed(self.fixture, DECORATOR))
        self.expect(target, directory, (), 0, "PATCHED_OK")
        self.assertIn(c["BLOCK"] + DECORATOR + c["ANCHOR"], target.read_text(encoding="utf-8"))
        # In an unknown region a whole-region replace would drop it.
        directory = self.fresh()
        target = self.write(directory, self.installed(self.edited_fixture(), DECORATOR))
        result = run(target, directory)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("a decorator on the busy-handler anchor", result.stdout)


if __name__ == "__main__":
    unittest.main()
