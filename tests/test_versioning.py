#!/usr/bin/env python3
"""Every version surface must agree, and a release must stay verifiable.

VERSION, both installers' __version__, their --version output and the
CHANGELOG are checked against each other on the real tree. Copies of the
tree are then broken one way at a time to prove tests/check_version.py
catches each disagreement. The release workflow is checked for the one
property that matters most: its write token never shares a job with code
downloaded from upstream.
"""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "tests" / "check_version.py"
WORKFLOWS = ROOT / ".github" / "workflows"
INSTALLERS = ("apply_debounce_fifo_patch.py", "apply_busy_overflow_router_patch.py")
RELEASED = textwrap.dedent("""
    # Changelog

    ## [Unreleased]

    ## [9.8.7] - 2026-10-09

    ### Fixed

    - A synthetic fix.

    [Unreleased]: https://github.com/EauDoon/hermes-parallel-followups/compare/v9.8.7...HEAD
    [9.8.7]: https://github.com/EauDoon/hermes-parallel-followups/releases/tag/v9.8.7
""")


def check(*arguments, root=None):
    command = [sys.executable, str(CHECKER), *arguments]
    if root is not None:
        command += ["--root", str(root)]
    return subprocess.run(command, capture_output=True, text=True, check=False)


def current_version():
    return (ROOT / "VERSION").read_text(encoding="utf-8").strip()


def job_blocks(workflow):
    """The text before `jobs:` and each job's own lines, keyed by job id."""
    head, _, body = workflow.partition("\njobs:\n")
    blocks, name = {}, None
    for line in body.splitlines():
        if line.startswith("  ") and not line.startswith("   ") and line.rstrip().endswith(":"):
            name = line.strip()[:-1]
            blocks[name] = []
        elif name is not None:
            blocks[name].append(line)
    return head, {job: "\n".join(lines) for job, lines in blocks.items()}


class VersioningTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.copy = Path(directory.name)
        (self.copy / "patches").mkdir()
        for name in INSTALLERS:
            shutil.copyfile(ROOT / "patches" / name, self.copy / "patches" / name)
        for name in ("VERSION", "CHANGELOG.md"):
            shutil.copyfile(ROOT / name, self.copy / name)

    def set_version(self, version, installers=INSTALLERS):
        old = '__version__ = "%s"' % current_version()
        for name in installers:
            path = self.copy / "patches" / name
            text = path.read_text(encoding="utf-8")
            self.assertEqual(text.count(old), 1)
            path.write_text(text.replace(old, '__version__ = "%s"' % version), encoding="utf-8")
        if installers == INSTALLERS:
            (self.copy / "VERSION").write_text(version + "\n", encoding="utf-8")

    def write_changelog(self, text):
        (self.copy / "CHANGELOG.md").write_text(text, encoding="utf-8")

    def assert_mismatch(self, result, reason):
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("VERSION_MISMATCH: ", result.stderr)
        self.assertIn(reason, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_the_real_tree_agrees(self):
        version = current_version()
        result = check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "VERSION_OK " + version)
        for name in INSTALLERS:
            with self.subTest(installer=name):
                printed = subprocess.run([sys.executable, str(ROOT / "patches" / name), "--version"],
                                         capture_output=True, text=True, check=False)
                self.assertEqual(printed.returncode, 0, printed.stderr)
                self.assertEqual(printed.stdout.strip(), "%s %s" % (name, version))

    def test_a_released_version_passes_with_its_tag_and_notes(self):
        self.set_version("9.8.7")
        self.write_changelog(RELEASED)
        self.assertEqual(check(root=self.copy).stdout.strip(), "VERSION_OK 9.8.7")
        self.assertEqual(check("--tag", "v9.8.7", root=self.copy).returncode, 0)
        notes = check("--notes", root=self.copy)
        self.assertEqual(notes.returncode, 0, notes.stderr)
        self.assertEqual(notes.stdout, "### Fixed\n\n- A synthetic fix.\n")

    def test_each_disagreement_is_refused(self):
        cases = {
            "installer-drift": (lambda: self.set_version("9.9.9", INSTALLERS[:1]), (),
                                "patches/%s has __version__ '9.9.9'" % INSTALLERS[0]),
            "no-unreleased-section": (
                lambda: self.write_changelog("# Changelog\n"), (), "no '## [Unreleased]' section"),
            "missing-release-section": (
                lambda: (self.set_version("9.8.8"), self.write_changelog(RELEASED)), (),
                "no '## [9.8.8] - YYYY-MM-DD' section"),
            "day-first-date": (
                lambda: (self.set_version("9.8.7"),
                         self.write_changelog(RELEASED.replace("2026-10-09", "09-10-2026"))), (),
                "needs an ISO date"),
            "impossible-date": (
                lambda: (self.set_version("9.8.7"),
                         self.write_changelog(RELEASED.replace("2026-10-09", "2026-13-40"))), (),
                "is not a valid date"),
            "missing-link-reference": (
                lambda: (self.set_version("9.8.7"),
                         self.write_changelog(RELEASED.rsplit("[9.8.7]: ", 1)[0])), (),
                "lacks the link reference"),
            "tag-mismatch": (
                lambda: (self.set_version("9.8.7"), self.write_changelog(RELEASED)), ("--tag", "v9.8.6"),
                "tag v9.8.6 does not match VERSION 9.8.7"),
            "prerelease-tag": (
                lambda: self.set_version("9.8.8-dev"), ("--tag", "v9.8.8-dev"),
                "only final versions are released"),
            "dev-after-its-release": (
                lambda: (self.set_version("9.8.7-dev"), self.write_changelog(RELEASED)), (),
                "already records as released"),
            "not-semver": (lambda: self.set_version("1.0"), (), "VERSION must hold exactly one SemVer line"),
            "notes-for-a-prerelease": (lambda: self.set_version("9.8.8-dev"), ("--notes",),
                                       "release notes exist only for a final version"),
        }
        for name, (arrange, arguments, reason) in cases.items():
            with self.subTest(case=name):
                self.setUp()
                arrange()
                self.assert_mismatch(check(*arguments, root=self.copy), reason)

    def test_release_workflow_keeps_its_token_away_from_upstream_code(self):
        workflow = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
        head, jobs = job_blocks(workflow)
        self.assertEqual(set(jobs), {"verify", "publish"})
        self.assertIn("tags: ['v*.*.*']", head)
        self.assertIn("permissions:\n  contents: read", head)
        self.assertNotIn("contents: write", head)
        self.assertNotIn("contents: write", jobs["verify"])
        self.assertIn("contents: write", jobs["publish"])
        self.assertIn("needs: verify", jobs["publish"])
        for upstream in ("fetch_pinned_source", "validate_upstream", "run_offline"):
            self.assertNotIn(upstream, jobs["publish"])
        self.assertIn('fetch_pinned_source.py "$RUNNER_TEMP/upstream-fixture" --validate', jobs["verify"])
        self.assertIn("run_offline.py", jobs["verify"])
        for job in jobs.values():
            self.assertIn('check_version.py --tag "$GITHUB_REF_NAME"', job)
            self.assertIn("persist-credentials: false", job)
        self.assertNotIn("github.token", jobs["verify"])
        self.assertIn("SHA256SUMS", jobs["publish"])

    def test_release_pins_match_the_check_workflow(self):
        # One reviewed SHA per action. Dependabot updates both files together.
        def pins(name):
            return {line.strip() for line in (WORKFLOWS / name).read_text(encoding="utf-8").splitlines()
                    if line.strip().startswith("- uses: ")}
        self.assertEqual(pins("release.yml"), pins("offline.yml"))

    def test_check_workflow_does_not_run_on_tags(self):
        # A pushed tag is verified by release.yml alone.
        workflow = (WORKFLOWS / "offline.yml").read_text(encoding="utf-8")
        self.assertIn("on:\n  push:\n    branches: [main]\n", workflow)


if __name__ == "__main__":
    unittest.main()
