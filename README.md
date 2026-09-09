# hermes-parallel-followups

Two drop-in patches for [Nous Research Hermes](https://github.com/NousResearch/hermes-agent) that fix message jumbling when you send several messages while the agent is busy, and let self-contained follow-ups run in parallel instead of waiting in the queue.

Supported source snapshot: **[d7b36070ef807841699ad32c5b6af547fee3ff64](https://github.com/NousResearch/hermes-agent/commit/d7b36070ef807841699ad32c5b6af547fee3ff64)**,
the public gateway revision selected at the original 20-07-2026 support date.
Both installers pass apply, check, reverse, and reapply on its real gateway files.
The validator also runs selected real FIFO/debounce methods with synthetic events.
Full gateway startup, live delivery, and model calls are not covered by these checks.

Upstream revision **[ed2d821021e073425994544dca292d36a12cf4a3](https://github.com/NousResearch/hermes-agent/commit/ed2d821021e073425994544dca292d36a12cf4a3)**,
checked on 09-09-2026, has a different runner structure. The router installer
refuses it because the hook is absent. Do not assume compatibility with newer
Hermes releases or bypass an anchor failure.

---

## The symptom

You run with `display.busy_input_mode: queue`. The agent is working on something. You send three more messages while you wait. When the turn finishes, you get one reply that mixes all three questions together, answers them out of order, or pairs an answer to the wrong question.

## Why it happens

With `busy_input_mode: queue`, the gateway's busy handler declines to handle plain TEXT and returns `False`:

```python
# gateway/run.py, _handle_active_session_busy_message
if (
    event.message_type == MessageType.TEXT
    and busy_text_mode == "queue"
    and effective_mode != "steer"
):
    return False
```

Control falls through to the adapter's debounce, which flushes into a **single pending slot**:

```python
# gateway/platforms/base.py, merge_pending_message_event
existing.text = f"{existing.text}\n{event.text}"
```

That merge has no time bound. Every text message sent during the turn is newline-joined into one event, and the drain pops **one** event and runs it as a single turn. Message boundaries are gone, so the model receives an unlabelled blob and answers it blob-shaped.

This is not a new observation: the Hermes source already calls it a bug. The comment at the FIFO site describes the raw merge as destroying message boundaries "so two separate user messages sent while the agent was busy arrived as one mashed-together turn", and routes through `_enqueue_fifo` to fix it. That fix covers interrupt mode, steer-fallback and `/queue`. The queue-mode text path returns `False` before it ever reaches the FIFO.

Every branch around that early return carries a detailed rationale comment. That one does not.

## What the patches do

### 1. `apply_debounce_fifo_patch.py` — stops the merging

Targets `gateway/platforms/base.py`.

`_flush_text_debounce_now` now hands each flushed burst to the runner's `_queue_or_replace_pending_event`, the same FIFO entry point interrupt mode and `/queue` already use, instead of merging it into the pending slot. Each follow-up gets its own turn in arrival order.

Merging inside the debounce window (0.35s rolling, 1.0s hard cap) is preserved, which is correct. A single thought split across two quick taps should stay one turn.

It reaches the runner through the bound `_busy_session_handler` the runner already installs on the adapter, so it needs no wiring changes in `run.py` and stays a single-file patch.

**Fallback behavior.** `_queue_or_replace_pending_event` can decline *silently* — it returns without queueing and without raising when the source resolves to no adapter, or when the per-session cap (`_BUSY_QUEUE_MAX_PENDING`, 32) is reached. Treating that as success would drop the burst, and the cap was effectively unreachable before this change because the old merge collapsed every follow-up into one slot rather than one entry each. So the flush confirms the queue actually grew and falls back to the historical merge when it did not. Three cases take the historical path:

- **No runner attached** (standalone adapter use, tests)
- **A media occupant in the pending slot** — it gets caption-merged without growing the queue, which the depth check would misread as a decline and merge a second time
- **A source resolving to a different adapter** — that adapter owns a different pending slot, and the drain that delivers this burst runs on ours

Merging is lossy; dropping is worse. Every one of these is covered by a test.

**This is the patch most people want. It is useful on its own.**

### 2. `apply_busy_overflow_router_patch.py` — optional parallelism

Targets `gateway/run.py`.

When the agent is busy **and something is already waiting**, a self-contained follow-up is dispatched to its own background agent rather than queued. Background results come back labelled with the prompt that produced them:

```
✅ Background task complete
Prompt: "<your question>"
```

Context-dependent messages stay in the queue and reach the running conversation in order.

Off by default. Enable with:

```yaml
display:
  busy_overflow_background: independent   # off | independent | all
  busy_overflow_max_per_session: 2         # integer, 0 to 32
  busy_overflow_max_total: 8               # integer, 0 to 128
```

- `off` — no behavior change (default)
- `independent` — only self-contained messages are backgrounded
- `all` — contextual text may be backgrounded, but command, media, internal,
  empty, and explicit reply events remain queued

Parallel work is bounded to two active overflow tasks per session and eight
across the runner by default. At either limit, messages follow the normal queue
path. Completed, failed, and canceled tasks release capacity. These limits count
only this patch's overflow tasks, not manually started background work. Zero
disables dispatch; invalid values fail closed to queueing. Lowering a cap prevents
new admissions and lets existing tasks finish.

Acknowledgment and generation belong to one tracked task. The routing handler
returns immediately after taking ownership, so canceling an acknowledgment
cannot send the same message through both lanes. Acknowledgments time out after
five seconds; a send failure still permits generation. Gateway cancellation
cancels the owned task and releases capacity. Failed tasks are observed and
release capacity, but this patch does not retry failed generation.
Acknowledgment uses an explicitly owned child task so cancellation remains
reliable on Python 3.10 and 3.11 even when acknowledgment completes concurrently.

`hermes config set` will warn that this is not a recognized key. That is expected; it is a custom key and the patch reads it directly.

## Important limitation of the router

A background agent starts **cold**. It gets no conversation history, and its answer is never written back into the main transcript. That is why the classifier is deliberately biased toward queueing: a queued message merely waits, whereas a wrongly-backgrounded one is answered blind.

The split depends on the messages. A misroute toward the queue costs parallelism
only. Tune the word lists in the patch to move that line, then run the classifier
regressions. The classifier is a heuristic, not a guarantee that a cold agent has
all necessary context.

## The classifier

Shape-based, no extra model call. A message stays queued if any of these hold:

- a back-reference: `(2)`, `option 2`, `as you said`, `you are`, `what about`, `also`, `[Replying to`
- a continuation opener: `ok`, `yes`, `leave`, `skip`, `implement`, `instead`, ...
- a bare deictic: `it`, `this`, `that`, `ones`, `his`, ...
- an artifact-mutating verb anywhere: `amend`, `edit`, `update`, `bold`, ...
- fewer than 25 characters
- no interrogative opener, or fewer than two content words

Three false positives that only real sentences exposed, all fixed and all regression-tested:

| input | was | cause |
|---|---|---|
| "are **there** any good trails nearby" | queued | `there` treated as a back-reference |
| "**what's** the difference between X and Y" | queued | apostrophes not normalized against `whats` |
| "how does the bank set rates **these days**" | queued | `these days` read as a back-reference |

Known residual: "why is it there" still queues on the pronoun `it`. Distinguishing that from a real back-reference needs parsing, and the safe direction is to queue.

## Install

Both patches are standalone scripts. They are idempotent, refuse symlink and non-regular targets, stage and `py_compile` the replacement before touching the live file, then back up and atomically replace the target. A staging or compile failure leaves the target unchanged.

Inspect compatibility without creating backups, bytecode, or temporary files:

```bash
python3 patches/apply_debounce_fifo_patch.py /path/to/hermes/gateway/platforms/base.py --check
python3 patches/apply_busy_overflow_router_patch.py /path/to/hermes/gateway/run.py --check
```

Successful checks print `APPLICABLE`, `UPGRADE_APPLICABLE` (router only), or
`ALREADY_PATCHED`. Exit code 2 means incompatible input or arguments; code 3
means syntax, staging, recovery, or target-change failure. Stop the gateway and
other source writers before applying or reversing. Identity and content checks
detect edits during staging, but filesystem replacement is not a transaction
with an unrelated concurrent writer.

```bash
python3 patches/apply_debounce_fifo_patch.py /path/to/hermes/gateway/platforms/base.py
python3 patches/apply_busy_overflow_router_patch.py /path/to/hermes/gateway/run.py
```

Both default to the standard container paths (`/opt/hermes/...`) when no argument is given. Restart the gateway afterwards.

If your deployment recreates the container, keep these where your image-update hook re-applies them; a plain in-container edit will not survive.

Backups are written next to the originals as `*.bak-pre-debouncefifo` and
`*.bak-pre-overflowrouter`. Existing backups are never overwritten. An exact
matching recovery copy can be reused after reverse/reapply; a different copy
aborts the write. Router upgrades preserve the prior installed source in an
additional `.upgrade` backup.

Reverse only the exact current patch while preserving unrelated source edits:

```bash
python3 patches/apply_debounce_fifo_patch.py /path/to/hermes/gateway/platforms/base.py --reverse --check
python3 patches/apply_debounce_fifo_patch.py /path/to/hermes/gateway/platforms/base.py --reverse
python3 patches/apply_busy_overflow_router_patch.py /path/to/hermes/gateway/run.py --reverse
```

Reversal stages and checks the result, saves the patched source in a `.reverse`
backup, and prints `REVERSED_OK`. Repeating it prints `ALREADY_UNPATCHED`.
An edited or older router block must be reviewed or upgraded before reversal.
Restart the gateway after applying or reversing. Keep backups for recovery.

## Tests

Run the complete dependency-free suite on Python 3.10 or newer:

```bash
python3 tests/run_offline.py
```

This includes classifier and router gates, concurrent admission and cancellation,
installer failures and lifecycle checks, and private transcript scanning fixtures.
It does not import an installed Hermes. CI runs it on Linux and Windows with
Python 3.10, 3.11, 3.12, and 3.14. Each offline test process has a 60-second
deadline and reports failure if exceeded. Windows hosts without symlink privileges skip the
four symlink checks; Linux CI exercises them.

For real pinned-source verification, obtain `gateway/platforms/base.py` and
`gateway/run.py` from the supported revision above, save them together as
`base.py` and `run.py`, then run:

```bash
python3 tests/validate_upstream.py /path/to/pristine-fixture
```

The validator verifies both SHA-256 hashes before using disposable copies. It
checks byte-exact reversal and reapplication, then executes the real selected
FIFO/debounce methods for ordered bursts, session isolation, timer cancellation,
and cap fallback. It never edits the supplied files, imports an installed gateway,
or downloads source automatically.

```bash
python3 tests/test_classifier.py        # labelled classifier cases
python3 tests/test_router.py            # 13 router gate cases
python3 tests/test_debounce_fifo.py     # real _flush_text_debounce_now, run in-container
python3 tests/test_burst_fullpath.py    # 10-message burst through real handle_message
```

`test_debounce_fifo.py` and `test_burst_fullpath.py` import from a live Hermes install and must run where `/opt/hermes` is importable.

`test_burst_fullpath.py` is the one worth reading. It fires ten back-to-back messages at a busy session through the real `handle_message`, with real `MessageEvent` / `SessionSource` / `GatewayRunner` objects, stubbing only the outermost I/O. It asserts that no queued turn fuses two messages, that nothing is lost, that arrival order holds, and that no context-dependent message reaches a cold agent.

`test_debounce_fifo.py` fails deliberately on unpatched code. Run it before patching and you should see the merge reproduced.

You can check the classifier against your own transcript without shipping any data anywhere:

```bash
python3 tests/test_classifier.py --db /path/to/state.db
```

That prints counts only, never message content.

## Caveats

- Version-specific. The patches assert on exact anchor text and abort cleanly if it is not found, so a mismatched Hermes version fails loudly rather than corrupting a file.
- With the router enabled, background agents run on your **main** model, so they add concurrent API calls. If you route auxiliary work through the same key and plan, watch for rate limiting.
- At the 32-message pending cap (`_BUSY_QUEUE_MAX_PENDING`), the debounce patch
  preserves overflow text by merging it into the historical pending slot. This
  avoids loss but sacrifices separate turns and strict arrival order at capacity.
  The upstream decline warning may say "Dropping" before this fallback preserves
  the text. No user-visible capacity notice is added by the FIFO patch.

## License

MIT. See [LICENSE](LICENSE).

Hermes Agent is MIT licensed, Copyright (c) 2025 Nous Research. These patches are a derivative work and quote small portions of that source for context. They are an independent contribution and are not affiliated with or endorsed by Nous Research.
