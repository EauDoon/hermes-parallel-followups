#!/usr/bin/env python3
"""Busy-queue overflow router.

Problem: with display.busy_input_mode=queue, every TEXT message that arrives
while the agent is busy is newline-merged into ONE pending event and answered
as a single turn, destroying message boundaries (the #43066 sub-bug, fixed for
interrupt/steer-fallback via the FIFO but never for the queue-mode text path,
which returns False before reaching it).

Fix: when the queue ALREADY holds a follow-up, route the *next* self-contained
message to a background task instead of letting it merge. Background results
arrive labelled with their own prompt, so question<->answer pairing survives.

Gated by display.busy_overflow_background:
    off          - default, no behavior change
    independent  - Option B: only self-contained messages are backgrounded
    all          - Option A: every overflow message is backgrounded

Idempotent, backed up, syntax-checked.
Usage: apply_busy_overflow_router_patch.py [/opt/hermes/gateway/run.py]
"""
import ast, sys, py_compile, os, stat, tempfile, argparse

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("path", nargs="?", default="/opt/hermes/gateway/run.py")
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
        return False  # An exact recovery copy already exists after reverse/reapply.
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
    return True

try:
    st = os.lstat(PATH)
except OSError as e:
    print("ABORT: target cannot be inspected:\n", e); sys.exit(2)
if not stat.S_ISREG(st.st_mode):
    print("ABORT: target must be a regular file (symlinks are not patched)"); sys.exit(2)

HOOK_OLD = """        effective_mode = self._busy_input_mode
        busy_text_mode = getattr(self, "_busy_text_mode", "interrupt")
        if (
            event.message_type == MessageType.TEXT
            and busy_text_mode == "queue"
            and effective_mode != "steer"
        ):
            return False
"""

HOOK_NEW = """        effective_mode = self._busy_input_mode
        busy_text_mode = getattr(self, "_busy_text_mode", "interrupt")
        if (
            event.message_type == MessageType.TEXT
            and busy_text_mode == "queue"
            and effective_mode != "steer"
        ):
            # Busy-overflow router. Before falling through
            # to the adapter's debounce merge (which newline-joins follow-ups
            # into one turn), give a self-contained overflow message its own
            # background agent so its answer comes back labelled.
            try:
                if await self._maybe_route_overflow_to_background(event, session_key):
                    return True
            except Exception:
                logger.warning(
                    "Busy-overflow router failed for session %s; "
                    "falling back to queue merge",
                    session_key, exc_info=True,
                )
            return False
"""

ANCHOR = "    async def _handle_active_session_busy_message(self, event: MessageEvent, session_key: str) -> bool:"

BLOCK_MARKER = """    # ------------------------------------------------------------------
    # Busy-queue overflow router
    # ------------------------------------------------------------------
"""

