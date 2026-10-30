#!/usr/bin/env python3
"""--status reports what an operator used to work out by hand, and writes nothing.

The README asked operators to compare the installed file with every recovery
copy, record hashes, and work out which generation holds the intended source.
Full recovery slots showed up only when a write aborted, and nothing said
whether a file was the one supported Hermes revision. --status prints a
STATUS line with the layout the installer sees, then the generation, whether
the reconstructed unpatched file is the pinned revision's, and every recovery
copy with its hash and slot usage.
"""
import ast
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_patch_installers import ROOT, string_constants, unpatched_source
import validate_upstream


ROUTER = ROOT / "patches" / "apply_busy_overflow_router_patch.py"
DEBOUNCE = ROOT / "patches" / "apply_debounce_fifo_patch.py"
FIXTURE = ROOT / "tests" / "fixtures" / "router_block_1.0.0.txt"
PINNED_FILE = {ROUTER: "run.py", DEBOUNCE: "base.py"}
BACKUP = {ROUTER: ".bak-pre-overflowrouter", DEBOUNCE: ".bak-pre-debouncefifo"}


def run(script, target, *options, pycache=None):
    env = dict(os.environ)
    if pycache is not None:
        env["PYTHONPYCACHEPREFIX"] = str(pycache)
    return subprocess.run([sys.executable, str(script), str(target), *options],
                          check=False, capture_output=True, text=True, env=env)


def report(result):
    """The STATUS state and the key: value lines, with copy lines as a list."""
    lines = result.stdout.splitlines()
    fields, copies = {}, []
    for line in lines[1:]:
        key, _, value = line.partition(": ")
        if key == "copy":
            copies.append(value)
        else:
            fields[key] = value
    return lines[0], fields, copies


def unpatched(script):
    if script == ROUTER:
        return unpatched_source(string_constants(ROUTER), "HOOK_OLD", "_maybe_route_overflow_to_background")
    return unpatched_source(string_constants(DEBOUNCE), "OLD", "_queue_or_replace_pending_event")


def router_install(block, source=None):
    c = string_constants(ROUTER)
    source = unpatched(ROUTER) if source is None else source
    return source.replace(c["HOOK_OLD"], c["HOOK_NEW"], 1).replace(c["ANCHOR"], block + c["ANCHOR"], 1)


def debounce_previous(body_name):
    c = string_constants(DEBOUNCE)
    guard = c["OLD"][:c["OLD"].index("        state = store.pop(session_key, None)\n")]
    return unpatched(DEBOUNCE).replace(c["OLD"], guard + c[body_name], 1)


