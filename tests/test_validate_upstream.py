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
import textwrap
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


# Minimal synthetic upstream that matches the pin's shapes: AnnAssign stores,
# a keyword-only adapter, a staticmethod reply anchor, and _adapter_for_source
# inherited from the mixin that comes first in GatewayRunner's bases.
CONTRACT_BASE = textwrap.dedent("""
    class BasePlatformAdapter:
        def __init__(self):
            self._text_debounce: dict = {}
            self._busy_session_handler = None

        def set_busy_session_handler(self, handler):
            self._busy_session_handler = handler

        async def _send_with_retry(self, chat_id, content, reply_to=None, metadata=None, max_retries=2):
            return None
""")
CONTRACT_RUN = textwrap.dedent("""
    from gateway.authz_mixin import GatewayAuthorizationMixin


    class GatewayRunner(GatewayAuthorizationMixin, OtherMixin):
        def __init__(self):
            self._background_tasks: set = set()

        def _wire(self, adapter):
            adapter.set_busy_session_handler(self._handle_active_session_busy_message)

        async def _handle_active_session_busy_message(self, event, session_key):
            return False

        def _queue_depth(self, session_key, *, adapter=None):
            return 0

        def _queue_or_replace_pending_event(self, session_key, event):
            return None

        async def _run_background_task(self, prompt, source, task_id, event_message_id=None, media_urls=None):
            return None

        def _thread_metadata_for_source(self, source, reply_to_message_id=None):
            return {}

        @staticmethod
        def _reply_anchor_for_event(event):
            return None
""")
CONTRACT_AUTHZ = textwrap.dedent("""
    class GatewayAuthorizationMixin:
        def _adapter_for_source(self, source):
            return None
""")


class UpstreamContractTests(unittest.TestCase):
    def sources(self, **replacements):
        """The synthetic set, with (old, new) replacements applied per file."""
        files = {"base.py": CONTRACT_BASE, "run.py": CONTRACT_RUN, "authz_mixin.py": CONTRACT_AUTHZ}
        for name, (old, new) in replacements.items():
            name = name.replace("_py", ".py")
            self.assertEqual(files[name].count(old), 1, (name, old))
            files[name] = files[name].replace(old, new, 1)
        return files

    def test_pinned_shapes_and_their_plain_forms_pass(self):
        variants = {
            "as-pinned": {},
            "plain-assign-and-method-anchor": {
                "base_py": ("self._text_debounce: dict = {}", "self._text_debounce = {}"),
                "run_py": ("    @staticmethod\n    def _reply_anchor_for_event(event):",
                           "    def _reply_anchor_for_event(self, event):"),
            },
            "runner-defines-the-resolver": {
                "run_py": ("class GatewayRunner(GatewayAuthorizationMixin, OtherMixin):",
                           "class GatewayRunner(OtherMixin):\n"
                           "    def _adapter_for_source(self, source):\n"
                           "        return None\n"),
            },
        }
        for name, replacements in variants.items():
            with self.subTest(variant=name):
                files = self.sources(**replacements)
                validate_upstream.verify_contract(files["base.py"], files["run.py"], files["authz_mixin.py"])

    def test_each_upstream_drift_is_named(self):
        mutations = {
            "no-event-message-id": (
                {"run_py": ("task_id, event_message_id=None, media_urls=None", "task_id, media_urls=None")},
                "run.py: GatewayRunner._run_background_task has no parameter event_message_id"),
            "no-adapter-keyword": (
                {"run_py": ("def _queue_depth(self, session_key, *, adapter=None):",
                            "def _queue_depth(self, session_key):")},
                "run.py: GatewayRunner._queue_depth has no parameter adapter"),
            "no-metadata": (
                {"base_py": ("reply_to=None, metadata=None,", "reply_to=None,")},
                "base.py: BasePlatformAdapter._send_with_retry has no parameter metadata"),
            "mixin-method-missing": (
                {"authz_mixin_py": ("def _adapter_for_source(self, source):", "def _adapter_for(self, source):")},
                "authz_mixin.py: GatewayAuthorizationMixin._adapter_for_source is missing"),
            "runner-not-inheriting-the-mixin": (
                {"run_py": ("class GatewayRunner(GatewayAuthorizationMixin, OtherMixin):",
                            "class GatewayRunner(OtherMixin):")},
                "run.py: GatewayRunner does not inherit _adapter_for_source from GatewayAuthorizationMixin"),
            "handler-wrapped-in-a-lambda": (
                {"run_py": ("adapter.set_busy_session_handler(self._handle_active_session_busy_message)",
                            "adapter.set_busy_session_handler("
                            "lambda event, key: self._handle_active_session_busy_message(event, key))")},
                "passes set_busy_session_handler something other than self._handle_active_session_busy_message"),
            "debounce-store-renamed": (
                {"base_py": ("self._text_debounce: dict = {}", "self._debounce_buffers: dict = {}")},
                "base.py: BasePlatformAdapter no longer assigns self._text_debounce"),
            "handler-not-stored": (
                {"base_py": ("self._busy_session_handler = handler", "self._handler = handler")},
                "set_busy_session_handler no longer stores its handler"),
            "anchor-needs-two-arguments": (
                {"run_py": ("def _reply_anchor_for_event(event):", "def _reply_anchor_for_event(event, chat):")},
                "run.py: GatewayRunner._reply_anchor_for_event requires chat"),
        }
        for name, (replacements, message) in mutations.items():
            with self.subTest(mutation=name):
                files = self.sources(**replacements)
                with self.assertRaises(ValueError) as raised:
                    validate_upstream.verify_contract(files["base.py"], files["run.py"], files["authz_mixin.py"])
                self.assertIn(message, str(raised.exception))
                self.assertIn(PIN, str(raised.exception))

    def test_missing_runner_class_fails_cleanly_with_the_revision_named(self):
        # Unreachable at the pin, where the hash check comes first, but this
        # is exactly what a pin bump meets. It used to end in a StopIteration
        # traceback from a bare next().
        files = self.sources(run_py=("class GatewayRunner(", "class GatewayService("))
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td)
            for name, text in files.items():
                (directory / name).write_text(text, encoding="utf-8")
            hashes = {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in files}
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(validate_upstream, "HASHES", hashes), patch("subprocess.run") as lifecycle, \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = validate_upstream.cli([str(directory)])
        self.assertEqual(code, 1)
        lifecycle.assert_not_called()
        self.assertIn("VALIDATION_FAILED: run.py: class GatewayRunner is missing", stderr.getvalue())
        self.assertIn(PIN, stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertNotIn("PINNED_CONTRACT_OK", stdout.getvalue())
        with self.assertRaises(ValueError) as raised:
            validate_upstream.selected("x = 1\n", "GatewayRunner", {"_queue_depth"}, "run.py")
        self.assertIn(PIN, str(raised.exception))

    def test_the_fixture_has_three_files_with_base_first(self):
        # base.py stays first so a drifted fixture is reported on base.py.
        self.assertEqual(list(validate_upstream.HASHES), ["base.py", "run.py", "authz_mixin.py"])
        self.assertEqual(list(validate_upstream.SOURCES), list(validate_upstream.HASHES))
        self.assertEqual(validate_upstream.SOURCES["authz_mixin.py"], "gateway/authz_mixin.py")


if __name__ == "__main__":
    unittest.main()
