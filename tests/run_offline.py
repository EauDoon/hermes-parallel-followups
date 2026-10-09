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
    "test_validate_upstream.py", "test_debounce_flush.py",
)
# Documented in the README as separate from the offline suite because they
# import an installed Hermes at /opt/hermes. validate_upstream.py is not
# matched by the test_*.py glob; it needs a pinned upstream fixture.
NEEDS_INSTALLED_HERMES = ("test_debounce_fifo.py", "test_burst_fullpath.py")
# Positional index of the encoding argument, so a positional encoding counts.
TEXT_HELPERS = {"read_text": 0, "write_text": 1}


def unencoded_text_io(tree):
    """Line numbers of text I/O calls that leave the encoding to the locale.

    The locale encoding is UTF-8 on Linux and usually cp1252 on Windows
    runners, so a source read that way decodes differently per platform.
    Covered: Path.read_text and Path.write_text, and the builtin open or a
    method named open (not os.open, which takes flags) whose mode is absent or
    a text-mode string constant. A mode that is not a constant is not judged.
    """
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # A **mapping might carry the encoding, so it is not judged either.
        if any(keyword.arg in ("encoding", None) for keyword in node.keywords):
            continue
        function = node.func
        if isinstance(function, ast.Attribute) and function.attr in TEXT_HELPERS:
            if len(node.args) <= TEXT_HELPERS[function.attr]:
                lines.append(node.lineno)
            continue
        if isinstance(function, ast.Name) and function.id == "open":
            mode_index = 1
        elif (isinstance(function, ast.Attribute) and function.attr == "open"
              and not (isinstance(function.value, ast.Name) and function.value.id == "os")):
            mode_index = 0
        else:
            continue
        if len(node.args) > mode_index + 2:
            continue  # encoding passed positionally after mode and buffering
        mode = next((keyword.value for keyword in node.keywords if keyword.arg == "mode"), None)
        if mode is None and len(node.args) > mode_index:
            mode = node.args[mode_index]
        if mode is None or (isinstance(mode, ast.Constant) and isinstance(mode.value, str)
                            and "b" not in mode.value):
            lines.append(node.lineno)
    return lines


def main():
    unencoded = []
    for directory in (ROOT / "patches", ROOT / "tests"):
        for source in sorted(directory.glob("*.py")):
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            name = source.relative_to(ROOT).as_posix()
            unencoded.extend("%s:%d" % (name, line) for line in unencoded_text_io(tree))
    # A new test file that nobody added to CHECKS would otherwise never run,
    # and CI stays green. A file may only be missing by being named above.
    uncollected = sorted(
        path.name for path in (ROOT / "tests").glob("test_*.py")
        if path.name not in CHECKS and path.name not in NEEDS_INSTALLED_HERMES
    )
    failures = ["%s is not collected by this runner" % name for name in uncollected]
    for name in uncollected:
        print(f"FAILED: {name} is not in CHECKS or NEEDS_INSTALLED_HERMES", flush=True)
    for location in unencoded:
        print(f"FAILED: {location} text I/O without encoding=", flush=True)
        failures.append(f"{location} text I/O without encoding=")
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
