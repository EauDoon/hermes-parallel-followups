#!/usr/bin/env python3
"""Fire 10 back-to-back messages at a BUSY session through the REAL
handle_message entry point and report how each was routed.

Real objects: MessageEvent, SessionSource, build_session_key,
BasePlatformAdapter.handle_message, GatewayRunner._handle_active_session_busy_message,
the overflow router, the debounce, and the FIFO. Stubs only at the I/O edge
(send, background execution, auth).
"""
import sys, asyncio, types
from types import SimpleNamespace
sys.path.insert(0, "/opt/hermes")

from gateway.platforms.base import (
    BasePlatformAdapter, MessageEvent, MessageType, build_session_key,
)
from gateway.session import SessionSource
from gateway.config import Platform
from gateway.run import GatewayRunner

SENT = []          # acks delivered to the user
DISPATCHED = []    # prompts sent to background agents


class _Adapter(BasePlatformAdapter):
    name = "telegram"
    async def connect(self): return True
    async def disconnect(self): return None
    async def get_chat_info(self, chat_id): return {}
    async def send(self, *a, **kw): return None
    async def _send_with_retry(self, **kw):
        SENT.append(kw.get("content", "")); return None


def build():
    a = object.__new__(_Adapter)
    a._active_sessions = {}
    a._pending_messages = {}
    a._session_tasks = {}
    a._text_debounce = {}
    a._message_handler = lambda *x, **k: None
    a.config = SimpleNamespace(extra={})
    a._busy_text_mode = "queue"
    a._busy_text_debounce_seconds = 0.35
    a._busy_text_hard_cap_seconds = 1.0
    a._apply_topic_recovery = lambda e: None

    r = object.__new__(GatewayRunner)
    r._queued_events = {}
    r._background_tasks = set()
    r._running_agents = {}
    r._draining = False
    r._busy_input_mode = "queue"
    r._busy_text_mode = "queue"
    r._adapter_for_source = lambda s: a
    r._is_user_authorized = lambda s: True
    r._reply_anchor_for_event = lambda e: e.message_id
    r._thread_metadata_for_source = lambda s, anchor: {}

    async def _bg(prompt, source, task_id, **kw):
        DISPATCHED.append(prompt)
    r._run_background_task = _bg

    a._busy_session_handler = r._handle_active_session_busy_message
    return a, r


SRC = SessionSource(
    platform=Platform.TELEGRAM, chat_id="1000001", chat_type="dm",
    user_id="1000001", user_name="Test User", chat_name="dm",
)

# 10 questions: I = self-contained (should parallelise), D = depends on the
# turn in flight (must stay queued, in order, unmerged).
BURST = [
    ("D", "also can you amend that list"),
    ("I", "what are the health benefits of cold plunges"),
    ("D", "do it"),
    ("I", "how does the BOJ set interest rates these days"),
    ("D", "which ones are the best"),
    ("I", "who founded the company Anthropic and when"),
    ("D", "no use the other one instead"),
    ("I", "why did the Ming dynasty ban maritime trade"),
    ("D", "tell me more about (2) and (3)"),
    ("I", "what is the capital of Mongolia and why is it there"),
]


async def main():
    a, r = build()
    key = build_session_key(SRC, group_sessions_per_user=True,
                            thread_sessions_per_user=False)
    # Simulate an agent mid-turn: live guard + a live owner task.
    a._active_sessions[key] = asyncio.Event()
    a._session_tasks[key] = asyncio.create_task(asyncio.sleep(60))

    print("session_key = %s\n" % key)
    for i, (label, text) in enumerate(BURST, 1):
        ev = MessageEvent(text=text, message_type=MessageType.TEXT,
                          source=SRC, message_id=str(1000 + i))
        before = len(DISPATCHED)
        await a.handle_message(ev)
        await asyncio.sleep(1.2)          # past the 1.0s debounce hard cap
        routed = len(DISPATCHED) > before
        print("  %2d [%s] %-52s -> %s" % (
            i, label, text[:52], "BACKGROUND" if routed else "queued"))

    await asyncio.sleep(1.5)
    a._session_tasks[key].cancel()

    slot = a._pending_messages.get(key)
    overflow = r._queued_events.get(key, [])
    queued_texts = ([slot.text] if slot is not None else []) + [e.text for e in overflow]

    print("\n--- queued turns (each must be ONE message) ---")
    for i, t in enumerate(queued_texts, 1):
        merged = "\n" in (t or "")
        print("  %2d %-9s %s" % (i, "MERGED!" if merged else "clean",
                                 (t or "").replace("\n", " || ")[:70]))

    print("\n--- background dispatches ---")
    for p in DISPATCHED:
        print("   +", p[:70])

    dep_texts = {t for l, t in BURST if l == "D"}
    merged_any = any("\n" in (t or "") for t in queued_texts)

    fails = []
    # THE point of the whole change: no queued turn may fuse two messages.
    if merged_any:
        fails.append("a queued turn contains a newline merge")
    # Nothing may be lost: every message ends up queued or backgrounded, once.
    if len(queued_texts) + len(DISPATCHED) != len(BURST):
        fails.append("message lost/duplicated: %d queued + %d bg != %d sent"
                     % (len(queued_texts), len(DISPATCHED), len(BURST)))
    # Safety: a context-dependent message must never be answered cold.
    leaked = dep_texts & set(DISPATCHED)
    if leaked:
        fails.append("dependent message(s) backgrounded: %s" % sorted(leaked))
    # Order of the queued lane must match arrival order.
    arrival = [t for _, t in BURST if t in set(queued_texts)]
    if arrival != queued_texts:
        fails.append("queued turns out of arrival order")
    # Parallelism is actually happening (misroutes cost speed, never safety).
    if len(DISPATCHED) < 3:
        fails.append("too little parallelism: only %d backgrounded" % len(DISPATCHED))

    print("\n=== BURST: %d queued, %d backgrounded, %d failures ===" % (
        len(queued_texts), len(DISPATCHED), len(fails)))
    for f in fails:
        print("   !", f)
    return 1 if fails else 0


sys.exit(asyncio.run(main()))
