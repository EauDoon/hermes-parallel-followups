#!/usr/bin/env python3
"""Inspect both patch targets under an explicit Hermes checkout."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


PATCHES = Path(__file__).resolve().parents[1] / "patches"
TARGETS = (
    ("debounce", "gateway/platforms/base.py", "apply_debounce_fifo_patch.py"),
    ("router", "gateway/run.py", "apply_busy_overflow_router_patch.py"),
)
MODES = {
    "status": ("--status",),
    "install-check": ("--check",),
    "reverse-check": ("--reverse", "--check"),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="Hermes checkout containing gateway/")
    parser.add_argument("--mode", choices=MODES, default="status",
                        help="read layout (default) or check a proposed operation")
    parser.add_argument("--json", action="store_true", help="emit a report for automation")
    args = parser.parse_args()
    # The installers name the target path in their output. A console code
    # page that cannot encode it gets escapes, not a traceback and exit 1.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    results = []
    for name, relative, script in TARGETS:
        target = args.root.absolute() / relative
        proc = subprocess.run(
            [sys.executable, "-B", str(PATCHES / script), str(target), *MODES[args.mode]],
            capture_output=True, check=False, encoding="utf-8", errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        lines = proc.stdout.splitlines()
        result = {
            "patch": name, "target": relative, "exit_code": proc.returncode,
            "result": lines[0] if lines else "", "details": lines[1:],
        }
        if proc.stderr:
            result["stderr"] = proc.stderr.splitlines()
        results.append(result)
    if args.json:
        print(json.dumps({"schema_version": 1, "mode": args.mode, "targets": results}, indent=2))
    else:
        for item in results:
            print(f"[{item['patch']}] {item['target']} (exit {item['exit_code']})")
            print("\n".join([item["result"], *item["details"], *item.get("stderr", [])]))
    return max((item["exit_code"] if item["exit_code"] >= 0 else 1) for item in results)


if __name__ == "__main__":
    sys.exit(main())
