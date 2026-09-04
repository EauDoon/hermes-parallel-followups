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
import sys, py_compile, os, stat, tempfile

PATH = sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes/gateway/platforms/base.py"


def write_backup_exclusive(path, contents, mode):
    """Create a recovery copy without following or replacing an existing path."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    descriptor = os.open(path, flags, mode & 0o777)
    try:
        with os.fdopen(descriptor, "wb") as backup:
            descriptor = -1
            backup.write(contents)
            backup.flush()
            if hasattr(os, "fchmod"):
                os.fchmod(backup.fileno(), mode & 0o777)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        raise

try:
    st = os.lstat(PATH)
except OSError as e:
    print("ABORT: target cannot be inspected:\n", e); sys.exit(2)
if not stat.S_ISREG(st.st_mode):
    print("ABORT: target must be a regular file (symlinks are not patched)"); sys.exit(2)

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
        # Any message type that is treated as a media occupant. The previous
        # code only checked MessageType.PHOTO, which left VIDEO / VOICE /
        # AUDIO / DOCUMENT / STICKER messages with empty media_urls on the
        # historical merge path and double-merged their text bursts. getattr
        # with a default keeps the patch forward-compatible with Hermeses
        # that do not yet define the newer members.
        _slot_is_media = _slot is not None and (getattr(_slot, "message_type", None) in (getattr(MessageType, "PHOTO", None), getattr(MessageType, "VIDEO", None), getattr(MessageType, "AUDIO", None), getattr(MessageType, "DOCUMENT", None), getattr(MessageType, "VOICE", None), getattr(MessageType, "STICKER", None), getattr(MessageType, "ANIMATION", None), getattr(MessageType, "VIDEO_NOTE", None)) or bool(getattr(_slot, "media_urls", None)))
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

try:
    with open(PATH, encoding="utf-8", newline="") as target:
        src = target.read()
except (OSError, UnicodeError):
    print("ABORT: target must be readable UTF-8"); sys.exit(2)
if "\r" in src.replace("\r\n", ""):
    print("ABORT: unsupported carriage-return line endings"); sys.exit(2)
if "\r\n" in src and "\n" in src.replace("\r\n", ""):
    print("ABORT: mixed line endings are not supported"); sys.exit(2)
line_ending = "\r\n" if "\r\n" in src else "\n"
old = OLD.replace("\n", line_ending)
new = NEW.replace("\n", line_ending)
old_count = src.count(old)
new_count = src.count(new)
if new_count:
    if (new_count, old_count) != (1, 0):
        print(
            "ABORT: malformed current install (patched=%d, unpatched=%d)"
            % (new_count, old_count)
        ); sys.exit(2)
    print("ALREADY_PATCHED"); sys.exit(0)
if old_count != 1:
    print("ABORT: expected exactly 1 flush site, found %d" % old_count); sys.exit(2)

candidate = bytecode = None
try:
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="", delete=False,
        dir=os.path.dirname(os.path.abspath(PATH)), prefix="." + os.path.basename(PATH) + ".", suffix=".tmp",
    ) as staged:
        candidate = staged.name
        staged.write(src.replace(old, new, 1))
    os.chmod(candidate, st.st_mode & 0o777)
    if hasattr(os, "chown"):
        os.chown(candidate, st.st_uid, st.st_gid)
    bytecode = candidate + ".pyc"
    py_compile.compile(candidate, cfile=bytecode, doraise=True)
    write_backup_exclusive(
        PATH + ".bak-pre-debouncefifo",
        src.encode("utf-8"),
        st.st_mode,
    )
    os.replace(candidate, PATH)
except (py_compile.PyCompileError, OSError) as e:
    print("ABORT: staged write or compile check failed; target unchanged:\n", e); sys.exit(3)
finally:
    for temporary in (candidate, bytecode):
        if temporary:
            try: os.unlink(temporary)
            except FileNotFoundError: pass
print("PATCHED_OK")
