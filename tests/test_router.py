#!/usr/bin/env python3
"""Integration test for _maybe_route_overflow_to_background.

Execs the EXACT class block the patch injects, with stubbed collaborators,
and drives every gate: config mode, overflow depth, command/media bypass,
classifier verdict, dispatch + ack.
"""
import ast, re, os, time, asyncio, logging, sys

import os
HERE = os.path.dirname(os.path.abspath(__file__))
PATCH = os.environ.get("OVR_PATCH") or os.path.join(
    HERE, os.pardir, "patches", "apply_busy_overflow_router_patch.py")

tree = ast.parse(open(PATCH, encoding="utf-8").read())
block = None
for node in tree.body:
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id == "BLOCK":
                block = ast.literal_eval(node.value)
assert block, "could not extract BLOCK"

STATE = {"cfg_mode": "off"}


def _load_gateway_runtime_config():
    return {"display": {"busy_overflow_background": STATE["cfg_mode"]}}


def cfg_get(cfg, *keys, default=None):
    cur = cfg
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


ns = {
    "re": re, "os": os, "time": time, "asyncio": asyncio,
    "logger": logging.getLogger("t"),
    "_load_gateway_runtime_config": _load_gateway_runtime_config,
    "cfg_get": cfg_get,
}
exec("class _R:\n" + block, ns)          # noqa: S102 - testing shipped source
R = ns["_R"]


class Src:
    platform = "telegram"; chat_id = "1"; chat_type = "dm"
    thread_id = None; user_id = "u"; user_id_alt = None
    user_name = "d"; chat_name = "c"


class Event:
    def __init__(self, text, cmd=False, media=None, internal=False):
        self.text = text; self._cmd = cmd
        self.media_urls = media or []; self.internal = internal
        self.source = Src(); self.message_id = "9"
    def is_command(self):
        return self._cmd


class Adapter:
    def __init__(self, debounced=False):
        self.sent = []
        # Mirrors BasePlatformAdapter._text_debounce: a busy follow-up sits
        # here for 0.35-1.0s before being flushed into _pending_messages.
        self._text_debounce = {"sess": object()} if debounced else {}
    async def _send_with_retry(self, **kw):
        self.sent.append(kw.get("content", "")); return None


class Runner(R):
    def __init__(self, depth, debounced=False):
        self._depth = depth
        self._background_tasks = set()
        self.adapter = Adapter(debounced)
        self.dispatched = []
    def _adapter_for_source(self, src): return self.adapter
    def _queue_depth(self, key, adapter=None): return self._depth
    def _reply_anchor_for_event(self, e): return "9"
    def _thread_metadata_for_source(self, src, anchor): return {}
    async def _run_background_task(self, prompt, source, task_id, **kw):
        self.dispatched.append(prompt)


DEP = "do it"
IND = "what is the capital of Mongolia and why"

CASES = [
    # (name, cfg_mode, depth, event, expect_routed, expect_dispatch, debounced)
    ("mode=off blocks everything",        "off",         2, Event(IND), False, 0, False),
    ("no overflow (depth 0) stays queued","independent", 0, Event(IND), False, 0, False),
    ("dependent text stays queued",       "independent", 2, Event(DEP), False, 0, False),
    ("independent text is routed",        "independent", 2, Event(IND), True,  1, False),
    ("option A routes dependent too",     "all",         2, Event(DEP), True,  1, False),
    ("slash command never routed",        "independent", 2, Event(IND, cmd=True), False, 0, False),
    ("media never routed",                "independent", 2, Event(IND, media=["x.jpg"]), False, 0, False),
    ("internal event never routed",       "independent", 2, Event(IND, internal=True), False, 0, False),
    ("empty text never routed",           "independent", 2, Event("   "), False, 0, False),
    ("bare 'true' means option B",        "true",        2, Event(IND), True,  1, False),
    # --- 2026-08-02 live-test regression: a follow-up sitting in the debounce
    # buffer is waiting work, but _queue_depth cannot see it. Before the fix
    # this returned False and the message merged.
    ("debounce buffer counts as overflow","independent", 0, Event(IND), True,  1, True),
    ("debounced + dependent still queues","independent", 0, Event(DEP), False, 0, True),
    ("no debounce, no slot -> not routed","independent", 0, Event(IND), False, 0, False),
]

fails = []
for name, mode, depth, ev, exp_routed, exp_disp, debounced in CASES:
    STATE["cfg_mode"] = mode
    r = Runner(depth, debounced)
    routed = asyncio.get_event_loop().run_until_complete(
        r._maybe_route_overflow_to_background(ev, "sess")
    )
    # let the spawned task run
    if r._background_tasks:
        asyncio.get_event_loop().run_until_complete(
            asyncio.gather(*list(r._background_tasks), return_exceptions=True)
        )
    ok = (bool(routed) == exp_routed) and (len(r.dispatched) == exp_disp)
    acked = len(r.adapter.sent)
    if exp_routed and acked != 1:
        ok = False
    if not ok:
        fails.append("%s -> routed=%s dispatched=%d acked=%d (expected routed=%s dispatched=%d)"
                     % (name, routed, len(r.dispatched), acked, exp_routed, exp_disp))
    print("  %s %s" % ("PASS" if ok else "FAIL", name))

print("\n=== ROUTER INTEGRATION: %d cases, %d failures ===" % (len(CASES), len(fails)))
for f in fails:
    print("   !", f)
sys.exit(1 if fails else 0)