BLOCK = '''    # ------------------------------------------------------------------
    # Busy-queue overflow router
    # ------------------------------------------------------------------
    # Shape-based classification of a follow-up that arrives while the agent
    # is busy AND at least one follow-up is already queued. "Dependent" text
    # (a correction, an acknowledgement, a back-reference, a bare pronoun)
    # must stay in the session queue because a background agent starts with
    # NO conversation history. Only self-contained questions are safe to run
    # in parallel. Ambiguity resolves to dependent: a queued message merely
    # waits, whereas a wrongly-backgrounded one gets answered blind.

    _OVR_MIN_CHARS = 25
    _OVR_ACK_TIMEOUT_SECONDS = 5.0

    # NOTE: "there" is deliberately absent - existential "are there any X"
    # is a very common self-contained question form, and
    # treating it as a back-reference queued them all.
    _OVR_DEICTIC = frozenset({
        "it", "its", "this", "that", "these", "those", "them", "they",
        "one", "ones", "above", "below", "he", "she", "him", "her",
        "his", "hers", "their", "theirs",
    })

    # Openers that signal continuation of the turn already in flight.
    # NOTE: bare imperatives like "do" are deliberately absent - "do it" is
    # caught by the deictic rule, while "does X ..." must stay routable.
    _OVR_OPENERS = frozenset({
        "ok", "okay", "oki", "yes", "yeah", "yep", "yup", "no", "nope",
        "sure", "thanks", "thank", "great", "nice", "wow", "cool", "perfect",
        "hi", "hello", "hey", "redo", "stop", "wait", "also", "and", "but",
        "then", "instead", "actually", "leave", "skip", "continue", "proceed",
        "go", "complete", "implement", "amend", "fix", "use", "try", "help",
        "make", "add", "remove", "change", "update", "run", "send", "show",
        "give", "let", "lets", "please", "pls", "again", "more", "next",
        "same", "correct", "wrong", "nvm", "nevermind", "hold",
    })

    _OVR_INTERROG = frozenset({
        "what", "whats", "why", "how", "who", "whos", "when", "where",
        "which", "whose", "is", "are", "was", "were", "does", "did", "do",
        "can", "could", "should", "will", "would", "has", "have", "had",
        "tell", "explain", "compare", "describe", "define", "any",
    })

    # Verbs that mutate an artifact already under discussion. Their presence
    # anywhere means the request acts on the work in flight, so a background
    # agent (which has no history) cannot serve it. Deliberately excludes
    # broad verbs like "write"/"build" that also occur in genuine questions.
    _OVR_ACTION = frozenset({
        "amend", "adjust", "append", "bold", "bolded", "delete", "edit",
        "format", "insert", "modify", "redo", "refill", "remove", "rename",
        "replace", "rerun", "retry", "revise", "reword", "rewrite", "shorten",
        "simplify", "tweak", "update", "retitle", "unbold", "reformat",
    })

    _OVR_STOP = frozenset({
        "the", "a", "an", "and", "or", "of", "to", "in", "on", "at", "for",
        "is", "are", "was", "were", "be", "been", "do", "does", "did", "so",
        "as", "by", "from", "with", "you", "your", "me", "my", "i", "we",
        "us", "our", "about", "not", "but", "if", "than", "then", "too",
        "can", "will", "would", "should", "could", "what", "why", "how",
        "who", "when", "where", "which", "much", "many", "more", "any",
    })

    @classmethod
    def _ovr_backref_re(cls):
        """Compiled back-reference detector (lazy, cached on the class)."""
        rx = cls.__dict__.get("_OVR_BACKREF_COMPILED")
        if rx is None:
            # "the former"/"the latter" are matched as phrases rather than added
            # to _OVR_DEICTIC: a bare "former" (former champions, former
            # employers) is ordinary vocabulary in a self-contained question.
            # The first-person forms are matched together with their verb for
            # the same reason: a bare "us" collides with the country, and "me"
            # would capture "tell me about X", which is self-contained.
            rx = re.compile(
                r"\\(\\s*\\d+\\s*\\)"
                # "(b)" is the same kind of list reference as "(2)". A capital
                # "option B" is a label; lowercase "option a family" is not,
                # so the letter class is case-sensitive.
                r"|\\(\\s*[a-d]\\s*\\)"
                r"|\\b(?:option|point|part|step)\\s+(?-i:[A-D])\\b"
                r"|\\boption\\s*\\d"
                r"|\\bpoint\\s*\\d"
                r"|\\bpart\\s*\\d"
                r"|\\bstep\\s*\\d"
                r"|\\b(?:option|point|part|step)\\s+(?:one|two|three|four|five|six|seven|eight|nine|ten)\\b"
                r"|\\b(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)\\s+(?:option|point|part|step)\\b"
                r"|\\b(?:the|my|our|your)\\s+(?:code|config|deck|document|draft|file|page|report|sheet|slide)\\b"
                r"|#\\d"
                r"|\\b\\d+\\s*(st|nd|rd|th)\\b"
                r"|\\bas\\s+you\\s+\\w+"
                r"|\\byou\\s+(said|mentioned|recommended|suggested|are|were|just|gave|wrote)"
                r"|\\byou\\s+(mean|meant|meaning)\\b"
                # "you recommended" misses the grammatical "did you recommend",
                # and "did you decide" was not listed at all. Both ask about
                # an action the cold agent cannot see. "can you recommend"
                # does not match, so a new request stays routable.
                r"|\\bdid\\s+you\\s+(?:say|mention|recommend|suggest|give|write|decide|choose|pick|set|ask|put|mean)\\b"
                r"|\\b(?:we|us|our|my)\\s+(?:agreed?|decided?|discussed?|chose|chosen|picked|settled|wanted|want|needed|need|think|thought|believe|assumed?|planned?|proposed|concluded|found|noted|asked?|said|say|meant?|meaning|intended?|should|would|could|must|will|can)\\b"
                r"|\\b(?:what|how|why|which|where|when|who)\\s+(?:did|do|does|are|is|was|were|should|would|could|have|has)\\s+(?:we|us|our|my)\\b"
                # "which model did we pick" puts words between the wh-word and
                # the auxiliary, and "why don't we" contracts it. "should we"
                # inverts them. "us" stays out of these looser forms so the
                # country is not read as the pronoun.
                r"|\\b(?:what|how|why|which|where|when|who)\\b(?:\\s+(?!we\\b|us\\b|our\\b|my\\b)\\w+){0,4}\\s+(?:did|do|does|are|is|was|were|should|would|could|have|has|don't|dont|didn't|didnt|doesn't|doesnt|haven't|havent|hasn't|hasnt|isn't|isnt|aren't|arent|wasn't|wasnt|weren't|werent|shouldn't|shouldnt|wouldn't|wouldnt|couldn't|couldnt|can't|cant|won't|wont)\\s+(?:we|our|my)\\b"
                r"|\\b(?:should|would|could|can|do|does|did|are|is|was|were|have|has|will|don't|dont|didn't|didnt|doesn't|doesnt|haven't|havent|isn't|isnt|aren't|arent|can't|cant|won't|wont)\\s+(?:we|our|my)\\b"
                r"|\\b(above|earlier|previous|previously)\\b"
                r"|\\bthe\\s+(?:former|latter)\\b"
                # "the last answer" was queued. "the first answer" and "the
                # second reply" name the same turn and were not.
                r"|\\b(?:first|second|third|next|last)\\s+(?:one|answer|reply|message|point)\\b"
                r"|\\b(what|how)\\s+about\\b"
                r"|\\balso\\b"
                r"|^\\s*\\[replying\\s+to",
                re.I,
            )
            cls._OVR_BACKREF_COMPILED = rx
        return rx

    @classmethod
    def _classify_busy_followup(cls, text):
        """True when ``text`` is self-contained enough to run in background."""
        import unicodedata
        if not isinstance(text, str):
            return False
        # Invisible format characters can hide a contextual token. Queue
        # ambiguous input instead of stripping away evidence of dependency.
        # Backspace, DEL, and the other controls do the same and are not Cf.
        # Newline, carriage return, and tab are ordinary text.
        if any(unicodedata.category(char) == "Cf" for char in text):
            return False
        if any(unicodedata.category(char) == "Cc" and char not in "\\n\\r\\t" for char in text):
            return False
        t = unicodedata.normalize("NFKC", text).replace("\\u2019", "'").replace("\\u2018", "'").strip()
        # A combining mark that NFKC does not fold into a letter can sit
        # inside "former" or "also". Format characters and controls are
        # already queued above. A mark that recomposes, as in NFD "São",
        # is ordinary text and is not queued for being a mark.
        if any(unicodedata.category(char) in ("Mn", "Me") for char in t):
            return False
        if re.search(r"(?m)^\\s*>", t):
            return False
        # Quote-replies and back-references are contextual by definition.
        # "what's" is already an interrogative after the apostrophe is
        # stripped, but this scan still sees the contraction, so "what's our"
        # misses the same "what is our" pattern and runs cold. Expand only
        # the wh-word form. "what's the difference" stays routable.
        _backref = re.sub(
            r"\\b(what|where|when|who|why|how)'s\\b",
            r"\\1 is",
            t,
            flags=re.I,
        )
        if cls._ovr_backref_re().search(_backref):
            return False
        # Drop a leading gateway timestamp prefix ("[Thu 2026-07-23 16:58 +08]").
        # Matched on a bracketed group containing a 4-digit year so real text in
        # brackets is left alone.
        t = re.sub(r"^\\[(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) \\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}(?::\\d{2})? [+-]\\d{2}(?::?\\d{2})?\\]\\s*", "", t).strip()
        if len(t) < cls._OVR_MIN_CHARS:
            return False
        # "these days" / "those days" are time idioms, not back-references.
        # Without this, "how does the BOJ set rates these days" tripped the
        # deictic rule and was queued instead of parallelised (same class of
        # false positive as "there" in _OVR_DEICTIC).
        _scan = re.sub(r"\\b(these|those)\\s+days\\b", " ", t.lower())
        # Apostrophes are stripped so "what's"/"it's"/"let's" match the same
        # entries as "whats"/"its"/"lets" instead of silently missing.
        words = [w.replace("'", "") for w in re.findall(r"[a-z0-9']+", _scan)]
        words = [w for w in words if w]
        if not words:
            return False
        if words[0] in cls._OVR_OPENERS:
            return False
        if any(w in cls._OVR_DEICTIC for w in words):
            return False
        # An artifact-mutating verb anywhere means "act on the work in flight".
        if any(w in cls._OVR_ACTION for w in words):
            return False
        if words[0] not in cls._OVR_INTERROG:
            return False
        anchors = [w for w in words if len(w) >= 3 and w not in cls._OVR_STOP]
        return len(anchors) >= 2

    def _overflow_router_mode(self):
        """Resolve display.busy_overflow_background -> off|independent|all."""
        try:
            cfg = _load_gateway_runtime_config()
            raw = cfg_get(cfg, "display", "busy_overflow_background", default="")
        except Exception:
            return "off"
        mode = str(raw or "").strip().lower()
        if mode in ("independent", "all"):
            return mode
        if mode in ("true", "yes", "on"):
            return "independent"
        return "off"

    def _overflow_router_limit(self, key, default, maximum):
        """Invalid limits disable dispatch; zero is an explicit queue-only cap."""
        try:
            raw = cfg_get(_load_gateway_runtime_config(), "display", key, default=default)
        except Exception:
            return 0
        if isinstance(raw, bool) or not isinstance(raw, int):
            return 0
        return raw if 0 <= raw <= maximum else 0

    async def _run_overflow_background(self, adapter, event, text, task_id, anchor):
        """One owned task covers acknowledgment and generation, including cancellation."""
        acknowledgment = None
        try:
            coroutine = adapter._send_with_retry(
                chat_id=event.source.chat_id,
                content="\\u26a1 Queue busy, running this in parallel",
                reply_to=anchor,
                metadata=self._thread_metadata_for_source(event.source, anchor),
            )
            try:
                acknowledgment = asyncio.create_task(coroutine)
            except BaseException:
                coroutine.close()
                raise
            # Python 3.10 wait_for can swallow caller cancellation when the
            # child completes concurrently (CPython #86296). Own the child
            # explicitly and preserve cancellation at this wait boundary.
            done, _ = await asyncio.wait(
                {acknowledgment}, timeout=self._OVR_ACK_TIMEOUT_SECONDS,
            )
            if acknowledgment in done:
                acknowledgment.result()
            else:
                acknowledgment.cancel()
                await asyncio.gather(acknowledgment, return_exceptions=True)
                logger.debug("Busy-overflow ack timed out")
        except asyncio.CancelledError:
            if acknowledgment is not None:
                acknowledgment.cancel()
                await asyncio.gather(acknowledgment, return_exceptions=True)
            raise
        except Exception:
            logger.debug("Busy-overflow ack send failed", exc_info=True)
        await self._run_background_task(
            text, event.source, task_id, event_message_id=anchor,
        )

    def _overflow_router_done(self, task):
        self._overflow_router_tasks.pop(task, None)
        self._background_tasks.discard(task)
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                logger.warning("Busy-overflow task failed (%s)", type(error).__name__)

    async def _maybe_route_overflow_to_background(self, event, session_key):
        """Send a self-contained overflow follow-up to its own background run.

        Returns True when the event was dispatched (caller must not queue it).
        """
        mode = self._overflow_router_mode()
        if mode == "off":
            return False
        if getattr(event, "internal", False) or event.is_command():
            return False
        if getattr(event, "reply_to_message_id", None):
            return False  # quoted/replied-to context is absent from a cold agent
        text = (event.text or "").strip()
        if not text:
            return False
        if getattr(event, "media_urls", None):
            return False  # media belongs with the album-merge path
        adapter = self._adapter_for_source(event.source)
        if adapter is None:
            return False
        # Only OVERFLOW: something must already be waiting, otherwise this is
        # the first follow-up and the normal queue handles it fine.
        #
        # _queue_depth() counts the pending slot + FIFO overflow but NOT the
        # adapter's text-debounce buffer, where a busy follow-up sits for
        # 0.35-1.0s before it is flushed into the slot. Counting only the slot
        # meant every message arriving inside that window saw depth 0, fell
        # through, and merged -- observed live: three questions
        # merged into one turn while a fourth (sent after the flush) routed
        # correctly. Count the debounce buffer as waiting work.
        depth = self._queue_depth(session_key, adapter=adapter)
        _debounce = getattr(adapter, "_text_debounce", None)
        if isinstance(_debounce, dict) and session_key in _debounce:
            depth += 1
        if depth < 1:
            return False
        if mode == "independent" and not self._classify_busy_followup(text):
            return False

        # No await between admission and registration: concurrent handlers on
        # the gateway event loop cannot oversubscribe a session's capacity.
        active = getattr(self, "_overflow_router_tasks", None)
        if active is None:
            active = self._overflow_router_tasks = {}
        for finished in tuple(active):
            if finished.done():
                active.pop(finished, None)
        limit = self._overflow_router_limit("busy_overflow_max_per_session", 2, 32)
        if sum(key == session_key for key in active.values()) >= limit:
            return False
        total_limit = self._overflow_router_limit("busy_overflow_max_total", 8, 128)
        if len(active) >= total_limit:
            return False

        # Import inside the injected method: installer imports do not exist
        # in gateway/run.py. Use 128 random bits even at burst throughput.
        import secrets
        task_id = "bg_ovr_%d_%s" % (int(time.time()), secrets.token_hex(16))
        anchor = self._reply_anchor_for_event(event)
        # Log before the task exists. The busy handler's caller queues the
        # event when this method raises, and the handler's own except also
        # logs. A NameError from logger.info after create_task therefore
        # runs the follow-up in the background and in the foreground queue.
        logger.info(
            "Busy-overflow routed to background: session=%s mode=%s task=%s len=%d",
            session_key, mode, task_id, len(text),
        )
        # Once owned by the background registry, the caller returns without
        # an await. Ack cancellation cannot make an already dispatched event
        # fall back into the foreground queue and run twice.
        coroutine = self._run_overflow_background(adapter, event, text, task_id, anchor)
        try:
            task = asyncio.create_task(coroutine)
        except BaseException:
            coroutine.close()
            raise
        self._background_tasks.add(task)
        active[task] = session_key
        task.add_done_callback(self._overflow_router_done)
        return True

'''

