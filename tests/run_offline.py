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


def main():
    for directory in (ROOT / "patches", ROOT / "tests"):
        for source in directory.glob("*.py"):
            ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    failures = []
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
