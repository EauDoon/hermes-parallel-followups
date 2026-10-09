"""The injected flush must deliver every occupant the FIFO would keep.

The historical merge replaces a pending event it does not know how to
caption-merge. That is safe only for the events the FIFO itself merges in
place. Everything else has to stay on the FIFO, or the flush reports success
while the occupant is gone or a caption has been appended twice.
"""
import ast
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_patch_installers import string_constants, unpatched_source


PATCH = Path(__file__).resolve().parents[1] / "patches" / "apply_debounce_fifo_patch.py"


def load_new():
    tree = ast.parse(PATCH.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "NEW" for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("NEW block missing")


class MessageType:
    TEXT = "text"
    PHOTO = "photo"
    VIDEO = "video"
    VOICE = "voice"


class Event:
    def __init__(self, text, message_type, media=None):
        self.text = text
        self.message_type = message_type
        self.media_urls = list(media or [])
        self.media_types = []
        self.source = "source"


def merge_pending_message_event(pending, session_key, event, merge_text=False):
    """Control flow of the pinned merge_pending_message_event, trimmed to delivery."""
    existing = pending.get(session_key)
    if existing:
        existing_is_photo = getattr(existing, "message_type", None) == MessageType.PHOTO
        incoming_is_photo = event.message_type == MessageType.PHOTO
        if existing_is_photo and incoming_is_photo:
            existing.media_urls.extend(event.media_urls)
            if event.text:
                existing.text = (existing.text + "\n" + event.text) if existing.text else event.text
            return
        if existing.media_urls or event.media_urls:
            if event.media_urls:
                existing.media_urls.extend(event.media_urls)
            if event.text:
                existing.text = (existing.text + "\n" + event.text) if existing.text else event.text
            return
        if (
            merge_text
            and getattr(existing, "message_type", None) == MessageType.TEXT
            and event.message_type == MessageType.TEXT
        ):
            if event.text:
                existing.text = (existing.text + "\n" + event.text) if existing.text else event.text
            return
    pending[session_key] = event


class Runner:
    def __init__(self, adapter):
        self.adapter = adapter
        self.overflow = {}
        self.fifo_calls = 0

    def _adapter_for_source(self, source):
        return self.adapter

    def _queue_depth(self, key, adapter=None):
        depth = len(self.overflow.get(key, []))
        if key in adapter._pending_messages:
            depth += 1
        return depth

    def _queue_or_replace_pending_event(self, key, event):
        self.fifo_calls += 1
        existing = self.adapter._pending_messages.get(key)
        # The real FIFO merges in place only for a photo or for media URLs.
        if existing is not None and (
            getattr(existing, "message_type", None) == MessageType.PHOTO
            or event.message_type == MessageType.PHOTO
            or bool(getattr(existing, "media_urls", None))
            or bool(getattr(event, "media_urls", None))
        ):
            merge_pending_message_event(self.adapter._pending_messages, key, event, merge_text=True)
            return
        if key in self.adapter._pending_messages:
            self.overflow.setdefault(key, []).append(event)
        else:
            self.adapter._pending_messages[key] = event


class Adapter:
    def __init__(self, runner=None):
        self.name = "fixture"
        self._pending_messages = {}
        self._busy_session_handler = None if runner is None else (lambda event, key: False).__get__(runner)
        self.merges = 0

    def _can_merge_text_debounce_events(self, existing, event):
        return True


def flush_of(adapter):
    namespace = {
        "MessageType": MessageType,
        "logger": logging.getLogger("debounce-flush-test"),
        "merge_pending_message_event": _counting_merge(adapter),
    }
    # The real method binds ``state`` before the replaced tail.
    exec(
        "def _flush(self, store, session_key):\n"
        "        state = store.get(session_key)\n" + load_new(),
        namespace,
    )
    return namespace["_flush"].__get__(adapter)


def _counting_merge(adapter):
    def merge(pending, session_key, event, merge_text=False):
        adapter.merges += 1
        merge_pending_message_event(pending, session_key, event, merge_text=merge_text)
    return merge


class DebounceFlushTests(unittest.TestCase):
    def test_video_without_media_urls_is_kept_and_text_is_its_own_turn(self):
        adapter = Adapter()
        runner = Runner(adapter)
        adapter._busy_session_handler = (lambda event, key: False).__get__(runner)
        video = Event("clip", MessageType.VIDEO)
        adapter._pending_messages["session"] = video
        burst = Event("what is the capital of Peru", MessageType.TEXT)
        delivered = flush_of(adapter)({"session": type("State", (), {"event": burst})()}, "session")

        self.assertTrue(delivered)
        self.assertIs(adapter._pending_messages["session"], video)
        self.assertEqual([event.text for event in runner.overflow["session"]], [burst.text])
        self.assertEqual(adapter.merges, 0)
        self.assertEqual(runner.fifo_calls, 1)

    def test_incoming_media_is_caption_merged_once(self):
        adapter = Adapter()
        runner = Runner(adapter)
        adapter._busy_session_handler = (lambda event, key: False).__get__(runner)
        occupant = Event("first", MessageType.TEXT)
        adapter._pending_messages["session"] = occupant
        burst = Event("second caption", MessageType.TEXT, ["a.jpg"])
        delivered = flush_of(adapter)({"session": type("State", (), {"event": burst})()}, "session")

        self.assertTrue(delivered)
        self.assertIs(adapter._pending_messages["session"], occupant)
        self.assertEqual(occupant.text, "first\nsecond caption")
        self.assertEqual(occupant.media_urls, ["a.jpg"])
        self.assertEqual(adapter.merges, 1)
        self.assertEqual(runner.fifo_calls, 0)
        self.assertNotIn("session", runner.overflow)

    def test_photo_caption_still_merges_once_without_the_fifo(self):
        adapter = Adapter()
        photo = Event("caption", MessageType.PHOTO, ["a.jpg"])
        adapter._pending_messages["session"] = photo
        burst = Event("more caption", MessageType.TEXT)
        delivered = flush_of(adapter)({"session": type("State", (), {"event": burst})()}, "session")

        self.assertTrue(delivered)
        self.assertIs(adapter._pending_messages["session"], photo)
        self.assertEqual(photo.text, "caption\nmore caption")
        self.assertEqual(adapter.merges, 1)

    def test_missing_photo_member_does_not_treat_a_typeless_slot_as_media(self):
        # getattr(..., None) made a missing enum member compare equal to a
        # slot whose message_type is None, so the historical path replaced it.
        class BareMessageType:
            TEXT = "text"

        adapter = Adapter()
        runner = Runner(adapter)
        adapter._busy_session_handler = (lambda event, key: False).__get__(runner)
        occupant = Event("keep-me", None)
        adapter._pending_messages["session"] = occupant
        burst = Event("what is the capital of Peru", BareMessageType.TEXT)
        namespace = {
            "MessageType": BareMessageType,
            "logger": logging.getLogger("debounce-flush-test"),
            "merge_pending_message_event": _counting_merge(adapter),
        }
        exec(
            "def _flush(self, store, session_key):\n"
            "        state = store.get(session_key)\n" + load_new(),
            namespace,
        )
        delivered = namespace["_flush"](adapter, {"session": type("State", (), {"event": burst})()}, "session")

        self.assertTrue(delivered)
        self.assertIs(adapter._pending_messages["session"], occupant)
        self.assertEqual(adapter.merges, 0)

    def test_previous_flush_body_upgrades_without_touching_the_original_backup(self):
        constants = string_constants(PATCH)
        previous = constants["PREVIOUS_NEW"]
        current = constants["NEW"]
        original = unpatched_source(constants, "OLD", "_queue_or_replace_pending_event")
        # Older injectors replaced only the pop/merge tail and left the sender
        # guard above it. That is the install an upgrade has to recognize.
        tail = constants["OLD"][constants["OLD"].index("        state = store.pop(session_key, None)\n"):]
        installed = original.replace(tail, previous, 1)
        self.assertNotIn(current, installed)
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td)
            target = directory / "base.py"
            target.write_text(installed, encoding="utf-8")
            backup = Path(str(target) + ".bak-pre-debouncefifo")
            backup.write_bytes(original.encode("utf-8"))
            environment = {**os.environ, "PYTHONPYCACHEPREFIX": str(directory / "pycache")}

            check = subprocess.run(
                [sys.executable, str(PATCH), str(target), "--check"],
                check=False, capture_output=True, text=True, env=environment,
            )
            self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
            self.assertEqual(check.stdout.strip(), "UPGRADE_APPLICABLE")
            self.assertEqual(target.read_text(encoding="utf-8"), installed)
            self.assertEqual(backup.read_text(encoding="utf-8"), original)

            applied = subprocess.run(
                [sys.executable, str(PATCH), str(target)],
                check=False, capture_output=True, text=True, env=environment,
            )
            self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
            self.assertEqual(applied.stdout.strip(), "PATCHED_OK")
            upgraded = target.read_text(encoding="utf-8")
            self.assertIn(current, upgraded)
            self.assertNotIn(previous, upgraded)
            self.assertEqual(backup.read_text(encoding="utf-8"), original)
            upgrade_copy = Path(str(backup) + ".upgrade")
            self.assertEqual(upgrade_copy.read_text(encoding="utf-8"), installed)

            again = subprocess.run(
                [sys.executable, str(PATCH), str(target), "--check"],
                check=False, capture_output=True, text=True, env=environment,
            )
            self.assertEqual(again.stdout.strip(), "ALREADY_PATCHED")
            self.assertEqual(upgrade_copy.read_text(encoding="utf-8"), installed)

    def test_other_sender_is_offered_to_the_fifo_instead_of_staying_debounced(self):
        # The sender guard sits above the replaced tail and returns before the
        # FIFO is offered the burst. The timer was already cleared, so a second
        # person's follow-up stays in the debounce store until they type again.
        constants = string_constants(PATCH)
        source = (
            "import logging\n"
            "logger = logging.getLogger('fixture')\n"
            "class MessageType:\n"
            "    TEXT = 'text'\n"
            "    PHOTO = 'photo'\n"
            "class Host:\n"
            "    async def _flush_text_debounce_now(self, session_key):\n"
            "        store = self._store\n"
            "        state = store.get(session_key)\n"
            "        if state is None:\n"
            "            return False\n"
            + constants["OLD"]
        )
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td)
            target = directory / "base.py"
            target.write_text(source, encoding="utf-8")
            environment = {**os.environ, "PYTHONPYCACHEPREFIX": str(directory / "pycache")}
            applied = subprocess.run(
                [sys.executable, str(PATCH), str(target)],
                check=False, capture_output=True, text=True, env=environment,
            )
            self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
            namespace = {}
            exec(target.read_text(encoding="utf-8"), namespace)
            host = namespace["Host"]()
            host.name = "fixture"
            host._pending_messages = {}
            host._store = {}
            alice = Event("alice-question", namespace["MessageType"].TEXT)
            bob = Event("bob-question", namespace["MessageType"].TEXT)
            host._pending_messages["session"] = alice
            host._store["session"] = type("State", (), {"event": bob})()
            runner = Runner(host)
            host._busy_session_handler = (lambda event, key: False).__get__(runner)
            host._can_merge_text_debounce_events = lambda existing, event: False

            import asyncio
            delivered = asyncio.run(host._flush_text_debounce_now("session"))

            self.assertTrue(delivered)
            self.assertIs(host._pending_messages["session"], alice)
            self.assertEqual([event.text for event in runner.overflow["session"]], ["bob-question"])
            self.assertNotIn("session", host._store)

    def test_logging_failure_still_falls_back_to_the_pending_merge(self):
        # The warning sits inside the try that guards the FIFO call. When the
        # sink raises, that exception used to leave the method before the
        # historical merge, and the burst was already gone from the store.
        class RaisingLogger:
            def warning(self, *args, **kwargs):
                raise RuntimeError("log sink failed")

        def flush(enqueue):
            adapter = Adapter()
            runner = Runner(adapter)
            adapter._busy_session_handler = (lambda event, key: False).__get__(runner)
            runner._queue_or_replace_pending_event = enqueue
            occupant = Event("first", MessageType.TEXT)
            adapter._pending_messages["session"] = occupant
            burst = Event("second question about the capital", MessageType.TEXT)
            namespace = {
                "MessageType": MessageType,
                "logger": RaisingLogger(),
                "merge_pending_message_event": _counting_merge(adapter),
            }
            exec(
                "def _flush(self, store, session_key):\n"
                "        state = store.get(session_key)\n" + load_new(),
                namespace,
            )
            delivered = namespace["_flush"](
                adapter, {"session": type("State", (), {"event": burst})()}, "session",
            )
            return delivered, occupant, adapter

        cases = {
            "declined": lambda key, event: None,
            "enqueue-raised": lambda key, event: (_ for _ in ()).throw(RuntimeError("fifo down")),
        }
        for name, enqueue in cases.items():
            with self.subTest(case=name):
                delivered, occupant, adapter = flush(enqueue)
                self.assertTrue(delivered)
                self.assertEqual(occupant.text, "first\nsecond question about the capital")
                self.assertEqual(adapter.merges, 1)

    def test_depth_check_after_a_successful_enqueue_does_not_merge_again(self):
        # The growth check is how a silent decline is told from a stored
        # burst. If that read raises after the FIFO has already accepted the
        # event, the except path used to merge it into the pending slot too.
        adapter = Adapter()
        runner = Runner(adapter)
        adapter._busy_session_handler = (lambda event, key: False).__get__(runner)
        calls = {"n": 0}

        def depth(key, adapter=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return 1
            raise RuntimeError("depth unreadable")

        def enqueue(key, event):
            runner.overflow.setdefault(key, []).append(event)

        runner._queue_depth = depth
        runner._queue_or_replace_pending_event = enqueue
        occupant = Event("first", MessageType.TEXT)
        adapter._pending_messages["session"] = occupant
        burst = Event("second question about the capital", MessageType.TEXT)
        # The failed re-read is logged with its traceback. Capture it, so the
        # warning is asserted instead of printed into the suite output.
        with self.assertLogs("debounce-flush-test", "WARNING") as logs:
            delivered = flush_of(adapter)({"session": type("State", (), {"event": burst})()}, "session")

        self.assertTrue(any("not merging it again" in line for line in logs.output), logs.output)
        self.assertTrue(delivered)
        self.assertEqual(occupant.text, "first")
        self.assertEqual(adapter.merges, 0)
        self.assertEqual([event.text for event in runner.overflow["session"]], [burst.text])


if __name__ == "__main__":
    unittest.main()
