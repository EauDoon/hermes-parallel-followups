#!/usr/bin/env python3
"""Busy-queue overflow router: run a self-contained busy follow-up as its own
background task instead of merging it into the queued turn.

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
import ast, sys, py_compile, os, stat, tempfile, argparse, hashlib

__version__ = "1.0.0"

EPILOG = """results (one line on stdout):
  APPLICABLE          --check: the patch can be installed
  UPGRADE_APPLICABLE  --check: an older injected generation can be upgraded
  ALREADY_PATCHED     the current patch is already installed; nothing changed
  REVERSIBLE          --check --reverse: the current patch, or a router block
                      from an earlier release, can be removed
  ALREADY_UNPATCHED   --reverse: no patch is installed; nothing changed
  PATCHED_OK          the patch was installed or upgraded
  REVERSED_OK         the patch was removed
  ABORT: <reason>     refused; the target is unchanged

Exit codes: 0 success, 2 incompatible target or argument, 3 staging, compile,
recovery or target-change failure.

Recovery copies sit beside the target and are never overwritten:
  <target>.bak-pre-overflowrouter          source before a fresh install
  <target>.bak-pre-overflowrouter.upgrade  source before an upgrade
  <target>.bak-pre-overflowrouter.reverse  source before an uninstall
Each name has seven more slots, .2 to .8. A copy already there is kept and
the next slot is taken (a fresh install or an uninstall reuses one that is
identical), and a write that finds all eight taken aborts with exit 3.

