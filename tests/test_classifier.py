#!/usr/bin/env python3
"""Regression suite for the busy-followup classifier.

Extracts the exact class block the patch injects (via ast, so we test the
shipped source rather than a retyped copy) and runs it over labelled cases.

    python3 tests/test_classifier.py [path/to/apply_busy_overflow_router_patch.py]

Optionally scan your OWN transcript to see how the split falls on real traffic:

    python3 tests/test_classifier.py --db /path/to/state.db

Nothing is uploaded or written; the scan is read-only and prints counts only.
"""
import ast, re, sys, os, argparse
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATCH = os.path.join(HERE, os.pardir, "patches",
                             "apply_busy_overflow_router_patch.py")

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("patch", nargs="?", default=DEFAULT_PATCH)
parser.add_argument("--db", help="read-only SQLite transcript; prints aggregate counts only")
args = parser.parse_args()
db_path = args.db
patch_path = args.patch

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
    "what’s the difference between a stub and a mock",
    "Ｗｈａｔ are the principal benefits of solar energy?",
    # The first-person rule is matched with its verb, so an ordinary question
    # that merely mentions a country or asks to be told something stays
    # routable.
    "what is the trade deficit of the US compared with Japan",
    "tell me about the population of Reykjavik",
    "what is the best option a family has for heating a home in winter",
    "what is the first city to exceed ten million people",
    "what is the population of Sa\u0303o Paulo and why does the census matter",
    # "did you recommend" is a back-reference. A request to recommend
    # something new is self-contained and must stay routable.
    "can you recommend a city with reliable winter transit",
    # A newline is ordinary text. Backspace and DEL are not, and are covered
    # with the queued cases below.
    "what is the population of Ulaanbaatar today\nand why does the census matter for planning",
    # "did I say" asks about the turn in flight. Present "do I" plus a verb is
    # a how-to question about the world and stays routable, as do "should I"
    # and "what should I say to" a third party.
    "how do I make sourdough bread rise more in winter",
    "how should I invest for retirement in my thirties",
    "can you explain how a heat pump works in winter",
    "what language should I learn first for data science",
    "how do I choose a laptop for programming on a budget",
    "how do I pick a ripe watermelon at the grocery store",
    "how do I decide between two job offers in different cities",
    "how do I write a cover letter for a data analyst role",
    "how do I attach a file to an email on an iphone",
    "how do I send a large video file to a friend",
    "how do I recall an email I sent by mistake in outlook",
    "what should I say to a landlord about a late rent payment",
    # "the X you produced" is a back-reference. An impersonal "if you ran" or
    # "if you used" is a hypothetical about anyone, not about this assistant.
    "what happens to the body if you ran a marathon untrained",
    "what happens if you used bleach on colored clothes",
    "what if you made a mistake on a tax return last year",
]

MUST_Q = [
    "do it",
    "tell me more about (2) and (3)",
    "tell me more about option two",
    # Digits and number-words are queued. A lettered choice is the same
    # back-reference: "(b)" and "option B" only resolve against the list
    # already on the table. Lowercase "option a family" is ordinary English
    # and stays in the background cases below.
    "compare (a) with (b) for the long term operating cost",
    "how does option A compare with option B for long term cost",
    "which second option should we choose",
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
    "what did you mean by the final recommendation?",
    "do you mean we should use the other model instead?",
    "could you explain what you meant by the final recommendation?",
    "can you clarify what you mean by the proposed architecture?",
    # A cold agent cannot see an artifact identified only by the conversation.
    "Can you summarize the report for me?",
    "Why is it’s formatting inconsistent across the examples?",
    "Why is th\u200bis configuration failing during startup?",
    # Backspace and DEL are not format characters, but they hide the same
    # tokens. "the former" and "also" must still force the queue.
    "how does the for\x08mer compare with geothermal power",
    # A combining grapheme joiner is not a format character or a control,
    # but it still splits "former" so the phrase match never sees it.
    # NFD "São" recomposes under NFKC and stays in the background cases.
    "how does the for\u034fmer compare with geothermal power today",
    "what is the population of Oslo and al\x08so Bergen today",
    "What are the implications of the conclusion?\n> earlier response",
    "[project2026] What are the most effective migration methods?",
    # "the former"/"the latter" resolve only against the turn in flight. A
    # background agent starts with no history, so it cannot tell which of two
    # previously offered options is meant.
    "how does the former compare to the latter",
    # "the last answer" is queued. "the first answer" and "the second reply"
    # are the same back-reference and were backgrounded. "the first city"
    # below stays routable: the noun is what makes it a turn reference.
    "what was the first answer about inflation targets today",
    "what was the second reply about inflation targets today",
    "which of the latter two options is cheaper to run",
    # Deliberate residual, in the same class as "the previous champions": a
    # phrase match cannot tell the pronoun from the adjective, and this
    # classifier resolves that ambiguity toward the queue.
    "what are the former champions of the Tour de France",
    # A first-person follow-up asks about state the cold agent cannot see:
    # what "we" agreed, decided, or meant is only in the turn in flight.
    "what did we decide about the budget allocation",
    "how does our current exposure compare to the index",
    "why do you think we should wait for the print",
    # The wh-word is not always the token before the auxiliary, and the
    # auxiliary is often contracted. The inverted form is the same question.
    "which model did we pick for the summary task",
    "why don't we set the timeout to thirty seconds",
    "should we wait for the quarterly print",
    "are we still using the old rate limit",
    # Apostrophes are stripped for the opener list, so "what's" is an
    # interrogative, but the back-reference scan still sees "what's our"
    # rather than "what is our" and lets the question run cold.
    "what's our timeout for the summary task right now",
    "what’s my deadline for the summary task right now",
    # "you recommended" matches, but the grammatical form is "did you
    # recommend". A cold agent has no memory of what it recommended, decided,
    # or said, so these have to stay queued.
    "why did you recommend the smaller model for the summary task",
    "what did you decide about the timeout configuration",
    "what did you say about monetary policy in the seventies",
    # The user's own earlier words and the assistant's earlier output are
    # only in the conversation. "did I", "I said" and "the figure you quoted"
    # were all answered cold.
    "what did I say about the deadline for the launch",
    "did I mention the budget limit for the offsite",
    "what did I ask you to change in the proposal",
    "how accurate is the estimate you produced for revenue",
    "what were the results you found for the regression",
    "how many rows are in the dataset you loaded",
    "what is the source for the figure you quoted",
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
    from contextlib import closing
    n = ind = 0
    try:
        # URI escaping handles spaces, # and ? without changing the path.
        # mode=ro refuses missing files instead of creating an empty database.
        uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            connection.execute("PRAGMA query_only=ON")
            for (content,) in connection.execute(
                    "select content from messages where role=?", ("user",)):
                if isinstance(content, str) and content.strip():
                    n += 1
                    ind += bool(classify(content))
    except (OSError, sqlite3.Error):
        print("ERROR: cannot read a transcript with messages(role, content); no data was modified")
        sys.exit(2)
    denominator = n or 1
    print("\n=== YOUR TRANSCRIPT: n=%d ===" % n)
    print("  -> background (independent): %d (%.1f%%)" % (ind, 100 * ind / denominator))
    print("  -> stay queued (dependent) : %d (%.1f%%)" % (n - ind, 100 * (n - ind) / denominator))
    print("  (counts only; no message content is printed or stored)")

sys.exit(1 if fails else 0)
