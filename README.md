# hermes-parallel-followups

Two drop-in patches for [Nous Research Hermes](https://github.com/NousResearch/hermes-agent) that fix message jumbling when you send several messages while the agent is busy, and let self-contained follow-ups run in parallel instead of waiting in the queue.

Tested against **Hermes Agent v0.19.0 (2026.7.20)**.

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
```

- `off` — no behavior change (default)
- `independent` — only self-contained messages are backgrounded
- `all` — every overflow message is backgrounded

`hermes config set` will warn that this is not a recognized key. That is expected; it is a custom key and the patch reads it directly.

## Important limitation of the router

A background agent starts **cold**. It gets no conversation history, and its answer is never written back into the main transcript. That is why the classifier is deliberately biased toward queueing: a queued message merely waits, whereas a wrongly-backgrounded one is answered blind.

On one real transcript the split was roughly 13% backgrounded, 87% queued. A misroute toward the queue costs parallelism only, never correctness. Tune the word lists in the patch to move that line.

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

Both patches are standalone scripts. They are idempotent, back up the file they touch, `py_compile` the result, and restore the backup if compilation fails.

```bash
python3 patches/apply_debounce_fifo_patch.py /path/to/hermes/gateway/platforms/base.py
python3 patches/apply_busy_overflow_router_patch.py /path/to/hermes/gateway/run.py
```

Both default to the standard container paths (`/opt/hermes/...`) when no argument is given. Restart the gateway afterwards.

If your deployment recreates the container, keep these where your image-update hook re-applies them; a plain in-container edit will not survive.

Backups are written next to the originals as `*.bak-pre-debouncefifo` and `*.bak-pre-overflowrouter`. To roll back, copy the backup over the original and restart.

## Tests

```bash
python3 tests/test_classifier.py        # 35 labelled classifier cases
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
- The 32-message pending cap (`_BUSY_QUEUE_MAX_PENDING`) now applies to text follow-ups that previously merged. Well beyond any real conversation, but it drops with a log warning rather than a user-visible one.

## License

MIT. See [LICENSE](LICENSE).

Hermes Agent is MIT licensed, Copyright (c) 2025 Nous Research. These patches are a derivative work and quote small portions of that source for context. They are an independent contribution and are not affiliated with or endorsed by Nous Research.
