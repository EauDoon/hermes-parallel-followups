# Case study: fixing busy-queue message jumbling

This case study explains one narrow debugging result: a busy Hermes session can turn several separate follow-ups into one unlabeled turn. It records the smallest effective fix, the safety fallbacks around it, and the local evidence that can be reproduced without an installed Hermes gateway.

**Evidence status:** offline checks passed on 28-09-2026 with Python 3.12.10 on Windows. The pinned Hermes source fixture and live gateway checks were not run in that check.

## 1. The symptom

Assume the agent is already processing a turn and `display.busy_input_mode` is `queue`. A user sends three separate questions:

```text
1. what are the health benefits of cold plunges
2. who founded Anthropic and when
3. why did the Ming dynasty ban maritime trade
```

The historical queue path can leave one pending event shaped like this:

```text
what are the health benefits of cold plunges
who founded Anthropic and when
why did the Ming dynasty ban maritime trade
```

The drain then runs one turn. The model receives a block without message boundaries, so the response can mix questions, answer in the wrong order, or pair an answer with the wrong prompt. The failure is message merging, not a model quality issue.

## 2. Root cause

The busy handler in `gateway/run.py` returns `False` for plain text in queue mode. That deliberately falls through to the platform adapter. The adapter's `_flush_text_debounce_now` historically called `merge_pending_message_event(..., merge_text=True)`, which appends each flushed burst to the single pending slot with a newline.

Hermes already has a FIFO entry point, `_queue_or_replace_pending_event`. The FIFO is used by other busy-input paths, including interrupt mode, steer fallback, and `/queue`. Queue-mode text returned before reaching it. The debounce window was useful for joining two quick taps that form one thought, but it did not put a bound on merging across a long active turn.

## 3. Minimal fix

The debounce patch changes the flush boundary so a flushed burst is offered to the runner's existing FIFO entry point. It reaches that runner through the bound `_busy_session_handler` already installed on the adapter. No new queue or event type is needed.

```text
flushed burst -> existing FIFO -> pending slot plus ordered overflow entries
```

The result is one queued turn per flushed burst, in arrival order. The 0.35 second rolling debounce and 1.0 second hard cap stay unchanged, so two taps inside the debounce window can still represent one thought.

The optional router is a separate choice. When at least one follow-up is already waiting, `independent` mode can send a self-contained text question to `_run_background_task` while context-dependent text remains in the main queue. `all` can also route contextual text and carries a higher cold-context risk. The router is disabled by default.

## 4. Fallback and cancellation tradeoffs

The boundary fix keeps delivery as the priority when the FIFO cannot safely accept an event:

| Situation | Behavior | Cost |
| --- | --- | --- |
| No runner, unresolved adapter, or different adapter owns the source | Use the historical pending-slot merge | The text is delivered, but boundaries can merge. |
| A media event occupies the pending slot | Preserve the historical caption merge | Media semantics take priority over separate text turns. |
| The FIFO reaches its 32-event pending cap or declines silently | Use the historical merge | Delivery is preserved, but strict separation and ordering can be lost at capacity. |
| Two taps arrive inside the debounce window | Keep them in one burst | This intentionally treats a split thought as one turn. |

The router has its own conservative limits:

- `independent` routes only text that looks self-contained. `all` can route contextual text and therefore has a higher cold-context risk.
- A background agent starts without conversation history, and its answer is not written into the main transcript. In `independent` mode, ambiguous, corrective, artifact-mutating, quoted, media, command, internal, empty, and short messages stay queued. `all` relaxes the classifier for contextual text.
- The default cap is two overflow tasks per session and eight across the runner. Zero disables dispatch. Invalid values fail closed to queueing.
- The acknowledgment and generation share one owned task. The acknowledgment has a five-second timeout. A send failure still permits generation, while owner cancellation cancels the acknowledgment and generation together. Completed, failed, and canceled tasks release capacity. Failed generation is observed and is not retried.
- Background work uses the main model, so enabling it can add concurrent provider calls and rate-limit pressure.

These choices make a false queue classification slower, while a false background classification can answer a context-dependent question without the conversation that explains it.

### Classifier detail