try:
    src = checked_read(PATH, st).decode("utf-8")
except (OSError, UnicodeError):
    print("ABORT: target must be readable UTF-8"); sys.exit(2)
if "\r" in src.replace("\r\n", ""):
    print("ABORT: unsupported carriage-return line endings"); sys.exit(2)
if "\r\n" in src and "\n" in src.replace("\r\n", ""):
    print("ABORT: mixed line endings are not supported"); sys.exit(2)
line_ending = "\r\n" if "\r\n" in src else "\n"

def target_text(value):
    return value.replace("\n", line_ending)

hook_old = target_text(HOOK_OLD)
hook_new = target_text(HOOK_NEW)
block_marker = target_text(BLOCK_MARKER)
block = target_text(BLOCK)
anchor = target_text(ANCHOR)

def _router_binds(body, name):
    """True when ``name`` is bound by these module-level statements.

    A docstring, a comment, and an import alias do not bind it. A combined
    import and a parenthesized import do. Function and class bodies are not
    module scope.
    """
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == name:
                return True
            continue
        if isinstance(node, (ast.With, ast.AsyncWith)):
            if _router_binds(node.body, name):
                return True
            continue
        if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While)):
            if _router_binds(node.body, name) or _router_binds(node.orelse, name):
                return True
            continue
        if isinstance(node, ast.Try):
            parts = [node.body, node.orelse, node.finalbody]
            parts.extend(handler.body for handler in node.handlers)
            if any(_router_binds(part, name) for part in parts):
                return True
            continue
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return True
            continue
        if isinstance(node, ast.AnnAssign) and node.value is not None and isinstance(node.target, ast.Name) and node.target.id == name:
            return True
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if (alias.asname or alias.name.split(".")[0]) == name:
                    return True
    return False


