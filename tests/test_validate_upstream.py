"""The pinned-source gate must fail loudly and actionably on a wrong fixture.

These refusal paths run without a fixture in the offline suite. A separate
pinned-source CI job verifies the real revision's happy path; these tests
remain independent of network availability.
"""
import hashlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import validate_upstream


VALIDATOR = Path(__file__).with_name("validate_upstream.py")
PIN = "d7b36070ef807841699ad32c5b6af547fee3ff64"


def validate(source):
    return subprocess.run([sys.executable, str(VALIDATOR), str(source)],
                          capture_output=True, text=True, check=False)


class ValidateUpstreamTests(unittest.TestCase):
    def test_pinned_download_retries_only_rate_limits_and_still_verifies_bytes(self):
        workflow = VALIDATOR.parents[1] / ".github/workflows/offline.yml"
        code = textwrap.dedent(workflow.read_text(encoding="utf-8").split("python - <<'PY'\n", 1)[1].rsplit("          PY", 1)[0])
        payloads = {"base.py": b"synthetic base", "run.py": b"synthetic runner"}
        hashes = {name: hashlib.sha256(raw).hexdigest() for name, raw in payloads.items()}
        def limited(value=None):
            return HTTPError("https://raw.githubusercontent.com/", 429, "rate limited", {"Retry-After": value} if value is not None else {}, None)
        cases = (
            ("success-after-rate-limit", [limited("7"), io.BytesIO(payloads["base.py"]), io.BytesIO(payloads["run.py"])], [7], None),
            ("bounded-rate-limit", [limited("999"), limited(), limited()], [10, 10], HTTPError),
            ("permanent-http-error", [HTTPError("https://raw.githubusercontent.com/", 404, "not found", {}, None)], [], HTTPError),
            ("wrong-hash", [io.BytesIO(b"different source")], [], SystemExit),
            ("oversize", [io.BytesIO(b"x" * 5_000_001)], [], SystemExit),
        )
        for name, responses, delays, failure in cases:
            expected_hashes = dict(hashes)
            if name == "oversize":
                expected_hashes["base.py"] = hashlib.sha256(responses[0].getvalue()).hexdigest()
            with self.subTest(case=name), patch.object(validate_upstream, "HASHES", expected_hashes), \
                    patch("urllib.request.urlopen", side_effect=responses) as download, \
                    patch("time.sleep") as sleep, patch("subprocess.run") as lifecycle:
                if failure is None:
                    def inspect(arguments, **kwargs):
                        directory = Path(arguments[-1])
                        self.assertEqual({path.name: path.read_bytes() for path in directory.iterdir()}, payloads)
                        self.assertTrue(kwargs["check"])
                        self.assertEqual(kwargs["timeout"], 90)
                    lifecycle.side_effect = inspect
                    exec(compile(code, str(workflow), "exec"), {})
                    lifecycle.assert_called_once()
                else:
                    with self.assertRaises(failure):
                        exec(compile(code, str(workflow), "exec"), {})
                    lifecycle.assert_not_called()
                self.assertEqual([call.args[0] for call in sleep.call_args_list], delays)
                self.assertEqual(download.call_count, len(responses))
                for call in download.call_args_list:
                    request = call.args[0]
                    self.assertIn(request.full_url, {
                        "https://api.github.com/repos/NousResearch/hermes-agent/contents/" + path + "?ref=" + PIN
                        for path in ("gateway/platforms/base.py", "gateway/run.py")})
                    self.assertEqual(request.get_header("Accept"), "application/vnd.github.raw+json")
                    self.assertIsNone(request.get_header("Authorization"))
                    self.assertEqual(call.kwargs["timeout"], 20)

    def test_wrong_fixture_layout_is_refused_with_the_revision_named(self):
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td)
            checkout = directory / "checkout"
            (checkout / "gateway" / "platforms").mkdir(parents=True)
            (checkout / "gateway" / "platforms" / "base.py").write_text("x = 1\n")
            (checkout / "gateway" / "run.py").write_text("y = 2\n")
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
            (stray / "base.py").write_text("x = 1\n")
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
            (directory / "base.py").write_text("# not the pinned revision\n")
            (directory / "run.py").write_text("# not the pinned revision\n")

            result = validate(directory)

            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertIn("base.py does not match supported public revision " + PIN, result.stderr)
            self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
