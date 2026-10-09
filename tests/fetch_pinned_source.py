#!/usr/bin/env python3
"""Download the pinned public Hermes source files and verify them before use.

Each file in validate_upstream.SOURCES is fetched from the public GitHub
contents API at validate_upstream.PIN, without credentials. Every file must
match its recorded sha256 before anything is written, so nothing unverified
reaches disk or is executed. Only HTTP 429 is retried.

    python3 tests/fetch_pinned_source.py ./upstream-fixture
    python3 tests/validate_upstream.py ./upstream-fixture

or, in one step, the same command the pinned-source CI job runs:

    python3 tests/fetch_pinned_source.py ./upstream-fixture --validate
"""
import argparse
import hashlib
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request

import validate_upstream


URL = "https://api.github.com/repos/NousResearch/hermes-agent/contents/{path}?ref={pin}"
MAX_BYTES = 5_000_000
ATTEMPTS = 3
VALIDATOR = Path(__file__).resolve().with_name("validate_upstream.py")


def download(name):
    """Return the verified bytes of one fixture file, or raise."""
    url = URL.format(path=validate_upstream.SOURCES[name], pin=validate_upstream.PIN)
    # No Authorization header: the pinned-source job executes the verified
    # code, so it must not hold a token.
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github.raw+json"})
    for attempt in range(ATTEMPTS):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read(MAX_BYTES + 1)
            break
        except urllib.error.HTTPError as error:
            if error.code != 429 or attempt == ATTEMPTS - 1:
                raise
            try:
                delay = int((error.headers or {}).get("Retry-After", ""))
            except ValueError:
                delay = 5 * (attempt + 1)
            time.sleep(min(10, max(1, delay)))
    if len(raw) > MAX_BYTES or hashlib.sha256(raw).hexdigest() != validate_upstream.HASHES[name]:
        raise SystemExit(f"Pinned source hash mismatch: {name}")
    return raw


def fetch(directory):
    """Download and verify every fixture file, then write them all."""
    verified = {name: download(name) for name in validate_upstream.SOURCES}
    directory.mkdir(parents=True, exist_ok=True)
    for name, raw in verified.items():
        (directory / name).write_bytes(raw)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n", 1)[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("directory", type=Path,
                        help="where to write the verified files (created if missing)")
    parser.add_argument("--validate", action="store_true",
                        help="then run tests/validate_upstream.py on that directory")
    args = parser.parse_args(argv)
    fetch(args.directory)
    print(f"PINNED_SOURCE_OK {validate_upstream.PIN}", flush=True)
    if args.validate:
        subprocess.run([sys.executable, str(VALIDATOR), str(args.directory)], check=True, timeout=90)


if __name__ == "__main__":
    main()
