#!/usr/bin/env python3
"""Route flushed busy-text bursts through the FIFO instead of merging them
.

Problem: with display.busy_input_mode=queue, _flush_text_debounce_now pushed
each debounced burst into the SINGLE pending slot via
merge_pending_message_event(merge_text=True), which newline-joins onto whatever
is already there. The merge has no time bound, so every follow-up sent during a
long turn collapsed into ONE turn and question<->answer pairing was destroyed.
This is the #43066 sub-bug; the FIFO fix landed for interrupt mode, steer
fallback and /queue, but never for the queue-mode text path.

Fix: hand the flushed burst to the runner's _queue_or_replace_pending_event,
which is the FIFO entry point those other paths already use, so each follow-up
gets its own turn in arrival order. Sub-second bursts still merge INSIDE the
debounce window (0.35s rolling / 1.0s hard cap) - that part is correct, a
single thought split across two taps should stay one turn.

The runner is reached via the bound _busy_session_handler it already installs
on this adapter, so no wiring changes are needed in run.py - this is a
single-file patch. Falls back to the historical merge when no runner is
attached (standalone adapter use, tests).

Idempotent, backed up, syntax-checked.
Usage: apply_debounce_fifo_patch.py [/opt/hermes/gateway/platforms/base.py]
"""
import sys, py_compile, shutil, os

PATH = sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes/gateway/platforms/base.py"
MARK = "_queue_or_replace_pending_event"

OLD = """        state = store.pop(session_key, None)
        if state is None:
            return False
        merge_pending_message_event(
            self._pending_messages,
            session_key,
            state.event,
            merge_text=True,
        )
        return True
"""

NEW = '''        state = store.pop(session_key, None)
        if state is None:
            return False
        # Hand the flushed burst to the runner's FIFO so each follow-up gets
        # its OWN turn in arrival order. The historical
        # call below newline-merged it into the single pending slot with no
        # time bound, so everything sent during a long turn arrived as one
        # mashed-together turn -- the #43066 sub-bug, fixed for interrupt /
        # steer-fallback / /queue but never for this path.
        #
        # The runner is reachable through the bound busy-session handler it
        # already installed on this adapter, so no extra wiring is required.
        # Photo/album merge semantics are preserved inside
        # _queue_or_replace_pending_event itself.
        _busy_handler = getattr(self, "_busy_session_handler", None)
        _runner = getattr(_busy_handler, "__self__", None)
        _enqueue = getattr(_runner, "_queue_or_replace_pending_event", None)
        if callable(_enqueue):
            try:
                _enqueue(session_key, state.event)
                return True
            except Exception:
                logger.warning(
                    "[%s] FIFO enqueue of debounced burst failed for %s; "
                    "falling back to pending-slot merge",
                    self.name, session_key, exc_info=True,
                )
        merge_pending_message_event(
            self._pending_messages,
            session_key,
            state.event,
            merge_text=True,
        )
        return True
'''

src = open(PATH, encoding="utf-8", newline="").read()
if MARK in src:
    print("ALREADY_PATCHED"); sys.exit(0)
if src.count(OLD) != 1:
    print("ABORT: expected exactly 1 flush site, found %d" % src.count(OLD)); sys.exit(2)

st = os.stat(PATH)
shutil.copy2(PATH, PATH + ".bak-pre-debouncefifo")
open(PATH, "w", encoding="utf-8", newline="").write(src.replace(OLD, NEW, 1))
try:
    py_compile.compile(PATH, doraise=True)
except py_compile.PyCompileError as e:
    shutil.copy2(PATH + ".bak-pre-debouncefifo", PATH)
    print("ABORT: syntax error, restored backup:\n", e); sys.exit(3)
os.chmod(PATH, st.st_mode & 0o777)
print("PATCHED_OK")
