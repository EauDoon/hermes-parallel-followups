#!/usr/bin/env python3
"""Repeat install and uninstall cycles take a new recovery slot, never a dead end.

Both installers kept one base backup and one ``.reverse`` copy. Applying,
reversing, editing (or updating Hermes) and applying again aborted with exit 3
on the base copy the first cycle left, and a second reverse after a reapply
aborted the same way on ``.reverse``. Each family now has eight names: the
original, then ``.2`` to ``.8``. A copy holding exactly the bytes the write
would store is reused, a different one is kept and skipped, and a full set or
a non-regular entry still aborts before anything is written.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_patch_installers import (
    INSTALLERS, ROOT, assert_no_staging_residue, entries, string_constants, unpatched_source,
)


def run(script, target, directory, *options):
    return subprocess.run(
        [sys.executable, str(script), str(target), *options],
        check=False, capture_output=True, text=True,
        env={**os.environ, "PYTHONPYCACHEPREFIX": str(directory / "pycache")},
    )


def slot(base, ordinal):
    return Path(str(base) if ordinal == 1 else "%s.%d" % (base, ordinal))


class RecoverySlotTests(unittest.TestCase):
    def fresh(self):
        """A new empty directory for one installer and direction."""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.directory = Path(holder.name)

    def prepared(self, script_name, old_name, old_marker):
        script = ROOT / "patches" / script_name
        target = self.directory / "target.py"
        # Bytes, not write_text: the edits below append LF lines, and a
        # translated CRLF file would then have mixed line endings.
        target.write_bytes(
            unpatched_source(string_constants(script), old_name, old_marker).encode("utf-8"),
        )
        return script, target

    def expect(self, script, target, options, returncode, first_line):
        result = run(script, target, self.directory, *options)
        self.assertEqual(result.returncode, returncode, result.stdout + result.stderr)
        self.assertEqual(result.stdout.splitlines()[0], first_line)
        return result

    def test_reapply_after_reverse_and_edit_takes_the_next_base_slot(self):
        for script_name, old_name, old_marker, suffix in INSTALLERS:
            with self.subTest(script=script_name):
                self.fresh()
                script, target = self.prepared(script_name, old_name, old_marker)
                base = Path(str(target) + suffix)
                original = target.read_bytes()
                self.expect(script, target, (), 0, "PATCHED_OK")
                self.expect(script, target, ("--reverse",), 0, "REVERSED_OK")
                self.assertEqual(target.read_bytes(), original)
                # An operator edit, or a Hermes update, between the cycles.
                edited = original + b"# updated after the reverse\n"
                target.write_bytes(edited)

                before = entries(self.directory)
                self.expect(script, target, ("--check",), 0, "APPLICABLE")
                self.assertEqual(entries(self.directory), before)
                self.expect(script, target, (), 0, "PATCHED_OK")

                self.assertEqual(base.read_bytes(), original)
                self.assertEqual(slot(base, 2).read_bytes(), edited)
                self.assertFalse(slot(base, 3).exists())
                assert_no_staging_residue(self, self.directory, target)

    def test_second_reverse_takes_the_next_reverse_slot(self):
        for script_name, old_name, old_marker, suffix in INSTALLERS:
            with self.subTest(script=script_name):
                self.fresh()
                script, target = self.prepared(script_name, old_name, old_marker)
                reverse = Path(str(target) + suffix + ".reverse")
                self.expect(script, target, (), 0, "PATCHED_OK")
                first_install = target.read_bytes()
                self.expect(script, target, ("--reverse",), 0, "REVERSED_OK")
                target.write_bytes(target.read_bytes() + b"# updated after the reverse\n")
                self.expect(script, target, (), 0, "PATCHED_OK")
                second_install = target.read_bytes()
                self.assertNotEqual(second_install, first_install)

                before = entries(self.directory)
                self.expect(script, target, ("--check", "--reverse"), 0, "REVERSIBLE")
                self.assertEqual(entries(self.directory), before)
                self.expect(script, target, ("--reverse",), 0, "REVERSED_OK")

                self.assertEqual(reverse.read_bytes(), first_install)
                self.assertEqual(slot(reverse, 2).read_bytes(), second_install)
                assert_no_staging_residue(self, self.directory, target)

    def test_an_identical_copy_behind_differing_ones_is_reused(self):
        for script_name, old_name, old_marker, suffix in INSTALLERS:
            with self.subTest(script=script_name):
                self.fresh()
                script, target = self.prepared(script_name, old_name, old_marker)
                reverse = Path(str(target) + suffix + ".reverse")
                self.expect(script, target, (), 0, "PATCHED_OK")
                installed = target.read_bytes()
                slot(reverse, 1).write_bytes(b"operator copy one\n")
                slot(reverse, 2).write_bytes(b"operator copy two\n")
                slot(reverse, 3).write_bytes(installed)

                before = entries(self.directory)
                self.expect(script, target, ("--reverse",), 0, "REVERSED_OK")

                self.assertEqual(entries(self.directory), before)
                self.assertEqual(slot(reverse, 1).read_bytes(), b"operator copy one\n")
                self.assertEqual(slot(reverse, 2).read_bytes(), b"operator copy two\n")
                self.assertEqual(slot(reverse, 3).read_bytes(), installed)
                assert_no_staging_residue(self, self.directory, target)

    def test_eight_differing_copies_abort_the_check_and_the_write(self):
        for script_name, old_name, old_marker, suffix in INSTALLERS:
            for direction in ("apply", "reverse"):
                with self.subTest(script=script_name, direction=direction):
                    self.fresh()
                    script, target = self.prepared(script_name, old_name, old_marker)
                    options = ()
                    base = Path(str(target) + suffix)
                    if direction == "reverse":
                        self.expect(script, target, (), 0, "PATCHED_OK")
                        options = ("--reverse",)
                        base = Path(str(base) + ".reverse")
                    for ordinal in range(1, 9):
                        slot(base, ordinal).write_bytes(b"operator copy %d\n" % ordinal)
                    current = target.read_bytes()
                    before = entries(self.directory)

                    for extra in (options + ("--check",), options):
                        with self.subTest(options=extra):
                            result = run(script, target, self.directory, *extra)
                            self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                            self.assertTrue(result.stdout.startswith("ABORT:"), result.stdout)
                            self.assertIn("no free recovery slot", result.stdout)
                            self.assertEqual(target.read_bytes(), current)
                            self.assertEqual(entries(self.directory), before)
                            for ordinal in range(1, 9):
                                self.assertEqual(slot(base, ordinal).read_bytes(),
                                                 b"operator copy %d\n" % ordinal)
                            assert_no_staging_residue(self, self.directory, target)

    def test_help_names_every_recovery_copy_family_and_its_slots(self):
        for script_name, _old_name, _old_marker, suffix in INSTALLERS:
            with self.subTest(script=script_name):
                helped = subprocess.run(
                    [sys.executable, str(ROOT / "patches" / script_name), "--help"],
                    check=False, capture_output=True, text=True,
                )
                self.assertEqual(helped.returncode, 0, helped.stdout + helped.stderr)
                for family in ("", ".upgrade", ".reverse"):
                    self.assertIn("<target>%s%s " % (suffix, family), helped.stdout)
                self.assertIn(".2 to .8", helped.stdout)

    def test_a_directory_in_the_first_slot_still_aborts(self):
        # A directory stands in for any non-regular entry. Symlinks need a
        # privilege that Windows runners lack; test_patch_installers covers
        # them where they are available.
        for script_name, old_name, old_marker, suffix in INSTALLERS:
            with self.subTest(script=script_name):
                self.fresh()
                script, target = self.prepared(script_name, old_name, old_marker)
                Path(str(target) + suffix).mkdir()
                original = target.read_bytes()
                before = entries(self.directory)
                for extra in (("--check",), ()):
                    with self.subTest(options=extra):
                        result = run(script, target, self.directory, *extra)
                        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                        self.assertIn("not a regular file", result.stdout)
                        self.assertEqual(target.read_bytes(), original)
                        self.assertEqual(entries(self.directory), before)


if __name__ == "__main__":
    unittest.main()