The router is off until display.busy_overflow_background is set to
independent or all in the gateway config. See the README for the limits.
"""

parser = argparse.ArgumentParser(
    description=(__doc__ or "").split("\n\n", 1)[0],
    epilog=EPILOG,
    formatter_class=argparse.RawDescriptionHelpFormatter,
)
parser.add_argument("path", nargs="?", default="/opt/hermes/gateway/run.py",
                    help="the gateway/run.py to patch (default: %(default)s)")
parser.add_argument("--check", action="store_true", help="validate applicability without writing files")
parser.add_argument("--reverse", action="store_true", help="remove the current patch, or a router block from an earlier release, while preserving unrelated edits")
parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
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


def recovery_slot(base, contents):
    """First recovery copy name beside ``base`` that the write may use.

    The names are ``base``, then ``base.2`` up to ``base.8``. An absent name
    is used, and a regular file that already holds exactly ``contents`` is
    reused. A regular file holding anything else is kept, and the next name is
    tried. A single name made a reapply after a reverse and an edit, or a
    second reverse after a reapply, abort on the copy the earlier cycle left,
    with no way forward short of moving recovery files by hand. A non-regular
    entry still aborts, and nothing is ever overwritten.
    """
    for ordinal in range(1, 9):
        slot = base if ordinal == 1 else "%s.%d" % (base, ordinal)
        try:
            info = os.lstat(slot)
        except FileNotFoundError:
            return slot
        if not stat.S_ISREG(info.st_mode):
            raise OSError("existing recovery backup is not a regular file")
        if checked_read(slot) == contents:
            return slot
    raise OSError(
        "no free recovery slot beside %s; preserve or relocate them "
        "before retrying" % base
    )

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
                # "the 2nd one" and "the 3rd step" point into a list on the
                # table. Before a calendar or legal noun ("the 18th century",
                # "the 14th amendment", "the 16th president") an ordinal names
                # a thing anyone can look up. "edition" and "place" stay
                # queued: "the 2nd edition" usually means a copy in the chat.
                r"|\\b\\d+\\s*(st|nd|rd|th)\\b(?![\\s-]+(?:wedding[\\s-]+)?(?:century|centuries|millennium|amendment|anniversary|birthday|dynasty|president)\\b)"
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
                # A cold agent cannot see what the user said earlier, or what the
                # assistant produced. "did I"/"have I" plus a verb, "I said", and
                # "the X you produced" all point back into the turn in flight.
                # Present "do I choose" is a how-to question and stays routable.
                r"|\\b(?:did|have|had)\\s+I\\s+(?:say|mention|ask|tell|send|share|give|write|upload|attach|paste|decide|choose|pick|agree|mean)\\b"
                r"|\\bI\\s+(?:said|mentioned|asked|told|wrote|meant|pasted)\\b"
                r"|\\bthe\\s+\\w+(?:\\s+(?!if\\b|when\\b|whenever\\b|once\\b|after\\b|before\\b|because\\b|unless\\b|until\\b|and\\b|or\\b)\\w+)?\\s+you\\s+(?:showed|shown|quoted|cited|found|loaded|ran|used|made|created|built|listed|produced|generated|computed|calculated|estimated|provided|described|explained|proposed|drafted|shared|sent|linked|picked|chose|gave|wrote|mentioned|suggested|recommended)\\b"
                # "your answer" and "your figures" name the assistant's own earlier
                # output. A definite artifact noun followed by a preposition ("the
                # output of", "the spec for") is usually a general question.
                r"|\\byour\\s+(?:answer|reply|response|summary|output|table|chart|figures?|numbers|list|calculation|math|draft|script)\\b"
                r"|\\b(?:the|my|our)\\s+(?:script|spreadsheet|attachment|screenshot|diff|dataset|email|table|chart|summary|output|logs?|proposal|spec)\\b(?!\\s+(?:of|for|from|in|on|to|at|with|between|about)\\b)"
                r"|\\b(above|earlier|previous|previously)\\b"
                r"|\\bthe\\s+(?:former|latter)\\b"
                # "the last answer" was queued. "the first answer" and "the
                # second reply" name the same turn and were not.
                r"|\\b(?:first|second|third|next|last)\\s+(?:one|answer|reply|message|point)\\b"
                r"|\\b(what|how)\\s+about\\b"
                # "also known as" and "also called" name a thing, and route as
                # the same question without "also" does. A leading "also" is
                # still an opener and stays queued.
                r"|\\balso\\b"
                r"(?!\\s+(?:known|called|named|referred)\\b)"
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
        if not isinstance(raw, bool) and isinstance(raw, int) and 0 <= raw <= maximum:
            return raw
        # An invalid value used to disable dispatch with no trace, so an
        # operator could not tell why nothing ran in parallel. Warn once per
        # key and value. The warning is advice only: nothing in it may change
        # the fail-closed result.
        try:
            marker = (key, repr(raw))
            seen = self.__dict__.setdefault("_ovr_invalid_limits", set())
            if marker not in seen:
                seen.add(marker)
                logger.warning(
                    "Busy-overflow display.%s=%r is not an integer from 0 "
                    "to %d; parallel dispatch is disabled",
                    key, raw, maximum,
                )
        except Exception:
            pass
        return 0

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
                # Owner cancellation raises at the wait above, so a child
                # that ended cancelled here was cancelled by someone else.
                # That is a failed send. result() would re-raise it, end
                # this task as cancelled, and drop an admitted follow-up.
                if acknowledgment.cancelled():
                    try:
                        logger.debug("Busy-overflow ack was cancelled")
                    except Exception:
                        pass
                else:
                    acknowledgment.result()
            else:
                acknowledgment.cancel()
                await asyncio.gather(acknowledgment, return_exceptions=True)
                try:
                    logger.debug("Busy-overflow ack timed out")
                except Exception:
                    pass
        except asyncio.CancelledError:
            if acknowledgment is not None:
                acknowledgment.cancel()
                await asyncio.gather(acknowledgment, return_exceptions=True)
            raise
        except Exception:
            # Admission already returned True, so the caller will not queue
            # this event. A broken diagnostic sink must not drop its run.
            try:
                logger.debug("Busy-overflow ack send failed", exc_info=True)
            except Exception:
                pass
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
        # create_task has already scheduled the follow-up. If recording it
        # raises, the caller queues the same event. Cancel the orphan first
        # so it cannot also generate.
        try:
            self._background_tasks.add(task)
            active[task] = session_key
            task.add_done_callback(self._overflow_router_done)
        except BaseException:
            active.pop(task, None)
            self._background_tasks.discard(task)
            task.cancel()
            raise
        return True

'''

# Every router block that shipped on main before BLOCK, longest first, as
# (length of its LF text in characters, sha256 of its UTF-8 bytes, label).
# An install of one of these is upgraded or reversed exactly, keeping any code
# that sits between it and the busy-handler anchor. 1.0.0 is the only tagged
# release; the rest were installed from main before tagging began.
# tests/test_installer_generations.py derives this set from the first-parent
# history of main, so a release that changes BLOCK fails the suite until the
# block it replaces is listed here.
PREVIOUS_BLOCKS = (
    (21426, "42f2e003b828b2c9c7c95fe9ac4aeea3d69d5c2dc3f12921e36a5664b2d299bf", "untagged main build"),
    (20893, "9adffdfb78d160165d6f83c6282940d1aa269c6c4dee33e6fa598b976089bc55", "untagged main build"),
    (20587, "2c657f6091ea2a30d86b3fbe7a814a187c04964a2c4965a1a3fb8f4806b803bf", "1.0.0"),
    (18348, "dc071d28f2dd81f5675a6fd98e1e29d595b6e009ef6a48fdd801682e73cf60a0", "untagged main build"),
    (17976, "3cfdb964c1536d9976804f021132f566e78717b3dc2f7ae9a01cd2c6633ef031", "untagged main build"),
    (17596, "0fa4b7fbee42f2e3c50b5eb7c95470a7855e8ff9bda41939e8e11a2d7afbe1d4", "untagged main build"),
    (17425, "37c7772c495cdc2951aaa941e426d977ed46c0ad17862c55310a5e9ff9a5d22f", "untagged main build"),
    (17111, "10e2c8bc16168ae3c59788e87f62641970714ff33512df45500ad85232ea54ed", "untagged main build"),
    (16662, "590b1ad083fa431081464590c500c5b790aa1fb497c5e7e40bab06354f3d4caa", "untagged main build"),
    (16246, "3a6c500e62641fa313485654054588cf5c9e7a7524a32f17a9a3b3cb5a63d2b7", "untagged main build"),
    (15987, "77182d19efc8b12b835560d5264881ccb342ac129e355ec6384948add005b2f3", "untagged main build"),
    (15116, "0f68c109f3f030402c14683557a346c0cb5af527c7efad283cf3b5c950b81000", "untagged main build"),
    (14817, "ced1a5b90bf9650f439a285c84d1f7f3ad03b85688c3168908dce526e3be83c3", "untagged main build"),
    (14173, "9d1932865d8e335b7219c416467b2e09d6a2385339ab3ee31a0229661d96de33", "untagged main build"),
    (13893, "ddff9bcf37fd7fda2c2da8a2dfcb2a50712f52bc9985422b41c387192cc1b00e", "untagged main build"),
    (10238, "d1083aa4070a06a4e695ecdcb8cd527711b7a39f30a0a015a2aed1d80037f934", "untagged main build"),
    (9894, "22bcef100ad9b59bcaf797d0c3686e2c16e2a301e2ac3225d5fa86c61508bd89", "untagged main build"),
    (9783, "b2322dd3521e11dfdf792752cde360fc4fb6968829ead74cd3bf7855e864f493", "untagged main build"),
    (9555, "b9da1d8ca43c63f9879b205f6524d9754f14ceb2ef27248897f4bcdae1638ce3", "untagged main build"),
    (9504, "2863adf20bb4c530b0d8862b4621479ea1c16232cc2c0b56f16e99775de93451", "untagged main build"),
)

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
        if isinstance(node, (ast.With, ast.AsyncWith)):
            if _binds(node.body, name):
                return True
            continue
        if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While)):
            if _binds(node.body, name) or _binds(node.orelse, name):
                return True
            continue
        # try/except* (3.11+) has the same fields as try/except. getattr keeps
        # this parseable and correct on 3.10, which has no TryStar.
        if isinstance(node, (ast.Try, getattr(ast, "TryStar", ast.Try))):
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


# The module-level names BLOCK and HOOK_NEW load at runtime. The injected code
# never uses os, so a run.py without `import os` is not refused for it.
# tests/test_installer_parity.py derives this set from the injected source,
# so a new name there fails the suite until it is listed here.
REQUIRED_IMPORTS = ("re", "time", "asyncio")
REQUIRED_RUNTIME = ("_load_gateway_runtime_config", "cfg_get", "logger")


def require_router_symbols():
    """Refuse an install whose injected router would NameError.

    Reverse removes those calls, so a file that does not bind the names can
    still be uninstalled. A missing cfg_get is otherwise swallowed and the
    router stays off. A docstring or a comment is not a binding, and an
    alias binds the other name. A file that does not parse is reported by
    the later syntax check rather than as a missing name. The decoded text
    keeps a UTF-8 byte order mark, which ast.parse reads as a SyntaxError, so
    it is stripped first or a BOM would skip this check.
    """
    try:
        tree = ast.parse(src.lstrip("\ufeff"))
    except SyntaxError:
        return
    for required in REQUIRED_IMPORTS:
        if not _binds(tree.body, required):
            print("ABORT: missing top-level import %r" % ("import " + required)); sys.exit(2)
    for required in REQUIRED_RUNTIME:
        if not _binds(tree.body, required):
            print("ABORT: gateway runtime symbol %r is not defined or imported at top level" % required); sys.exit(2)

block_count = src.count(block)
marker_count = src.count(block_marker)
hook_new_count = src.count(hook_new)
anchor_count = src.count(anchor)


class Refusal(Exception):
    """A layout no write may touch; the message is the ABORT reason."""


ANCHOR_STAND_IN = "    def _busy_handler_anchor(self):\n        pass\n"


def class_body(text_lf):
    """Parse LF class-body text that sits directly above the busy-handler anchor.

    Returns (statements, anchor_decorated), or None when the text is not a
    class body. A stand-in for the anchor method is appended, so a decorator
    left above the anchor still parses; it is reported, not counted.
    """
    try:
        tree = ast.parse("class _T:\n    pass\n" + text_lf + ANCHOR_STAND_IN)
    except (SyntaxError, ValueError):
        return None
    body = tree.body[0].body if isinstance(tree.body[0], ast.ClassDef) else []
    stand_in = body[-1] if body else None
    if not (isinstance(stand_in, ast.FunctionDef) and stand_in.name == "_busy_handler_anchor"):
        return None
    return body[1:-1], bool(stand_in.decorator_list)


def defined_names(node):
    """Class attributes one class-body statement defines."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
    return [name.id for target in targets for name in ast.walk(target) if isinstance(name, ast.Name)]


