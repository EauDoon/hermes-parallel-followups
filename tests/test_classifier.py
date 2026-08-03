#!/usr/bin/env python3
"""Regression suite for the busy-followup classifier.

Extracts the exact class block the patch injects (via ast, so we test the
shipped source rather than a retyped copy) and runs it over labelled cases.

    python3 tests/test_classifier.py [path/to/apply_busy_overflow_router_patch.py]

Optionally scan your OWN transcript to see how the split falls on real traffic:

    python3 tests/test_classifier.py --db /path/to/state.db

Nothing is uploaded or written; the scan is read-only and prints counts only.
"""
import ast, re, sys, os

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATCH = os.path.join(HERE, os.pardir, "patches",
                             "apply_busy_overflow_router_patch.py")

args = [a for a in sys.argv[1:]]
db_path = None
if "--db" in args:
    i = args.index("--db")
    db_path = args[i + 1]
    del args[i:i + 2]
patch_path = args[0] if args else DEFAULT_PATCH

tree = ast.parse(open(patch_path, encoding="utf-8").read())
block = None
for node in tree.body:
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id == "BLOCK":
                block = ast.literal_eval(node.value)
if block is None:
    print("FAIL: could not extract BLOCK from %s" % patch_path)
    sys.exit(1)

ns = {"re": re}
exec("class _T:\n" + block, ns)          # noqa: S102 - testing shipped source
classify = ns["_T"]._classify_busy_followup
print("classifier loaded from %s (MIN_CHARS=%d)"
      % (os.path.basename(patch_path), ns["_T"]._OVR_MIN_CHARS))

# --- labelled cases --------------------------------------------------------
# MUST_BG: self-contained, safe for an agent with no conversation history.
# MUST_Q : depends on the turn in flight; answering it cold would be wrong.
MUST_BG = [
    "what is the population of Ulaanbaatar today",
    "why did the Bretton Woods system collapse",
    "how does a heat pump compare to a gas boiler",
    "is the central bank likely to raise rates later",
    "are housing starts at risk of falling further",
    "Who is the founder of the Linux kernel",
    "What is the best season for visiting Kyoto",
    "can you look up why the build server keeps failing",
    # "there" must NOT read as a back-reference: existential questions are
    # one of the commonest self-contained forms.
    "are there any good hiking trails near the coast?",
    # "these days" is a time idiom, not a back-reference.
    "how does the central bank set interest rates these days",
    # a leading gateway timestamp prefix must not defeat classification
    "[Thu 2026-07-23 16:58:23 +08] What are the benefits of cold water swimming",
    # apostrophes must normalise ("what's" == "whats")
    "what's the difference between a stub and a mock",
]

MUST_Q = [
    "do it",
    "tell me more about (2) and (3)",
    "yes let's set it up",
    "leave it, let's see how the job runs",
    "Its not correct",
    "which ones are the high tier ones?",
    "Do option 2. Amend the document",
    "Solve for (4) as you recommended",
    "Help me solve this",
    "you are using the medium setting?",
    "ok implement this change",
    "Can we also amend the config to refill the top 2 entries",
    "can you make the section titles bolded",
    "what about the team at the other company?",
    "who is using the API with his?",
    '[Replying to: "an earlier answer"] what does that mean',
    "at this moment, how are we planning to use both models?",
    "skip the ones I need to submit manually",
    "why dont we set it to 1m?",
]

fails = []
for m in MUST_BG:
    if not classify(m):
        fails.append(("EXPECTED background, got queued", m))
for m in MUST_Q:
    if classify(m):
        fails.append(("EXPECTED queued, got background", m))

print("\n=== REGRESSION SUITE: %d cases ===" % (len(MUST_BG) + len(MUST_Q)))
if fails:
    print("  FAILURES: %d" % len(fails))
    for why, m in fails:
        print("   ! %-34s %s" % (why, m[:60]))
else:
    print("  ALL PASS")

# --- optional: scan your own transcript ------------------------------------
if db_path:
    import sqlite3
    c = sqlite3.connect(db_path)
    rows = [r[0].strip() for r in
            c.execute("select content from messages where role=?", ("user",))
            if isinstance(r[0], str) and r[0].strip()]
    ind = sum(1 for m in rows if classify(m))
    n = len(rows) or 1
    print("\n=== YOUR TRANSCRIPT: n=%d ===" % len(rows))
    print("  -> background (independent): %d (%.1f%%)" % (ind, 100 * ind / n))
    print("  -> stay queued (dependent) : %d (%.1f%%)" % (n - ind, 100 * (n - ind) / n))
    print("  (counts only; no message content is printed or stored)")

sys.exit(1 if fails else 0)
