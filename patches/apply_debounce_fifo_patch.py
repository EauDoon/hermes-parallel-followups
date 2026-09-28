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
import ast, sys, py_compile, os, stat, tempfile, argparse, re

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("path", nargs="?", default="/opt/hermes/gateway/platforms/base.py")
parser.add_argument("--check", action="store_true", help="validate applicability without writing files")
parser.add_argument("--reverse", action="store_true", help="remove the exact current patch while preserving unrelated edits")
args = parser.parse_args()
PATH = args.path


def checked_read(path, expected=None):
    """Read a regular file and verify that its directory entry still owns it."""
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode):
        raise OSError("target or backup is no longer a regular file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        contents = stream.read()
        after = os.fstat(stream.fileno())
    fingerprint = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode)
    if (fingerprint(before) != fingerprint(opened)
            or fingerprint(opened) != fingerprint(after)
            or fingerprint(after) != fingerprint(os.lstat(path))
            or (expected is not None and fingerprint(after) != fingerprint(expected))):
        raise OSError("target changed during patch preparation")
    return contents


def guard_target():
    if checked_read(PATH, st) != src.encode("utf-8"):
        raise OSError("target changed during patch preparation")


def parses(text):
    """Compile the bytes an apply would write, not the decoded str.

    py_compile reads the staged file back as bytes, so it applies Python's own
    BOM and encoding-cookie detection. Compiling the str instead makes a byte
    order mark a SyntaxError, and --check would then refuse a file that the
    very next apply writes successfully.
    """
    return compile(text.encode("utf-8"), PATH, "exec")


def recovery_copy_conflict(path, contents):
    """Why the recovery copy at ``path`` makes the write abort, or None.

    Mirrors the O_EXCL branch of write_backup_exclusive so --check predicts
    the apply instead of approving a write that then fails, and names the same
    reason. Deliberately not shared with that branch: a path that disappears
    between the O_EXCL failure and the lstat there is a hard write abort, while
    here it simply means the copy does not exist yet and the write will make it.
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return "existing recovery backup is not a regular file"
    if checked_read(path) != contents:
        return "existing recovery backup differs; preserve or relocate it before retrying"
    return None


def write_backup_exclusive(path, contents, mode):
    """Create a recovery copy without following or replacing an existing path."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    try:
        descriptor = os.open(path, flags, mode & 0o777)
    except FileExistsError:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise OSError("existing recovery backup is not a regular file")
        if checked_read(path) != contents:
            raise OSError("existing recovery backup differs; preserve or relocate it before retrying")
        return  # An exact recovery copy already exists after reverse/reapply.
    try:
        with os.fdopen(descriptor, "wb") as backup:
            descriptor = -1
            backup.write(contents)
            backup.flush()
            os.fsync(backup.fileno())
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

