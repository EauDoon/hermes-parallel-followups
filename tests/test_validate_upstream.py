"""The pinned-source gate must fail loudly and actionably on a wrong fixture.

These refusal paths run without a fixture in the offline suite. A separate
pinned-source CI job verifies the real revision's happy path; these tests
remain independent of network availability.
"""
import contextlib
import hashlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import fetch_pinned_source
import validate_upstream


VALIDATOR = Path(__file__).with_name("validate_upstream.py")
PIN = "d7b36070ef807841699ad32c5b6af547fee3ff64"


def validate(source):
    return subprocess.run([sys.executable, str(VALIDATOR), str(source)],
                          capture_output=True, text=True, check=False)


class ValidateUpstreamTests(unittest.TestCase):
    def test_pinned_download_retries_only_rate_limits_and_still_verifies_bytes(self):
        names = list(validate_upstream.SOURCES)
        payloads = {name: b"synthetic " + name.encode() for name in names}
        hashes = {name: hashlib.sha256(raw).hexdigest() for name, raw in payloads.items()}
        urls = {"https://api.github.com/repos/NousResearch/hermes-agent/contents/" + path + "?ref=" + PIN
                for path in validate_upstream.SOURCES.values()}
        def limited(value=None):
            return HTTPError("https://api.github.com/", 429, "rate limited", {"Retry-After": value} if value is not None else {}, None)
        cases = (
            ("success-after-rate-limit", [limited("7"), *(io.BytesIO(payloads[name]) for name in names)], [7], None),
            ("bounded-rate-limit", [limited("999"), limited(), limited()], [10, 10], HTTPError),
            ("permanent-http-error", [HTTPError("https://api.github.com/", 404, "not found", {}, None)], [], HTTPError),
            ("wrong-hash", [io.BytesIO(b"different source")], [], SystemExit),
            ("wrong-hash-after-a-good-file", [io.BytesIO(payloads[names[0]]), io.BytesIO(b"different source")], [], SystemExit),
            ("oversize", [io.BytesIO(b"x" * 5_000_001)], [], SystemExit),
        )
        for name, responses, delays, failure in cases:
            expected_hashes = dict(hashes)
            if name == "oversize":
                expected_hashes[names[0]] = hashlib.sha256(responses[0].getvalue()).hexdigest()
            with self.subTest(case=name), tempfile.TemporaryDirectory() as td, \
                    patch.object(validate_upstream, "HASHES", expected_hashes), \
                    patch("urllib.request.urlopen", side_effect=responses) as download, \
                    patch("time.sleep") as sleep, patch("subprocess.run") as lifecycle, \
                    contextlib.redirect_stdout(io.StringIO()):
                directory = Path(td) / "fixture"
                if failure is None:
                    def inspect(arguments, **kwargs):
                        self.assertEqual(Path(arguments[-1]), directory)
                        self.assertEqual({path.name: path.read_bytes() for path in directory.iterdir()}, payloads)
                        self.assertTrue(kwargs["check"])
                        self.assertEqual(kwargs["timeout"], 90)
                    lifecycle.side_effect = inspect
                    fetch_pinned_source.main([str(directory), "--validate"])
                    lifecycle.assert_called_once()
                else:
                    with self.assertRaises(failure):
                        fetch_pinned_source.main([str(directory), "--validate"])
                    lifecycle.assert_not_called()
                    # Every file is verified before any is written.
                    self.assertFalse(directory.exists())
                self.assertEqual([call.args[0] for call in sleep.call_args_list], delays)
                self.assertEqual(download.call_count, len(responses))
                for call in download.call_args_list:
                    request = call.args[0]
                    self.assertIn(request.full_url, urls)
                    self.assertEqual(request.get_header("Accept"), "application/vnd.github.raw+json")
                    self.assertIsNone(request.get_header("Authorization"))
                    self.assertEqual(call.kwargs["timeout"], 20)

    def test_fetch_without_validate_writes_the_verified_files_only(self):
        payloads = {name: b"synthetic " + name.encode() for name in validate_upstream.SOURCES}
        hashes = {name: hashlib.sha256(raw).hexdigest() for name, raw in payloads.items()}
        with tempfile.TemporaryDirectory() as td, \
                patch.object(validate_upstream, "HASHES", hashes), \
                patch("urllib.request.urlopen", side_effect=[io.BytesIO(raw) for raw in payloads.values()]), \
                patch("subprocess.run") as lifecycle, contextlib.redirect_stdout(io.StringIO()) as output:
            directory = Path(td) / "fixture"
            fetch_pinned_source.main([str(directory)])
            self.assertEqual({path.name: path.read_bytes() for path in directory.iterdir()}, payloads)
        lifecycle.assert_not_called()
        self.assertEqual(output.getvalue().strip(), "PINNED_SOURCE_OK " + PIN)

    def test_ci_runs_the_shared_download_script(self):
        # The download used to live in a YAML heredoc that only CI could run
        # and these tests could reach only by string-splitting the workflow.
        workflow = (VALIDATOR.parents[1] / ".github" / "workflows" / "offline.yml").read_text(encoding="utf-8")
        self.assertIn('python tests/fetch_pinned_source.py "$RUNNER_TEMP/upstream-fixture" --validate', workflow)
        self.assertNotIn("<<", workflow)

    def test_wrong_fixture_layout_is_refused_with_the_revision_named(self):
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td)
            checkout = directory / "checkout"
            (checkout / "gateway" / "platforms").mkdir(parents=True)
            (checkout / "gateway" / "platforms" / "base.py").write_text("x = 1\n", encoding="utf-8")
            (checkout / "gateway" / "run.py").write_text("y = 2\n", encoding="utf-8")
            empty = directory / "empty"
            empty.mkdir()
            cases = {
                "not-a-directory": directory / "absent",
                "empty-directory": empty,
                "checkout-tree": checkout,
                "truncated-file": None,  # filled in below
            }
            stray = directory / "stray"
            stray.mkdir()
            (stray / "base.py").write_text("x = 1\n", encoding="utf-8")
            cases["truncated-file"] = stray

            for name, source in cases.items():
                with self.subTest(case=name):
                    result = validate(source)

                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertNotIn("Traceback", result.stderr)
                    self.assertIn("VALIDATION_FAILED", result.stderr)
                    self.assertIn(PIN, result.stderr)
                    self.assertNotIn("Errno", result.stderr)

    def test_drifted_source_names_the_file_and_the_revision(self):
        # A file that is present but is not the pinned revision is the staleness
        # case this gate exists for. The content here is synthetic and is
        # expected to fail on the hash, never on the layout check.
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td)
            (directory / "base.py").write_text("# not the pinned revision\n", encoding="utf-8")
            (directory / "run.py").write_text("# not the pinned revision\n", encoding="utf-8")

            result = validate(directory)

            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertIn("base.py does not match supported public revision " + PIN, result.stderr)
            self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
