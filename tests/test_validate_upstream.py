"""The pinned-source gate must fail loudly and actionably on a wrong fixture.

These refusal paths run without a fixture in the offline suite. A separate
pinned-source CI job verifies the real revision's happy path; these tests
remain independent of network availability.
"""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


VALIDATOR = Path(__file__).with_name("validate_upstream.py")
PIN = "d7b36070ef807841699ad32c5b6af547fee3ff64"


def validate(source):
    return subprocess.run([sys.executable, str(VALIDATOR), str(source)],
                          capture_output=True, text=True, check=False)


class ValidateUpstreamTests(unittest.TestCase):
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
