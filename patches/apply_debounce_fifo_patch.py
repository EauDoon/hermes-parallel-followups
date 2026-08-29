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
        #
        # ``_queue_or_replace_pending_event`` can DECLINE silently: it returns
        # without queueing and without raising when the source resolves to no
        # adapter, or when the per-session pending cap is reached. Treating the
        # call as success there would DROP the burst, where the historical
        # merge would still have delivered it (mashed, but delivered) -- and
        # the cap was effectively unreachable before, since the old merge
        # collapsed every follow-up into one slot instead of one entry each.
        # So confirm the queue actually grew, and fall back to the merge when
        # it did not. Merging is lossy; dropping is worse.
        _busy_handler = getattr(self, "_busy_session_handler", None)
        _runner = getattr(_busy_handler, "__self__", None)
        _enqueue = getattr(_runner, "_queue_or_replace_pending_event", None)
        _resolve = getattr(_runner, "_adapter_for_source", None)
        _depth = getattr(_runner, "_queue_depth", None)
        # A media occupant needs the caption-merge semantics that
        # ``_queue_or_replace_pending_event`` applies internally, and that
        # merge succeeds WITHOUT growing the queue -- which the depth check
        # below would misread as a decline and merge a second time. Keep the
        # historical path for that case; it is what the FIFO would do anyway.
        _slot = self._pending_messages.get(session_key)
        _slot_is_media = _slot is not None and (
            getattr(_slot, "message_type", None) == MessageType.PHOTO
            or bool(getattr(_slot, "media_urls", None))
        )
        _target = None
        if callable(_resolve) and not _slot_is_media:
            try:
                _target = _resolve(getattr(state.event, "source", None))
            except Exception:
                _target = None
        # Delegate only when the runner routes this source back to THIS
        # adapter: another adapter owns a different pending slot, and the
        # drain that delivers this burst runs on ours. Declining to delegate
        # costs the fix on exotic topologies; delegating blindly would risk
        # the burst landing where nothing drains it.
        if callable(_enqueue) and callable(_depth) and _target is self:
            try:
                _before = _depth(session_key, adapter=_target)
                _enqueue(session_key, state.event)
                if _depth(session_key, adapter=_target) > _before:
                    return True
                logger.warning(
                    "[%s] FIFO declined the debounced burst for %s "
                    "(pending cap reached?); falling back to pending-slot merge",
                    self.name, session_key,
                )
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
if NEW in src:
    print("ALREADY_PATCHED"); sys.exit(0)
if src.count(OLD) != 1:
    print("ABORT: expected exactly 1 flush site, found %d" % src.count(OLD)); sys.exit(2)

st = os.stat(PATH)
shutil.copy2(PATH, PATH + ".bak-pre-debouncefifo")
open(PATH, "w", encoding="utf-8", newline="").write(src.replace(OLD, NEW, 1))
try:
    py_compile.compile(PATH, doraise=True)
except (py_compile.PyCompileError, OSError) as e:
    shutil.copy2(PATH + ".bak-pre-debouncefifo", PATH)
    print("ABORT: compile check failed, restored backup:\n", e); sys.exit(3)
os.chmod(PATH, st.st_mode & 0o777)
print("PATCHED_OK")
