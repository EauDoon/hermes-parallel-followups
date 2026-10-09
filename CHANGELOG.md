# Changelog

All notable changes to the two installers are recorded here. The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html) as the README's "Versioning and releases" section applies it to the installer contract. Release headings use ISO dates (YYYY-MM-DD), as the format requires.

## [Unreleased]

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
