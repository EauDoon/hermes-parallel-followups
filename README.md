# hermes-parallel-followups

Two drop-in patches for [Nous Research Hermes](https://github.com/NousResearch/hermes-agent) that keep busy-session follow-ups separate and can run safe, self-contained questions in parallel.

## The problem

With `display.busy_input_mode: queue`, the gateway returns `False` for plain text received during an active turn. The adapter then flushes follow-ups into one pending slot and newline-joins them:

```text
busy TEXT -> early return -> debounce -> one pending slot -> newline merge
```

Three questions become one turn. The model sees an unlabeled block, so question and answer pairing becomes unreliable. The debugging evidence and synthetic reproduction are in [docs/CASE_STUDY.md](docs/CASE_STUDY.md).

## The fix

| Patch | Change | Use |
| --- | --- | --- |
| [`apply_debounce_fifo_patch.py`](patches/apply_debounce_fifo_patch.py) | Routes each flushed busy-text burst through Hermes's existing FIFO entry point. Each burst gets its own turn in arrival order. The 0.35 second rolling debounce and 1.0 second hard cap remain in place. | Start here. It fixes message boundaries on its own. |
| [`apply_busy_overflow_router_patch.py`](patches/apply_busy_overflow_router_patch.py) | When something is already waiting, sends a self-contained text follow-up to a bounded background task. In `independent` mode, context-dependent text stays queued. | Optional parallelism. It is off by default. |

The FIFO patch falls back to the historical pending-slot merge when no runner is attached, a different adapter owns the source, the pending slot or the burst is a photo or already has media URLs, or the FIFO declines at its cap. This preserves delivery while accepting merged text at those boundaries. A video, voice, or document with no media URLs is not that case: the historical merge would replace the slot and drop it, so the flush hands it to the FIFO instead. A burst from a different sender is also handed to the FIFO, so it becomes its own turn instead of staying in the debounce store after the timer has been cancelled. The historical merge is not used for that burst, because it would mix the two senders into one slot.

The router supports `off`, `independent`, and `all` through a custom display key:

```yaml
display:
  busy_overflow_background: independent  # off | independent | all
  busy_overflow_max_per_session: 2
  busy_overflow_max_total: 8
```

| Key | Accepted values | Default |
| --- | --- | --- |
| `busy_overflow_background` | `off`, `independent`, or `all`. `true`, `yes`, and `on`, including the YAML booleans they load as, mean `independent`. Anything else means `off`. | `off` |
| `busy_overflow_max_per_session` | An integer from 0 to 32 | 2 |
| `busy_overflow_max_total` | An integer from 0 to 128 | 8 |

A limit that is out of range or not an integer disables dispatch, and the gateway log gets one warning for each distinct invalid value. Zero is a valid limit that disables dispatch without a warning.

`independent` routes only self-contained questions. `all` also routes contextual text and carries more correctness risk because a background agent starts without conversation history. Commands, media, internal events, empty text, and explicit reply events remain queued. Invalid limits fail closed, zero disables dispatch, and completed, failed, or canceled tasks release capacity. Acknowledgment cancellation is requested after five seconds. Supported adapters must cooperate with cancellation; the router awaits their cleanup while retaining the task and its capacity slot. An adapter that suppresses cancellation can exceed that deadline, so this is not a hard end-to-end timeout. Cancellation does not send the same event through both lanes.

The router key is custom. `hermes config set` may warn that it is not recognized; the patch reads it directly.

## Compatibility before installation

| Source revision | Status |
| --- | --- |
| `d7b36070ef807841699ad32c5b6af547fee3ff64` | Supported pinned source fixture; CI checks its exact hashes and the patch lifecycle. |
| `ed2d821021e073425994544dca292d36a12cf4a3` | Known incompatible router structure; its hook is absent. |
| Any other revision | Unverified. An `APPLICABLE` result checks source anchors and preconditions, not runtime compatibility. |

Do not bypass an anchor failure or assume a newer Hermes release is supported.
The fixture gate runs selected methods with synthetic events, not a complete
Hermes gateway or a live model. The exact hashes and local command are below.

## Quick start

Inspect the exact target before creating a backup:

```bash
python3 patches/apply_debounce_fifo_patch.py /path/to/hermes/gateway/platforms/base.py --check
python3 patches/apply_busy_overflow_router_patch.py /path/to/hermes/gateway/run.py --check
```

Stop the gateway and other source writers, then apply one or both patches:

```bash
python3 patches/apply_debounce_fifo_patch.py /path/to/hermes/gateway/platforms/base.py
python3 patches/apply_busy_overflow_router_patch.py /path/to/hermes/gateway/run.py
```

Restart the gateway after applying. With no path, both scripts use the standard `/opt/hermes` targets. If the deployment recreates its container, reapply the patches from the image-update hook.

The installers refuse symlinks and non-regular files, require exact anchors, stage and compile the replacement before touching the target, detect target drift, and atomically replace the file. A staging or compile failure leaves the target unchanged. The debounce installer also requires the replaced block to sit inside `_flush_text_debounce_now`, so a file that merely contains the same ten lines in some other method is refused rather than rewritten. An upgrade of an older flush body uses that same rule. Each installer also refuses a target that does not bind the names its injected code calls at runtime (`logger` and `MessageType` in `base.py`; `logger`, the `re`, `time`, and `asyncio` imports, `_load_gateway_runtime_config`, and `cfg_get` in `run.py`), because a missing name would raise at the first busy follow-up instead of at install time. The offline suite derives those lists from the injected code, so they cannot drift from what the patch actually calls. Reverse does not apply that check: it removes those calls, so an already installed patch can still be uninstalled when the names are missing. `--check` creates no backup, bytecode, or temporary file, and it evaluates the read-only preconditions for the inspected state: it syntax-checks the exact bytes an apply would write, it resolves the recovery copy that write would need, refusing with exit code 3 and the same reason when a copy already on disk is not one the write may reuse, and it refuses when the target directory fails the access check for staging. This is a point-in-time preflight, not a guarantee that a later write succeeds: disk space, ownership changes, filesystem errors, or another writer can still make apply abort. Apply repeats its target guards and handles actual staging and replacement failures. Successful checks print `APPLICABLE`, `UPGRADE_APPLICABLE` for a router or debounce flush-body upgrade, or `ALREADY_PATCHED`; reverse checks print `REVERSIBLE`, or `ALREADY_UNPATCHED` when no patch remains. Exit code 2 means an incompatible target or argument. Exit code 3 means a staging, compile, recovery, or target-change failure. Replacement is not a transaction with an unrelated concurrent writer, so stop other writers first.

## Reverse and recover

Reverse only the exact current patch:

```bash
python3 patches/apply_debounce_fifo_patch.py /path/to/hermes/gateway/platforms/base.py --reverse --check
python3 patches/apply_debounce_fifo_patch.py /path/to/hermes/gateway/platforms/base.py --reverse
python3 patches/apply_busy_overflow_router_patch.py /path/to/hermes/gateway/run.py --reverse
```

Backups are adjacent to the originals as `*.bak-pre-debouncefifo` and `*.bak-pre-overflowrouter`. Existing backups are never overwritten. An exact recovery copy may be reused after a reverse and reapply; a different existing copy aborts the write. Router upgrades preserve the prior installed source in `.upgrade`, and later upgrades in `.upgrade.2`, `.upgrade.3`, and so on, so each generation stays recoverable and no upgrade is locked out by an earlier one. A debounce flush-body upgrade uses those same slot names beside `*.bak-pre-debouncefifo`, and leaves the original recovery copy untouched. Reversal preserves the patched source in `.reverse`. Reversal changes only the exact current patch, so unrelated source edits remain; an edited or older router block aborts. Before manual recovery, compare the installed file with the original backup and each `.upgrade` generation, record their hashes, and identify which generation contains the intended pre-change source. Never copy a backup over unrelated edits blindly; prefer exact `--reverse` when it is applicable. Restart the gateway after reversal. See the [case study](docs/CASE_STUDY.md) for the fallback and cancellation tradeoffs.

## Pinned fixture and evidence

The supported source snapshot is [d7b36070ef807841699ad32c5b6af547fee3ff64](https://github.com/NousResearch/hermes-agent/commit/d7b36070ef807841699ad32c5b6af547fee3ff64), selected on 20-07-2026. The pinned validator requires these exact source hashes before it uses disposable copies:

```text
gateway/platforms/base.py  6bfdf20de31ae01fbd088457b91252d2430f9bc45d0a84ba132590be54fc909f
gateway/run.py             36429599eefc193ba6b33c077d0f92b3933f1173c8577b9ac61c3767dddbda89
gateway/authz_mixin.py     bfe908efbe0504d3803571195cee92ac6717d9c5a0eda81e79549f8bff61b11f
```

`tests/validate_upstream.py` first checks, statically and without executing anything, every upstream member the patches call: the `_send_with_retry` keywords the router acknowledgment passes, the bound busy handler that `set_busy_session_handler` stores and that every call in `run.py` passes, the `_text_debounce` and `_background_tasks` stores, the signatures of the runner methods the router and the flush call, and `_adapter_for_source`, which `GatewayRunner` inherits from `gateway/authz_mixin.py`. It then checks apply, check, reverse, and reapply, and exercises selected real FIFO and debounce methods with synthetic events. It never downloads source, imports an installed gateway, or edits the supplied fixture.

A separate `pinned-source` CI job runs `tests/fetch_pinned_source.py`. It downloads only these files from that exact public revision without credentials, verifies every hash before it writes or executes anything, and then runs this validator on disposable copies. It does not fetch mutable main or use deployed source.

To reproduce the job locally, fetch and verify the fixture, then validate it:

```bash
python3 tests/fetch_pinned_source.py ./upstream-fixture
python3 tests/validate_upstream.py ./upstream-fixture
```

`python3 tests/fetch_pinned_source.py ./upstream-fixture --validate` does both in one step, as CI does. To use files obtained another way, supply a directory containing the pinned `base.py`, `run.py`, and `authz_mixin.py` directly (without the `gateway/` subdirectories). The validator refuses any file whose hash differs.

Revision [ed2d821021e073425994544dca292d36a12cf4a3](https://github.com/NousResearch/hermes-agent/commit/ed2d821021e073425994544dca292d36a12cf4a3), checked on 09-09-2026, has a different runner structure. The router installer refuses it because the required hook is absent. Compatibility with that revision and newer releases is unsupported. Do not bypass an anchor failure.

## Checks

Run the dependency-free suite with Python 3.10 or newer:

```bash
python3 tests/run_offline.py
```

It covers the classifier, router gates, concurrent admission and cancellation, installer failure and lifecycle paths, and read-only transcript scanning. It does not import an installed Hermes. The runner fails if a `tests/test_*.py` file is neither listed in `CHECKS` nor named as needing an installed Hermes, so a new test cannot be added and then silently never run. CI runs once for each pull request update and once for each push to main. Python 3.10, 3.11, 3.12, 3.13, and 3.14 on Linux and Windows are blocking jobs; Python 3.15 runs on both systems as a non-blocking pre-release job until its final release. Dependabot proposes updates to the pinned workflow actions weekly. The real-source checks `tests/test_debounce_fifo.py` and `tests/test_burst_fullpath.py` require Hermes at `/opt/hermes`; they are separate from the offline suite. For a read-only aggregate split of your own transcript, run `python3 tests/test_classifier.py --db /path/to/state.db`; it prints counts only. See [docs/CASE_STUDY.md](docs/CASE_STUDY.md) for the recorded local result and evidence boundary.

## License and provenance

MIT. See [LICENSE](LICENSE).

Hermes Agent is MIT licensed, Copyright (c) 2025 Nous Research. These patches are a derivative work and quote small portions of that source for context. They are an independent contribution and are not affiliated with or endorsed by Nous Research.