# Preconditions: the injected router calls these names at runtime. A missing
# one is a NameError. For cfg_get and the config loader, the method catches
# Exception and returns "off", so the router silently disables. A docstring
# or a comment that merely contains the import text is not a binding, and an
# alias binds the other name.
try:
    _router_tree = ast.parse(src)
except SyntaxError:
    _router_tree = None
if _router_tree is not None:
    for required in ("re", "os", "time", "asyncio"):
        if not _router_binds(_router_tree.body, required):
            print("ABORT: missing top-level import %r" % ("import " + required)); sys.exit(2)
    for required in ("_load_gateway_runtime_config", "cfg_get", "logger"):
        if not _router_binds(_router_tree.body, required):
            print("ABORT: gateway runtime symbol %r is not defined or imported at top level" % required); sys.exit(2)

block_count = src.count(block)
marker_count = src.count(block_marker)
hook_new_count = src.count(hook_new)
anchor_count = src.count(anchor)


def recovery_path():
    """Backup path the write uses, chosen by exactly the rule it writes under.

    An upgrade has to keep the copy taken by the first install, so it writes
    beside it. --check resolves the same path so its verdict matches the write
    that follows it.
    """
    path = PATH + ".bak-pre-overflowrouter" + (".reverse" if args.reverse else "")
    if marker_count and not args.reverse:
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return path
        if not stat.S_ISREG(info.st_mode):
            raise OSError("existing recovery backup is not a regular file")
        # Every upgrade keeps the source it replaced, so those copies need one
        # slot each: .upgrade, then .upgrade.2, .upgrade.3. A single .upgrade
        # name made the FIRST upgrade fill the only slot and every later one
        # abort, which left the router on a stale block with no way forward
        # short of deleting a recovery copy by hand.
        for ordinal in range(1, 9):
            slot = path + ".upgrade" if ordinal == 1 else "%s.upgrade.%d" % (path, ordinal)
            if not os.path.lexists(slot):
                return slot
        raise OSError(
            "no free upgrade recovery slot beside %s; preserve or relocate "
            "them before retrying" % path
        )
    return path


