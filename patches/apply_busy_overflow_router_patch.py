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
    off          - default, no behaviour change
    independent  - Option B: only self-contained messages are backgrounded
    all          - Option A: every overflow message is backgrounded

Idempotent, backed up, syntax-checked.
Usage: apply_busy_overflow_router_patch.py [/opt/hermes/gateway/run.py]
"""
import sys, py_compile, shutil, os

PATH = sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes/gateway/run.py"

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
            rx = re.compile(
                r"\\(\\s*\\d+\\s*\\)"
                r"|\\boption\\s*\\d"
                r"|\\bpoint\\s*\\d"
                r"|\\bpart\\s*\\d"
                r"|\\bstep\\s*\\d"
                r"|#\\d"
                r"|\\b\\d+\\s*(st|nd|rd|th)\\b"
                r"|\\bas\\s+you\\s+\\w+"
                r"|\\byou\\s+(said|mentioned|recommended|suggested|are|were|just|gave|wrote)"
                r"|\\byou\\s+(mean|meant|meaning)\\b"
                r"|\\b(above|earlier|previous|previously)\\b"
                r"|\\blast\\s+(one|answer|reply|message|point)\\b"
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
        t = (text or "").strip()
        # Quote-replies and back-references are contextual by definition.
        if cls._ovr_backref_re().search(t):
            return False
        # Drop a leading gateway timestamp prefix ("[Thu 2026-07-23 16:58 +08]").
        # Matched on a bracketed group containing a 4-digit year so real text in
        # brackets is left alone.
        t = re.sub(r"^\\[[^\\]]*\\d{4}[^\\]]*\\]\\s*", "", t).strip()
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

    async def _maybe_route_overflow_to_background(self, event, session_key):
        """Send a self-contained overflow follow-up to its own background run.

        Returns True when the event was dispatched (caller must not queue it).
        """
        mode = self._overflow_router_mode()
        if mode == "off":
            return False
        if getattr(event, "internal", False) or event.is_command():
            return False
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

        task_id = "bg_ovr_%d_%s" % (int(time.time()), os.urandom(3).hex())
        anchor = self._reply_anchor_for_event(event)
        task = asyncio.create_task(
            self._run_background_task(
                text,
                event.source,
                task_id,
                event_message_id=anchor,
            )
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        logger.info(
            "Busy-overflow routed to background: session=%s mode=%s task=%s len=%d",
            session_key, mode, task_id, len(text),
        )
        try:
            await adapter._send_with_retry(
                chat_id=event.source.chat_id,
                content="\\u26a1 Queue busy, running this in parallel",
                reply_to=anchor,
                metadata=self._thread_metadata_for_source(event.source, anchor),
            )
        except Exception:
            logger.debug("Busy-overflow ack send failed", exc_info=True)
        return True

'''

src = open(PATH, encoding="utf-8", newline="").read()
if BLOCK in src:
    print("ALREADY_PATCHED"); sys.exit(0)

# Preconditions: required module imports must already exist at top level.
for need in ("\nimport re\n", "\nimport os\n", "\nimport time\n", "\nimport asyncio\n"):
    if need not in src:
        print("ABORT: missing top-level import %r" % need.strip()); sys.exit(2)
if src.count(HOOK_OLD) != 1:
    print("ABORT: expected exactly 1 hook site, found %d" % src.count(HOOK_OLD)); sys.exit(2)
if src.count(ANCHOR) != 1:
    print("ABORT: expected exactly 1 anchor, found %d" % src.count(ANCHOR)); sys.exit(2)

st = os.stat(PATH)
shutil.copy2(PATH, PATH + ".bak-pre-overflowrouter")
out = src.replace(HOOK_OLD, HOOK_NEW, 1).replace(ANCHOR, BLOCK + ANCHOR, 1)
open(PATH, "w", encoding="utf-8", newline="").write(out)
try:
    py_compile.compile(PATH, doraise=True)
except py_compile.PyCompileError as e:
    shutil.copy2(PATH + ".bak-pre-overflowrouter", PATH)
    print("ABORT: syntax error, restored backup:\n", e); sys.exit(3)
os.chmod(PATH, st.st_mode & 0o777)
print("PATCHED_OK")
