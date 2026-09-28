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

The FIFO patch falls back to the historical pending-slot merge when no runner is attached, a different adapter owns the source, the pending slot or the burst is a photo or already has media URLs, or the FIFO declines at its cap. This preserves delivery while accepting merged text at those boundaries. A video, voice, or document with no media URLs is not that case: the historical merge would replace the slot and drop it, so the flush hands it to the FIFO instead.

The router supports `off`, `independent`, and `all` through a custom display key:

```yaml
display:
  busy_overflow_background: independent  # off | independent | all
  busy_overflow_max_per_session: 2
  busy_overflow_max_total: 8
```

`independent` routes only self-contained questions. `all` also routes contextual text and carries more correctness risk because a background agent starts without conversation history. Commands, media, internal events, empty text, and explicit reply events remain queued. Invalid limits fail closed, zero disables dispatch, and completed, failed, or canceled tasks release capacity. Acknowledgments time out after five seconds, and cancellation cannot send the same event through both lanes.

The router key is custom. `hermes config set` may warn that it is not recognized; the patch reads it directly.

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

The installers refuse symlinks and non-regular files, require exact anchors, stage and compile the replacement before touching the target, detect target drift, and atomically replace the file. A staging or compile failure leaves the target unchanged. The debounce installer also requires the replaced block to sit inside `_flush_text_debounce_now`, so a file that merely contains the same ten lines in some other method is refused rather than rewritten. Each installer also refuses a target that does not bind the names its injected code calls at runtime (`logger` and `MessageType` in `base.py`; the module imports, `_load_gateway_runtime_config`, and `cfg_get` in `run.py`), because a missing name would raise at the first busy follow-up instead of at install time. `--check` creates no backup, bytecode, or temporary file, and it evaluates the same preconditions the install does: it syntax-checks the exact bytes an apply would write, and it resolves the recovery copy that write would need, refusing with exit code 3 and the same reason when a copy already on disk is not one the write may reuse. Its verdict therefore always matches the install that follows it. Successful checks print `APPLICABLE`, `UPGRADE_APPLICABLE` for a router or debounce flush-body upgrade, or `ALREADY_PATCHED`; reverse checks print `REVERSIBLE`, or `ALREADY_UNPATCHED` when no patch remains. Exit code 2 means an incompatible target or argument. Exit code 3 means a staging, compile, recovery, or target-change failure. Replacement is not a transaction with an unrelated concurrent writer, so stop other writers first.

## Reverse and recover

Reverse only the exact current patch:

```bash
python3 patches/apply_debounce_fifo_patch.py /path/to/hermes/gateway/platforms/base.py --reverse --check
python3 patches/apply_debounce_fifo_patch.py /path/to/hermes/gateway/platforms/base.py --reverse
python3 patches/apply_busy_overflow_router_patch.py /path/to/hermes/gateway/run.py --reverse
```

Backups are adjacent to the originals as `*.bak-pre-debouncefifo` and `*.bak-pre-overflowrouter`. Existing backups are never overwritten. An exact recovery copy may be reused after a reverse and reapply; a different existing copy aborts the write. Router upgrades preserve the prior installed source in `.upgrade`, and later upgrades in `.upgrade.2`, `.upgrade.3`, and so on, so each generation stays recoverable and no upgrade is locked out by an earlier one. A debounce flush-body upgrade uses those same slot names beside `*.bak-pre-debouncefifo`, and leaves the original recovery copy untouched. Reversal preserves the patched source in `.reverse`. Reversal changes only the exact current patch, so unrelated source edits remain; an edited or older router block aborts. Restart the gateway after reversal. See the [case study](docs/CASE_STUDY.md) for the fallback and cancellation tradeoffs.

## Compatibility and evidence

The supported source snapshot is [d7b36070ef807841699ad32c5b6af547fee3ff64](https://github.com/NousResearch/hermes-agent/commit/d7b36070ef807841699ad32c5b6af547fee3ff64), selected on 20-07-2026. The pinned validator requires these exact source hashes before it uses disposable copies:

```text
gateway/platforms/base.py  6bfdf20de31ae01fbd088457b91252d2430f9bc45d0a84ba132590be54fc909f
gateway/run.py             36429599eefc193ba6b33c077d0f92b3933f1173c8577b9ac61c3767dddbda89
```

`tests/validate_upstream.py` checks apply, check, reverse, and reapply, then exercises selected real FIFO and debounce methods with synthetic events. It never downloads source, imports an installed gateway, or edits the supplied fixture.

Supply a directory containing the pinned `base.py` and `run.py` directly (without the `gateway/` subdirectories), then run:

```bash
python3 tests/validate_upstream.py ./upstream-fixture
```

Revision [ed2d821021e073425994544dca292d36a12cf4a3](https://github.com/NousResearch/hermes-agent/commit/ed2d821021e073425994544dca292d36a12cf4a3), checked on 09-09-2026, has a different runner structure. The router installer refuses it because the required hook is absent. Compatibility with that revision and newer releases is unsupported. Do not bypass an anchor failure.

## Checks

Run the dependency-free suite with Python 3.10 or newer:

```bash
python3 tests/run_offline.py
```

It covers the classifier, router gates, concurrent admission and cancellation, installer failure and lifecycle paths, and read-only transcript scanning. It does not import an installed Hermes. The runner fails if a `tests/test_*.py` file is neither listed in `CHECKS` nor named as needing an installed Hermes, so a new test cannot be added and then silently never run. CI runs on Linux and Windows with Python 3.10, 3.11, 3.12, and 3.14. The real-source checks `tests/test_debounce_fifo.py` and `tests/test_burst_fullpath.py` require Hermes at `/opt/hermes`; they are separate from the offline suite. For a read-only aggregate split of your own transcript, run `python3 tests/test_classifier.py --db /path/to/state.db`; it prints counts only. See [docs/CASE_STUDY.md](docs/CASE_STUDY.md) for the recorded local result and evidence boundary.

## License and provenance

MIT. See [LICENSE](LICENSE).

Hermes Agent is MIT licensed, Copyright (c) 2025 Nous Research. These patches are a derivative work and quote small portions of that source for context. They are an independent contribution and are not affiliated with or endorsed by Nous Research.