if block_count:
    counts = (block_count, hook_new_count, marker_count, anchor_count)
    if counts != (1, 1, 1, 1):
        print("ABORT: malformed current install (block=%d, patched_hook=%d, marker=%d, anchor=%d)" % counts); sys.exit(2)
    if not src.index(block_marker) < src.index(anchor) < src.index(hook_new):
        print("ABORT: injected block marker, anchor, and patched hook are out of order"); sys.exit(2)
    try:
        parses(src)
    except (SyntaxError, ValueError) as error:
        print("ABORT: target syntax is invalid; target unchanged:\n", error); sys.exit(3)
    if not args.reverse:
        print("ALREADY_PATCHED"); sys.exit(0)

if args.reverse:
    if block_count:
        out = src.replace(block, "", 1).replace(hook_new, hook_old, 1)
    elif marker_count:
        print("ABORT: only the exact current router can be reversed"); sys.exit(2)
    elif src.count(hook_old) == 1 and anchor_count == 1:
        print("ALREADY_UNPATCHED"); sys.exit(0)
    else:
        print("ABORT: expected an intact current or unpatched router"); sys.exit(2)
elif marker_count:
    if marker_count != 1:
        print("ABORT: expected exactly 1 injected block marker, found %d" % marker_count); sys.exit(2)
    if hook_new_count != 1:
        print("ABORT: expected exactly 1 patched hook, found %d" % hook_new_count); sys.exit(2)
    if anchor_count != 1:
        print("ABORT: expected exactly 1 anchor, found %d" % anchor_count); sys.exit(2)
    hook_start = src.index(hook_new)
    block_start = src.index(block_marker)
    block_end = src.index(anchor)
    if not block_start < block_end < hook_start:
        print("ABORT: injected block marker, anchor, and patched hook are out of order"); sys.exit(2)
    out = src[:block_start] + block + src[block_end:]