class StatusTests(unittest.TestCase):
    def fresh(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        return Path(holder.name)

    def status(self, script, text, directory=None, crlf=False):
        directory = directory or self.fresh()
        target = directory / "target.py"
        target.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))
        result = run(script, target, "--status")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return report(result)

    def pinned_to(self, script, text, directory):
        """A copy of ``script`` whose pin is the sha256 of ``text``.

        The synthetic sources are not the pinned revision. Pinning a copy to
        one of them proves that every state reconstructs the exact unpatched
        bytes, which the real pin then checks in the pinned-source gate.
        """
        source = script.read_text(encoding="utf-8")
        pinned = string_constants(script)["PINNED_SHA256"]
        self.assertEqual(source.count(pinned), 1)
        copy = directory / script.name
        copy.write_text(source.replace(pinned, hashlib.sha256(text.encode("utf-8")).hexdigest()),
                        encoding="utf-8", newline="\n")
        return copy

    def test_every_router_state(self):
        c = string_constants(ROUTER)
        fixture = FIXTURE.read_bytes().decode("utf-8")
        edited = fixture.replace("_OVR_MIN_CHARS = 25", "_OVR_MIN_CHARS = 26", 1)
        current = router_install(c["BLOCK"])
        cases = (
            ("UNPATCHED", "none", unpatched(ROUTER)),
            ("CURRENT", "current", current),
            ("PREVIOUS", "1.0.0", router_install(fixture)),
            ("EDITED", "unknown", router_install(edited)),
            ("MALFORMED", "unknown", current + "\n" + c["ANCHOR"] + "\n        pass\n"),
            ("UNRECOGNIZED", "none", "# unsupported source\n"),
        )
        for state, generation, text in cases:
            with self.subTest(state=state):
                first, fields, copies = self.status(ROUTER, text)
                self.assertEqual(first, "STATUS " + state)
                self.assertEqual(fields["generation"], generation)
                self.assertEqual(fields["installer"],
                                 "apply_busy_overflow_router_patch.py " + c["__version__"])
                self.assertEqual(fields["target_sha256"], hashlib.sha256(text.encode("utf-8")).hexdigest())
                verdict = "unknown" if state in ("EDITED", "MALFORMED") else "differs from pinned revision d7b36070"
                self.assertEqual(fields["source"], verdict)
                for key in ("backup_slots", "upgrade_slots", "reverse_slots"):
                    self.assertEqual(fields[key], "0/8")
                self.assertEqual(copies, [])

    def test_every_debounce_state(self):
        c = string_constants(DEBOUNCE)
        current = unpatched(DEBOUNCE).replace(c["OLD"], c["NEW"], 1)
        cases = (
            ("UNPATCHED", "none", unpatched(DEBOUNCE)),
            ("CURRENT", "current", current),
            ("PREVIOUS", "legacy flush body", debounce_previous("PREVIOUS_NEW")),
            ("PREVIOUS", "media-fixed flush body", debounce_previous("MEDIA_FIXED_NEW")),
            ("MALFORMED", "unknown", current + "\n\nclass Second:\n    async def _flush_text_debounce_now(self, session_key):\n" + c["NEW"]),
            ("UNRECOGNIZED", "none", "# unsupported source\n"),
        )
        for state, generation, text in cases:
            with self.subTest(state=state, generation=generation):
                first, fields, copies = self.status(DEBOUNCE, text)
                self.assertEqual(first, "STATUS " + state)
                self.assertEqual(fields["generation"], generation)
                verdict = "unknown" if state == "MALFORMED" else "differs from pinned revision d7b36070"
                self.assertEqual(fields["source"], verdict)
                self.assertEqual(copies, [])

    def test_source_verdict_reconstructs_the_exact_unpatched_bytes(self):
        fixture = FIXTURE.read_bytes().decode("utf-8")
        c = string_constants(ROUTER)
        d = string_constants(DEBOUNCE)
        cases = (
            (ROUTER, unpatched(ROUTER), "UNPATCHED"),
            (ROUTER, router_install(c["BLOCK"]), "CURRENT"),
            (ROUTER, router_install(fixture), "PREVIOUS"),
            (DEBOUNCE, unpatched(DEBOUNCE), "UNPATCHED"),
            (DEBOUNCE, unpatched(DEBOUNCE).replace(d["OLD"], d["NEW"], 1), "CURRENT"),
            (DEBOUNCE, debounce_previous("PREVIOUS_NEW"), "PREVIOUS"),
        )
        for script, text, state in cases:
            for crlf in (False, True):
                with self.subTest(script=script.name, state=state, crlf=crlf):
                    directory = self.fresh()
                    copy = self.pinned_to(script, unpatched(script), directory)
                    first, fields, _copies = self.status(copy, text, directory, crlf)
                    self.assertEqual(first, "STATUS " + state)
                    self.assertEqual(fields["source"], "matches pinned revision d7b36070")

    def test_recovery_copies_and_slot_usage(self):
        # Two upgrades of a 1.0.0 install and two differing reverses fill
        # one base slot, two upgrade slots and two reverse slots.
        fixture = FIXTURE.read_bytes().decode("utf-8")
        directory = self.fresh()
        target = directory / "target.py"
        backup = str(target) + BACKUP[ROUTER]
        Path(backup).write_bytes(unpatched(ROUTER).encode("utf-8"))
        for edit in ("", "# edited after the first cycle\n"):
            target.write_bytes((router_install(fixture) + edit).encode("utf-8"))
            for options in ((), ("--reverse",)):
                result = run(ROUTER, target, *options)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            if not edit:
                # Right after the first reverse the base copy is the target.
                first, fields, copies = report(run(ROUTER, target, "--status"))
                self.assertIn("target.py.bak-pre-overflowrouter regular sha256=%s equals_target=yes"
                              % hashlib.sha256(target.read_bytes()).hexdigest(), copies)
        names = sorted(path.name for path in directory.iterdir())
        before = {name: (directory / name).read_bytes() for name in names}
        pycache = directory / "pycache-probe"
        result = run(ROUTER, target, "--status", pycache=pycache)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        first, fields, copies = report(result)
        self.assertEqual(first, "STATUS UNPATCHED")
        self.assertEqual((fields["backup_slots"], fields["upgrade_slots"], fields["reverse_slots"]),
                         ("1/8", "2/8", "2/8"))
        expected = []
        for suffix in ("", ".upgrade", ".upgrade.2", ".reverse", ".reverse.2"):
            name = "target.py" + BACKUP[ROUTER] + suffix
            contents = (directory / name).read_bytes()
            expected.append("%s regular sha256=%s equals_target=%s" % (
                name, hashlib.sha256(contents).hexdigest(), "yes" if contents == target.read_bytes() else "no"))
        self.assertEqual(copies, expected)
        # Read-only: the same files with the same bytes, and no bytecode.
        self.assertEqual(sorted(path.name for path in directory.iterdir()), names)
        self.assertEqual({name: (directory / name).read_bytes() for name in names}, before)
        self.assertFalse(pycache.exists())

    def test_a_non_regular_slot_is_reported(self):
        for script in (ROUTER, DEBOUNCE):
            with self.subTest(script=script.name):
                directory = self.fresh()
                (directory / ("target.py" + BACKUP[script] + ".reverse")).mkdir()
                first, fields, copies = self.status(script, unpatched(script), directory)
                self.assertEqual(fields["reverse_slots"], "1/8")
                self.assertEqual(copies, ["target.py%s.reverse not-regular" % BACKUP[script]])

    def test_status_refuses_other_modes_but_check_reverse_still_works(self):
        for script in (ROUTER, DEBOUNCE):
            directory = self.fresh()
            target = directory / "target.py"
            target.write_bytes(unpatched(script).encode("utf-8"))
            for extra in (("--check",), ("--reverse",), ("--diff",)):
                with self.subTest(script=script.name, extra=extra):
                    result = run(script, target, "--status", *extra)
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn("--status cannot be combined", result.stderr)
                    self.assertEqual(result.stdout, "")
            result = run(script, target, "--check", "--reverse")
            self.assertEqual((result.returncode, result.stdout.strip()), (0, "ALREADY_UNPATCHED"))
            self.assertEqual(sorted(path.name for path in directory.iterdir()), ["target.py"])

    def test_status_keeps_the_target_guards(self):
        for script in (ROUTER, DEBOUNCE):
            with self.subTest(script=script.name):
                directory = self.fresh()
                missing = run(script, directory / "absent.py", "--status")
                self.assertEqual(missing.returncode, 2)
                self.assertTrue(missing.stdout.startswith("ABORT: target cannot be inspected"))
                target = directory / "target.py"
                target.write_bytes(b"\xff\xfe not utf-8\n")
                binary = run(script, target, "--status")
                self.assertEqual((binary.returncode, binary.stdout.strip()),
                                 (2, "ABORT: target must be readable UTF-8"))

    def test_embedded_pins_equal_the_pinned_source_gate(self):
        for script in (ROUTER, DEBOUNCE):
            with self.subTest(script=script.name):
                constants = string_constants(script)
                self.assertEqual(constants["PINNED_REVISION"], validate_upstream.PIN)
                self.assertEqual(constants["PINNED_SHA256"], validate_upstream.HASHES[PINNED_FILE[script]])


if __name__ == "__main__":
    unittest.main()