# Every name a router block defines. All earlier blocks define a subset.
ROUTER_MEMBERS = frozenset(name for node in class_body(BLOCK)[0] for name in defined_names(node))


def identify_region(region):
    """(label, rest) when ``region`` starts with a block in PREVIOUS_BLOCKS.

    ``region`` runs from the block marker to the busy-handler anchor. ``rest``
    is the LF text after the block. It belongs to someone else, so an upgrade
    or a reverse must keep it. None when no listed block matches.
    """
    region_lf = region.replace("\r\n", "\n")
    for length, digest, label in PREVIOUS_BLOCKS:
        if (len(region_lf) >= length
                and hashlib.sha256(region_lf[:length].encode("utf-8")).hexdigest() == digest):
            return label, region_lf[length:]
    return None


def older_install():
    """Locate the router region of an older install.

    Returns (block_start, block_end, known). ``known`` is (label, rest) for a
    released block and None for an edited or unknown one. Raises Refusal for
    a layout that no write may touch.
    """
    if marker_count != 1:
        raise Refusal("expected exactly 1 injected block marker, found %d" % marker_count)
    if hook_new_count != 1:
        raise Refusal("expected exactly 1 patched hook, found %d" % hook_new_count)
    if anchor_count != 1:
        raise Refusal("expected exactly 1 anchor, found %d" % anchor_count)
    hook_start = src.index(hook_new)
    block_start = src.index(block_marker)
    block_end = src.index(anchor)
    if not block_start < block_end < hook_start:
        raise Refusal("injected block marker, anchor, and patched hook are out of order")
    known = identify_region(src[block_start:block_end])
    if known is not None:
        parsed = class_body(known[1])
        if parsed is None:
            raise Refusal("the code between the router block and the busy-handler anchor does not parse")
        repeated = sorted({name for node in parsed[0] for name in defined_names(node)} & ROUTER_MEMBERS)
        if repeated:
            raise Refusal("malformed install: router members repeated after the block (%s)" % ", ".join(repeated))
    return block_start, block_end, known