else:
    if src.count(hook_old) != 1:
        print("ABORT: expected exactly 1 hook site, found %d" % src.count(hook_old)); sys.exit(2)
    if anchor_count != 1:
        print("ABORT: expected exactly 1 anchor, found %d" % anchor_count); sys.exit(2)
    out = src.replace(hook_old, hook_new, 1).replace(anchor, block + anchor, 1)

if args.check:
    try:
        parses(out)
    except (SyntaxError, ValueError) as error:
        print("ABORT: candidate syntax is invalid:\n", error); sys.exit(3)
    # The recovery copy is the one precondition an apply can reach after this
    # point, so a verdict that ignored it would approve a write that aborts.
    try:
        conflict = recovery_copy_conflict(recovery_path(), src.encode("utf-8"))
    except OSError as error:
        print("ABORT: the recovery copy blocks this write; target unchanged:\n", error); sys.exit(3)
    if conflict:
        print("ABORT: the recovery copy blocks this write; target unchanged:\n", conflict); sys.exit(3)
    parent = os.path.dirname(os.path.abspath(PATH)) or "."
    if not os.access(parent, os.W_OK | os.X_OK):
        print("ABORT: target directory is not writable; the install cannot create its temporary file:\n", parent); sys.exit(3)
    print("REVERSIBLE" if args.reverse else "UPGRADE_APPLICABLE" if marker_count else "APPLICABLE"); sys.exit(0)

candidate = bytecode = backup_path = None
created_backup = False
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
    backup_path = recovery_path()
    created_backup = write_backup_exclusive(backup_path, src.encode("utf-8"), st.st_mode)
    guard_target()
    os.replace(candidate, PATH)
    created_backup = False
except (py_compile.PyCompileError, OSError) as e:
    if created_backup and backup_path:
        try:
            os.unlink(backup_path)
        except FileNotFoundError:
            pass
    print("ABORT: staged write or compile check failed; target unchanged:\n", e); sys.exit(3)
finally:
    for temporary in (candidate, bytecode):
        if temporary:
            try: os.unlink(temporary)
            except FileNotFoundError: pass
print("REVERSED_OK" if args.reverse else "PATCHED_OK")
