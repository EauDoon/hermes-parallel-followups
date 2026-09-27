#!/usr/bin/env python3
"""Run every dependency-free check without importing an installed Hermes gateway."""
import ast
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
CHECKS = (
    "test_classifier.py", "test_router.py", "test_router_lifecycle.py",
    "test_patch_installers.py", "test_patch_workflows.py", "test_transcript_scan.py",
)
# Documented in the README as separate from the offline suite because they
# import an installed Hermes at /opt/hermes. validate_upstream.py is not
# matched by the test_*.py glob; it needs a pinned upstream fixture.
NEEDS_INSTALLED_HERMES = ("test_debounce_fifo.py", "test_burst_fullpath.py")


def main():
    for directory in (ROOT / "patches", ROOT / "tests"):
        for source in directory.glob("*.py"):
            ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    # A new test file that nobody added to CHECKS would otherwise never run,
    # and CI stays green. A file may only be missing by being named above.
    uncollected = sorted(
        path.name for path in (ROOT / "tests").glob("test_*.py")
        if path.name not in CHECKS and path.name not in NEEDS_INSTALLED_HERMES
    )
    failures = ["%s is not collected by this runner" % name for name in uncollected]
    for name in uncollected:
        print(f"FAILED: {name} is not in CHECKS or NEEDS_INSTALLED_HERMES", flush=True)
    for name in CHECKS:
        print(f"\nRunning {name}", flush=True)
        try:
            result = subprocess.run([sys.executable, str(ROOT / "tests" / name)],
                                    cwd=ROOT, check=False, timeout=60)
        except subprocess.TimeoutExpired:
            print(f"FAILED: {name} exceeded its 60-second deadline", flush=True)
            failures.append(name)
            continue
        if result.returncode:
            failures.append(name)
    print("\nOFFLINE_CHECKS_OK" if not failures else "FAILED: " + ", ".join(failures))
    return bool(failures)


if __name__ == "__main__":
    sys.exit(main())