The classifier is shape-based and makes no extra model call. In `independent` mode it queues a message when it finds a back-reference such as `(2)`, `option 2`, `as you said`, `what about`, `the former`, `what did we decide`, or `also`; a continuation opener such as `ok`, `leave`, `implement`, or `instead`; a deictic token such as `it`, `this`, `that`, or `ones`; an artifact-mutating verb such as `amend`, `edit`, `update`, or `bold`; fewer than 25 characters; no interrogative opener; or fewer than two content words.

Regression cases cover existential `there`, apostrophe normalization in `what's`, and the time idiom `these days`. First-person back-references are matched together with their verb, so `what did we decide` and `what should we do` queue while an ordinary question that merely mentions the US, or asks to be told about a fact, stays routable. Two safe residuals remain. `why is it there` stays queued because parsing is needed to distinguish its pronoun from a back-reference, and `the former champions of the Tour de France` stays queued because a phrase match cannot tell the pronoun from the adjective. The optional transcript command reads SQLite in read-only mode and prints aggregate counts only.

## 5. Reproduce the offline case

The existing tests execute the exact injected classifier and router blocks, then exercise installer lifecycle and failure paths with disposable files. They do not import an installed Hermes gateway.

From the repository root, run:

```bash
python3 tests/run_offline.py
```

The recorded Windows run used Python 3.12.10 on 28-09-2026. Use your verified Python interpreter to run the same command above.

Recorded result:

```text
classifier: 52 cases, ALL PASS
router integration: 13 cases, 0 failures
test_router_lifecycle.py: 13 tests, OK
test_patch_installers.py: 17 tests, OK (skipped=4)
test_patch_workflows.py: 5 tests, OK
test_transcript_scan.py: 4 tests, OK
OFFLINE_CHECKS_OK
```

The four skipped installer checks are Windows symlink cases that require symlink privilege. Linux CI exercises those cases. The offline result is synthetic evidence for the injected logic and installer behavior. It is not evidence of gateway startup, message delivery, model calls, throughput, or a production deployment.

## 6. Source compatibility boundary

The supported source snapshot is [d7b36070ef807841699ad32c5b6af547fee3ff64](https://github.com/NousResearch/hermes-agent/commit/d7b36070ef807841699ad32c5b6af547fee3ff64), selected on 20-07-2026. `tests/validate_upstream.py` is the source gate. It requires these exact hashes, applies both patches to disposable copies, checks reverse and reapply, and runs selected real FIFO and debounce methods with synthetic events:

```text
gateway/platforms/base.py  6bfdf20de31ae01fbd088457b91252d2430f9bc45d0a84ba132590be54fc909f
gateway/run.py             36429599eefc193ba6b33c077d0f92b3933f1173c8577b9ac61c3767dddbda89
```

That fixture gate was not run in the local check above because no matching public source fixture was supplied. The separate `tests/test_debounce_fifo.py` and `tests/test_burst_fullpath.py` checks require an installed Hermes at `/opt/hermes` and were not run.

Revision [ed2d821021e073425994544dca292d36a12cf4a3](https://github.com/NousResearch/hermes-agent/commit/ed2d821021e073425994544dca292d36a12cf4a3), checked on 09-09-2026, has a different runner structure. The router installer refuses it because its required hook is absent. Compatibility with that revision and newer releases is unsupported. An anchor failure must not be bypassed.

## 7. Install and recovery guardrails

Both installers are standalone and idempotent. They require exact source anchors, refuse symlinks and non-regular targets, stage and compile before replacement, detect target changes, and use atomic replacement. `--check` is read-only. A failed staging, compile, recovery, or target-change check leaves the target unchanged.

Before applying or reversing, stop the gateway and other writers of the target files. Backups stay beside the originals as `*.bak-pre-debouncefifo` and `*.bak-pre-overflowrouter`; existing backups are not overwritten. A matching recovery copy can be reused after reverse and reapply. Router upgrades retain the prior installed source in `.upgrade`, and reversal retains the patched source in `.reverse`. Reverse only the exact current patch and restart the gateway afterwards.

The full commands and exit-code meanings are in the [README](../README.md). The source and installer behavior remain version-specific by design.

## License and provenance

MIT. See [LICENSE](../LICENSE).

Hermes Agent is MIT licensed, Copyright (c) 2025 Nous Research. This repository is a derivative work that quotes small portions of Hermes source for context. It is an independent contribution and is not affiliated with or endorsed by Nous Research.
