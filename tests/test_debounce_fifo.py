#!/usr/bin/env python3
"""Drive the REAL patched _flush_text_debounce_now and assert two successive
bursts become two separate turns instead of one newline-merged turn.

Run inside the container: python3 /tmp/test_debounce_fifo.py
"""
import sys, asyncio, types
sys.path.insert(0, "/opt/hermes")

from gateway.platforms.base import (
    BasePlatformAdapter, TextDebounceState, MessageType,
)
from gateway.run import GatewayRunner

FAILS = []


def chk(cond, label):
    print("  %s %s" % ("PASS" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)


class Src:
    platform = "telegram"; chat_id = "1"; chat_type = "dm"; thread_id = None
    user_id = "u1"; user_id_alt = None; user_name = "d"; chat_name = "c"


class Ev:
    def __init__(self, text):
        self.text = text; self.message_type = MessageType.TEXT
        self.media_urls = []; self.media_types = []
        self.source = Src(); self.message_id = "1"; self.reply_to_message_id = None


class _Adapter(BasePlatformAdapter):
    """Concrete stub - BasePlatformAdapter is abstract and `name` is a property."""
    name = "test"
    async def connect(self): return True
    async def disconnect(self): return None
    async def get_chat_info(self, chat_id): return {}
    async def send(self, *a, **kw): return None


class StubRunner:
    """Minimal runner exposing the real FIFO methods under test."""
    _BUSY_QUEUE_MAX_PENDING = GatewayRunner._BUSY_QUEUE_MAX_PENDING
    _queue_or_replace_pending_event = GatewayRunner._queue_or_replace_pending_event
    _enqueue_fifo = GatewayRunner._enqueue_fifo
    _queue_depth = GatewayRunner._queue_depth

    def __init__(self, adapter):
        self._adapter = adapter
        self._queued_events = {}

    def _adapter_for_source(self, source):
        return self._adapter

    async def busy_handler(self, event, session_key):
        return False


def make_adapter():
    a = object.__new__(_Adapter)
    # name is a class attr on _Adapter
    a._text_debounce = {}
    a._pending_messages = {}
    runner = StubRunner(a)
    a._busy_session_handler = runner.busy_handler  # bound -> exposes runner
    return a, runner


async def flush(adapter, key, text):
    adapter._text_debounce[key] = TextDebounceState(
        event=Ev(text), task=None, first_ts=0.0, last_ts=0.0
    )
    return await adapter._flush_text_debounce_now(key)


async def main():
    KEY = "sess"
    a, runner = make_adapter()

    print("=== two successive bursts ===")
    await flush(a, KEY, "what is the capital of Mongolia")
    await flush(a, KEY, "what is the capital of Peru")

    slot = a._pending_messages.get(KEY)
    overflow = runner._queued_events.get(KEY, [])

    chk(slot is not None, "first burst landed in the pending slot")
    chk(slot is not None and "\n" not in (slot.text or ""),
        "first burst NOT newline-merged")
    chk(slot is not None and slot.text == "what is the capital of Mongolia",
        "first burst text intact")
    chk(len(overflow) == 1, "second burst went to FIFO overflow (own turn)")
    chk(len(overflow) == 1 and overflow[0].text == "what is the capital of Peru",
        "second burst text intact and separate")

    depth = runner._queue_depth(KEY, adapter=a)
    chk(depth == 2, "queue depth counts both as separate turns (got %d)" % depth)

    print("=== third burst also separate ===")
    await flush(a, KEY, "what is the capital of Chad")
    overflow = runner._queued_events.get(KEY, [])
    chk(len(overflow) == 2, "third burst appended to FIFO (got %d)" % len(overflow))
    merged_any = any("\n" in (e.text or "") for e in [a._pending_messages[KEY]] + overflow)
    chk(not merged_any, "no newline merge anywhere across three bursts")

    print("=== fallback when the runner cannot resolve this adapter ===")
    # _queue_or_replace_pending_event returns early without queueing when
    # _adapter_for_source yields nothing. Delegating into that would DROP the
    # burst, so the patch must fall back to the merge instead.
    c, c_runner = make_adapter()
    c_runner._adapter_for_source = lambda source: None
    await flush(c, KEY, "what is the capital of Mongolia")
    await flush(c, KEY, "what is the capital of Peru")
    slot_c = c._pending_messages.get(KEY)
    chk(slot_c is not None, "unresolvable adapter: burst not dropped")
    chk(slot_c is not None and "\n" in (slot_c.text or ""),
        "unresolvable adapter: fell back to merge rather than losing it")
    chk(not c_runner._queued_events.get(KEY),
        "unresolvable adapter: nothing half-enqueued")

    print("=== fallback when the FIFO silently declines (pending cap) ===")
    # _queue_or_replace_pending_event returns WITHOUT queueing and WITHOUT
    # raising once _BUSY_QUEUE_MAX_PENDING is hit. Treating that as success
    # would drop the burst, so the flush must notice the queue did not grow.
    d, d_runner = make_adapter()
    d_runner._queue_or_replace_pending_event = lambda key, ev: None
    await flush(d, KEY, "what is the capital of Mongolia")
    await flush(d, KEY, "what is the capital of Peru")
    slot_d = d._pending_messages.get(KEY)
    chk(slot_d is not None, "silent decline: burst not dropped")
    chk(slot_d is not None and "\n" in (slot_d.text or ""),
        "silent decline: fell back to merge rather than losing it")

    print("=== media occupant keeps the historical caption merge ===")
    # _queue_or_replace_pending_event merges text into a photo caption and
    # does NOT grow the queue. Delegating would look like a decline and merge
    # a second time, so a media occupant must take the historical path.
    e, e_runner = make_adapter()
    photo = Ev("caption-1")
    photo.message_type = MessageType.PHOTO
    photo.media_urls = ["a.jpg"]
    e._pending_messages[KEY] = photo
    await flush(e, KEY, "what is the capital of Mongolia")
    chk(not e_runner._queued_events.get(KEY),
        "media occupant: nothing sent to the FIFO")
    chk(e._pending_messages[KEY] is photo,
        "media occupant: slot still holds the photo event")
    chk((e._pending_messages[KEY].text or "").count("what is the capital") == 1,
        "media occupant: caption merged exactly once, not duplicated")

    print("=== fallback when no runner attached ===")
    b = object.__new__(_Adapter)
    b._text_debounce = {}; b._pending_messages = {}
    b._busy_session_handler = None
    await flush(b, KEY, "what is the capital of Mongolia")
    await flush(b, KEY, "what is the capital of Peru")
    txt = b._pending_messages[KEY].text or ""
    chk("\n" in txt, "no-runner path still merges (historical fallback intact)")


asyncio.run(main())
print("\n=== DEBOUNCE FIFO: %d failures ===" % len(FAILS))
for f in FAILS:
    print("   !", f)
sys.exit(1 if FAILS else 0)