OLD = """        existing_pending = self._pending_messages.get(session_key)
        if (
            existing_pending is not None
            and not self._can_merge_text_debounce_events(existing_pending, state.event)
        ):
            return False

        state = store.pop(session_key, None)
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

PREVIOUS_NEW = '''        state = store.pop(session_key, None)
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

NEW = '''        existing_pending = self._pending_messages.get(session_key)
        # Different senders must not share a turn. Returning before the FIFO
        # left this burst in the debounce store after its timer was cancelled,
        # so a second person was never given a turn of their own. The FIFO
        # keeps the two senders apart. The historical merge below would not,
        # and neither would an in-place media merge, so those two paths put
        # the burst back instead of mixing it into the other sender's slot.
        _senders_differ = (
            existing_pending is not None
            and not self._can_merge_text_debounce_events(existing_pending, state.event)
        )
        state = store.pop(session_key, None)
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
        # The FIFO merges in place, and the queue does not grow, only when
        # the occupant or this burst is a photo or already carries media
        # URLs. The depth check below would read that as a decline and merge
        # a second time, so those events stay on the historical path, which
        # does the same caption merge. Every other occupant has to take the
        # FIFO. A video, voice, or document with empty media_urls is not one
        # of those in-place merges: the historical path replaces the slot and
        # drops it, while reporting success. A missing PHOTO member must not
        # be None either, or a slot whose message_type is None compares equal
        # and takes that same drop.
        _slot = self._pending_messages.get(session_key)
        _photo = getattr(MessageType, "PHOTO", False)
        _slot_is_media = _slot is not None and (
            (_photo is not False and (
                getattr(_slot, "message_type", None) == _photo
                or getattr(state.event, "message_type", None) == _photo
            ))
            or bool(getattr(_slot, "media_urls", None))
            or bool(getattr(state.event, "media_urls", None))
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
        if _senders_differ:
            store[session_key] = state
            return False
        merge_pending_message_event(
            self._pending_messages,
            session_key,
            state.event,
            merge_text=True,
        )
        return True
'''

MEDIA_FIXED_NEW = '''        state = store.pop(session_key, None)
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
        # The FIFO merges in place, and the queue does not grow, only when
        # the occupant or this burst is a photo or already carries media
        # URLs. The depth check below would read that as a decline and merge
        # a second time, so those events stay on the historical path, which
        # does the same caption merge. Every other occupant has to take the
        # FIFO. A video, voice, or document with empty media_urls is not one
        # of those in-place merges: the historical path replaces the slot and
        # drops it, while reporting success. A missing PHOTO member must not
        # be None either, or a slot whose message_type is None compares equal
        # and takes that same drop.
        _slot = self._pending_messages.get(session_key)
        _photo = getattr(MessageType, "PHOTO", False)
        _slot_is_media = _slot is not None and (
            (_photo is not False and (
                getattr(_slot, "message_type", None) == _photo
                or getattr(state.event, "message_type", None) == _photo
            ))
            or bool(getattr(_slot, "media_urls", None))
            or bool(getattr(state.event, "media_urls", None))
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

def flush_site(source):
    """Span of the first ``_flush_text_debounce_now`` body, or None.

    The OLD block is plain lines with no signature of their own, so a count
    of one is not proof that it is still the flush site. The body ends at the
    next line that is not blank, not a comment, and not indented strictly
    deeper than the def. A following class, the next method, or a
    module-level function all close it. Stopping only at the next ``def`` of
    the same indent left a nested class, and anything after the last method,
    inside the span, so a copy of the anchor there was rewritten.
    """
    site = re.search(
        r"(?m)^([ \t]*)(?:async[ \t]+)?def[ \t]+_flush_text_debounce_now[ \t]*\(",
        source,
    )
    if site is None:
        return None
    indent = site.group(1)
    line_start = source.find("\n", site.end())
    if line_start < 0:
        return site.end(), len(source)
    end = len(source)
    i = line_start + 1
    while i < len(source):
        nxt = source.find("\n", i)
        if nxt < 0:
            nxt = len(source)
        line = source[i:nxt]
        if line.strip() and not line.lstrip().startswith("#"):
            deeper = line.startswith(indent + " ") or line.startswith(indent + "\t")
            if not deeper:
                end = i
                break
        if nxt == len(source):
            break
        i = nxt + 1
    return site.end(), end


try:
    src = checked_read(PATH, st).decode("utf-8")
except (OSError, UnicodeError):
    print("ABORT: target must be readable UTF-8"); sys.exit(2)
if "\r" in src.replace("\r\n", ""):
    print("ABORT: unsupported carriage-return line endings"); sys.exit(2)
if "\r\n" in src and "\n" in src.replace("\r\n", ""):
    print("ABORT: mixed line endings are not supported"); sys.exit(2)
line_ending = "\r\n" if "\r\n" in src else "\n"

# Preconditions: the injected flush body calls these names at runtime. A missing
# binding is a NameError raised INSIDE the flush, after the burst has already
# left the debounce store, so the follow-up is dropped instead of falling back
# to the merge. The router installer refuses the same class of install for the
# same reason; refuse here too rather than at the first busy follow-up.
def _target_binds(target, name):
    if isinstance(target, ast.Name):
        return target.id == name
    if isinstance(target, (ast.Tuple, ast.List)):
        return any(_target_binds(element, name) for element in target.elts)
    return False


def _binds(body, name):
    """True when ``name`` is bound by these module-level statements.

    Function and class bodies are not module scope. An annotation with no
    value, an import alias, and a comment do not bind the name either; a
    parenthesized import does, because it is still an import.
    """
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == name:
                return True
            continue
        if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith)):
            if _binds(node.body, name) or _binds(node.orelse, name):
                return True
            continue
        if isinstance(node, ast.Try):
            parts = [node.body, node.orelse, node.finalbody]
            parts.extend(handler.body for handler in node.handlers)
            if any(_binds(part, name) for part in parts):
                return True
            continue
        if isinstance(node, ast.Assign) and any(_target_binds(target, name) for target in node.targets):
            return True
        if isinstance(node, ast.AnnAssign) and node.value is not None and _target_binds(node.target, name):
            return True
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound_name = alias.asname or alias.name.split(".")[0]
                if bound_name == name:
                    return True
    return False


def bound(source, name):
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # The later syntax check reports this. Calling it a missing name
        # hides a file that does bind the name and simply does not parse.
        return True
    return _binds(tree.body, name)


for required in ("logger", "MessageType"):
    if not bound(src, required):
        print("ABORT: base-platform symbol %r is not defined or imported" % required); sys.exit(2)

def recovery_path(upgrading):
    """Backup path the write uses.

    A fresh install and a reverse keep the names they already used. Upgrading
    a previously injected flush body has to leave the original recovery copy
    alone, or the never-overwrite rule aborts the only write that installs
    the corrected body.
    """
    path = PATH + ".bak-pre-debouncefifo" + (".reverse" if args.reverse else "")
    if not upgrading:
        return path
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return path
    if not stat.S_ISREG(info.st_mode):
        raise OSError("existing recovery backup is not a regular file")
    for ordinal in range(1, 9):
        slot = path + ".upgrade" if ordinal == 1 else "%s.upgrade.%d" % (path, ordinal)
        if not os.path.lexists(slot):
            return slot
    raise OSError(
        "no free upgrade recovery slot beside %s; preserve or relocate "
        "them before retrying" % path
    )


old = OLD.replace("\n", line_ending)
new = NEW.replace("\n", line_ending)
previous = PREVIOUS_NEW.replace("\n", line_ending)
media_fixed = MEDIA_FIXED_NEW.replace("\n", line_ending)
# The sender guard is the part of the unpatched tail that older injected
# bodies left in place. An upgrade has to replace the guard and that body
# together, or the guard still returns before the FIFO sees the burst.
guard = old[:old.index("        state = store.pop(session_key, None)" + line_ending)]
legacy_region = guard + previous
media_region = guard + media_fixed
old_count = src.count(old)
new_count = src.count(new)
legacy_count = src.count(legacy_region)
media_count = src.count(media_region)
upgrading = False
if new_count:
    if (new_count, old_count, legacy_count, media_count) != (1, 0, 0, 0):
        print(
            "ABORT: malformed current install (patched=%d, unpatched=%d, legacy=%d, media=%d)"
            % (new_count, old_count, legacy_count, media_count)
        ); sys.exit(2)
    try:
        parses(src)
    except (SyntaxError, ValueError) as error:
        print("ABORT: target syntax is invalid; target unchanged:\n", error); sys.exit(3)
    if not args.reverse:
        print("ALREADY_PATCHED"); sys.exit(0)
    out = src.replace(new, old, 1)
elif legacy_count or media_count:
    if (legacy_count, media_count, old_count) not in ((1, 0, 0), (0, 1, 0)):
        print(
            "ABORT: malformed previous install (legacy=%d, media=%d, unpatched=%d)"
            % (legacy_count, media_count, old_count)
        ); sys.exit(2)
    region = legacy_region if legacy_count else media_region
    try:
        parses(src)
    except (SyntaxError, ValueError) as error:
        print("ABORT: target syntax is invalid; target unchanged:\n", error); sys.exit(3)
    if args.reverse:
        out = src.replace(region, old, 1)
    else:
        upgrading = True
        out = src.replace(region, new, 1)
else:
    if old_count != 1:
        print("ABORT: expected exactly 1 flush site, found %d" % old_count); sys.exit(2)
    site = flush_site(src)
    if site is None or not site[0] <= src.index(old) < site[1]:
        print("ABORT: the flush site is not inside _flush_text_debounce_now"); sys.exit(2)
    if args.reverse:
        print("ALREADY_UNPATCHED"); sys.exit(0)
    out = src.replace(old, new, 1)

if args.check:
    try:
        parses(out)
    except (SyntaxError, ValueError) as error:
        print("ABORT: candidate syntax is invalid:\n", error); sys.exit(3)
    # The recovery copy is the one precondition an apply can reach after this
    # point, so a verdict that ignored it would approve a write that aborts.
    try:
        conflict = recovery_copy_conflict(recovery_path(upgrading), src.encode("utf-8"))
    except OSError as error:
        print("ABORT: the recovery copy blocks this write; target unchanged:\n", error); sys.exit(3)
    if conflict:
        print("ABORT: the recovery copy blocks this write; target unchanged:\n", conflict); sys.exit(3)
    print("REVERSIBLE" if args.reverse else "UPGRADE_APPLICABLE" if upgrading else "APPLICABLE"); sys.exit(0)

candidate = bytecode = None
try:
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="", delete=False,
        dir=os.path.dirname(os.path.abspath(PATH)), prefix="." + os.path.basename(PATH) + ".", suffix=".tmp",
    ) as staged:
        candidate = staged.name
        staged.write(out)
        staged.flush()
        os.fsync(staged.fileno())
    os.chmod(candidate, st.st_mode & 0o777)
    if hasattr(os, "chown"):
        os.chown(candidate, st.st_uid, st.st_gid)
    bytecode = candidate + ".pyc"
    py_compile.compile(candidate, cfile=bytecode, doraise=True)
    guard_target()
    write_backup_exclusive(
        recovery_path(upgrading),
        src.encode("utf-8"),
        st.st_mode,
    )
    guard_target()
    os.replace(candidate, PATH)
except (py_compile.PyCompileError, OSError) as e:
    print("ABORT: staged write or compile check failed; target unchanged:\n", e); sys.exit(3)
finally:
    for temporary in (candidate, bytecode):
        if temporary:
            try: os.unlink(temporary)
            except FileNotFoundError: pass
print("REVERSED_OK" if args.reverse else "PATCHED_OK")
