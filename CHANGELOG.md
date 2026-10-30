# Changelog

All notable changes to the two installers are recorded here. The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html) as the README's "Versioning and releases" section applies it to the installer contract. Release headings use ISO dates (YYYY-MM-DD), as the format requires.

## [Unreleased]

### Added

- `--diff` on both installers: runs exactly what `--check` runs, prints the
  same first line, then the unified diff of the target against the bytes an
  install, upgrade or (with `--reverse`) uninstall would write. It writes
  nothing. A refusal prints what `--check` prints, with the same exit code.
  The diff is UTF-8 with LF line endings, so a CRLF target shows no carriage
  returns. Before, `--check` gave only a verdict, and a router upgrade could
  not be reviewed before it replaced the production file.
- `--status` on both installers: a read-only report that starts with
  `STATUS <state>` (`UNPATCHED`, `CURRENT`, `PREVIOUS`, `EDITED` for the
  router, `MALFORMED` or `UNRECOGNIZED`), then `key: value` lines naming the
  installer version, the target's sha256, the installed generation, whether
  the unpatched file it would restore is the pinned revision d7b36070's, the
  use of each recovery slot family, and every recovery copy with its sha256.
  It writes nothing. Before, operators compared and hashed recovery copies by
  hand, learned that slots were full only when a write aborted, and saw only
  `found 0` on a newer Hermes.
- `--reverse` on the router installer removes a block from an earlier release
  (v1.0.0, or a build installed from main before tagging began) in one step,
  together with the patched hook, and keeps any code between that block and
  the busy handler. It used to refuse every block but the current one, so an
  older install had to be upgraded before it could be uninstalled. An edited
  or unknown block still aborts.

### Changed

- In `independent` mode the router now runs two self-contained question
  shapes in parallel that it used to queue: an ordinal before a calendar or
  legal noun (`the 18th century`, `the 14th amendment`, `the 16th president`,
  `a 50th wedding anniversary`) and `also known as`, `also called`,
  `also named` or `also referred to`. A list pick (`the 2nd one`), a rank
  (`3rd place`), an edition, `earlier`, and a leading `also` stay queued.
  `off` and `all` are unchanged.
- The router's injected block changed, so an existing router install reports
  `UPGRADE_APPLICABLE` once and keeps the block it replaces in one `.upgrade`
  recovery slot. This batches the two acknowledgment fixes below and the
  classifier change above into a single upgrade.
- Python 3.15 is a blocking CI job on Linux and Windows. Its final release
  is out, which is when the workflow said the non-blocking job would be
  promoted.

### Fixed

- A router upgrade no longer deletes code it does not own. It replaced
  everything from the injected block marker to the busy-handler anchor, so a
  method between an older router block and `_handle_active_session_busy_message`
  (an operator's, another patch's, or an upstream merge's) was removed while
  the installer printed `PATCHED_OK`; only the `.upgrade` copy kept it. Every
  block that shipped on main is now listed by length and hash, replaced
  exactly, and whatever follows it is kept. An unknown region is still
  upgraded when all of it is router code. When it holds anything else, the
  upgrade aborts with exit 2, names the first such statement and its line,
  and changes nothing.
- Repeat install and uninstall cycles no longer dead-end on a recovery copy.
  Applying, reversing, updating Hermes (or editing the file) and applying
  again aborted with exit 3, `existing recovery backup differs`, and so did a
  second reverse after a reapply. The original backup and `.reverse` copies
  now take slots the way upgrade copies already did: the name, then `.2` up to
  `.8`. An identical copy is reused, a different one is kept, and a write that
  finds all eight slots taken aborts with `no free recovery slot`, exit 3.
  Nothing is ever overwritten, and a slot that is not a regular file still
  aborts. `--check` resolves the same slot as the write.
- A failed diagnostic log after an acknowledgment error or timeout no longer
  drops an already accepted parallel follow-up. The background run still starts
  exactly once, and acknowledgment cleanup and capacity limits are unchanged.
- An acknowledgment that ends cancelled when its owning task was not
  cancelled (the adapter's send raised `CancelledError`, or another task
  cancelled it) is now treated as a failed send, so the accepted parallel
  follow-up still runs once. It used to end the owning task as cancelled,
  which dropped the follow-up without a warning. Cancelling the owning task
  still prevents the run.

## [1.0.0] - 2026-10-09

First tagged release. Earlier work, up to pull request #52, shipped untagged from main. This release declares the installer contract the README documents (flags, result tokens, exit codes, recovery copy names, and the `display.busy_overflow_*` config keys) as the public API. Compatibility with Hermes stays pinned to revision d7b36070.

### Added

- `--version` on both installers, and a `--help` that lists every result token and the exit codes instead of reflowing the module docstring.
- A release workflow. A pushed `vX.Y.Z` tag is verified (tag, `VERSION`, installers and changelog agree; offline suite; pinned-source gate) and then published as a GitHub Release with both installers and `SHA256SUMS`. The job that holds the write token runs no upstream code.
- `VERSION` as the single version source, and `tests/check_version.py`, which keeps it, both installers and this changelog in agreement.
- `tests/fetch_pinned_source.py`, which downloads and verifies the pinned upstream fixture, for CI and for local reproduction.
- A static upstream contract check in the pinned-source gate. It covers `gateway/authz_mixin.py`, where `GatewayRunner` inherits `_adapter_for_source`, the bound busy handler the debounce flush reaches the runner through, the `_text_debounce` store, and the signatures of the calls the router makes after dispatch.
- One warning per distinct invalid `busy_overflow_max_per_session` or `busy_overflow_max_total` value, and documented ranges, defaults and mode aliases for all three router keys.
- `HERMES_ROOT` for the installed-Hermes checks, which now exit 2 with `REQUIRES_HERMES` instead of an import traceback when no Hermes is found.
- Installer parity and derived-precondition tests, so the two standalone installers cannot drift apart and their required names follow the injected code.
- Dependabot updates for the pinned workflow actions, a non-blocking Python 3.15 CI job, and `.gitattributes` to keep LF line endings.

### Changed

- The classifier now queues first-person back-references (`what did I say`, `did I mention`), the assistant's past actions (`the figure you quoted`, `the dataset you loaded`), and references to the assistant's own output (`your answer`, `the script`, `the table`), which were answered cold before. Existing router installs report `UPGRADE_APPLICABLE` once and keep the previous block in one `.upgrade` recovery slot.
- The router installer no longer requires `import os` in `run.py`, and accepts a name bound by a tuple assignment, as the debounce installer already did.
- CI runs once per change, on pull requests and on pushes to main, and no longer on tag pushes.
- The pinned fixture is three files: `base.py`, `run.py` and `authz_mixin.py`.

### Fixed

- A target that starts with a UTF-8 byte order mark no longer bypasses the runtime-symbol checks. Such a file that does not bind a required name is now refused instead of patched.
- Tests read and write text as UTF-8 instead of the locale encoding, and the offline runner fails on new text I/O without an explicit encoding.
- A class missing from the pinned fixture is reported as `VALIDATION_FAILED` naming the pin, not as a traceback.
- The case study's stale and contradictory evidence, and the README's claim of unlimited upgrade generations: there are eight.

[Unreleased]: https://github.com/EauDoon/hermes-parallel-followups/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/EauDoon/hermes-parallel-followups/releases/tag/v1.0.0