def foreign_code(block_start, block_end):
    """Name the first part of an unknown router region that is not router code.

    An unknown region (an edited block, or a build that never shipped) is
    replaced whole on upgrade, which is only safe when every statement in it
    defines a router member. Anything else there would be deleted. Returns
    None for a router-only region; raises Refusal when it does not parse.
    """
    parsed = class_body(src[block_start:block_end].replace("\r\n", "\n"))
    if parsed is None:
        raise Refusal("the injected router region does not parse as class code")
    statements, decorated = parsed
    first_line = src.count("\n", 0, block_start)
    for node in statements:
        names = defined_names(node)
        outside = [name for name in names if name not in ROUTER_MEMBERS]
        if outside or not names:
            line = first_line + node.lineno - 2
            return ("%r on line %d" % (outside[0], line)) if outside else ("a statement on line %d" % line)
    if decorated:
        return "a decorator on the busy-handler anchor"
    return None


def recovery_path():
    """Backup path the write uses, chosen by exactly the rule it writes under.

    An upgrade has to keep the copy taken by the first install, so it writes
    beside it. A fresh install and a reverse take the first free or identical
    slot of their own name. --check resolves the same path so its verdict
    matches the write that follows it.
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
    return recovery_slot(path, src.encode("utf-8"))


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
        require_router_symbols()
        print("ALREADY_PATCHED"); sys.exit(0)

if args.reverse:
    if block_count:
        out = src.replace(block, "", 1).replace(hook_new, hook_old, 1)
    elif marker_count:
        # A released older block is removed exactly, together with the
        # patched hook, so a 1.0.0 install no longer has to be upgraded
        # before it can be uninstalled. Code after the block is kept.
        try:
            block_start, block_end, known = older_install()
        except Refusal as refusal:
            print("ABORT: %s" % refusal); sys.exit(2)
        if known is None:
            print("ABORT: only the current or a released router block can be reversed; "
                  "this block is edited or unknown"); sys.exit(2)
        try:
            parses(src)
        except (SyntaxError, ValueError) as error:
            print("ABORT: target syntax is invalid; target unchanged:\n", error); sys.exit(3)
        out = src[:block_start] + target_text(known[1]) + src[block_end:]
        out = out.replace(hook_new, hook_old, 1)
    elif src.count(hook_old) == 1 and anchor_count == 1:
        print("ALREADY_UNPATCHED"); sys.exit(0)
    else:
        print("ABORT: expected an intact current or unpatched router"); sys.exit(2)
elif marker_count:
    # The upgrade used to replace everything from the marker to the anchor,
    # so a method between the old block and the busy handler was deleted
    # while the installer printed PATCHED_OK. A released block is now
    # replaced by itself, and an unknown region only when all of it is
    # router code.
    try:
        block_start, block_end, known = older_install()
        foreign = None if known else foreign_code(block_start, block_end)
    except Refusal as refusal:
        print("ABORT: %s" % refusal); sys.exit(2)
    if foreign:
        print("ABORT: code that is not part of the router sits between the router block "
              "and the busy-handler anchor (%s); target unchanged" % foreign); sys.exit(2)
    require_router_symbols()
    rest = target_text(known[1]) if known else ""
    out = src[:block_start] + block + rest + src[block_end:]
else:
    if src.count(hook_old) != 1:
        print("ABORT: expected exactly 1 hook site, found %d" % src.count(hook_old)); sys.exit(2)
    if anchor_count != 1:
        print("ABORT: expected exactly 1 anchor, found %d" % anchor_count); sys.exit(2)
    require_router_symbols()
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
