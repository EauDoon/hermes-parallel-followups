#!/usr/bin/env python3
"""Validate a pinned public source snapshot using disposable copies and no imports.

Provide a directory containing original base.py and run.py from PIN below.
Nothing is downloaded by this command, and the source directory is read-only.
Selected real FIFO/debounce methods execute with synthetic events and no I/O.
This is not a full Hermes startup or live-model integration test.
"""
import argparse
import ast
import asyncio
from enum import Enum
import hashlib
import logging
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace


PIN = "d7b36070ef807841699ad32c5b6af547fee3ff64"
HASHES = {
    "base.py": "6bfdf20de31ae01fbd088457b91252d2430f9bc45d0a84ba132590be54fc909f",
    "run.py": "36429599eefc193ba6b33c077d0f92b3933f1173c8577b9ac61c3767dddbda89",
}
ROOT = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def selected(source, class_name, methods):
    tree = ast.parse(source)
    original = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    body = [node for node in original.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in methods]
    require({node.name for node in body} == set(methods), "Pinned method selection incomplete")
    definition = ast.parse(f"class {class_name}: pass").body[0]
    definition.body = body
    return definition


def load_real_fifo(base, runner):
    base_tree = ast.parse(base)
    globals_needed = {"MessageType", "merge_pending_message_event", "_platform_name"}
    body = [node for node in base_tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef))
            and node.name in globals_needed]
    require({node.name for node in body} == globals_needed, "Pinned base symbols incomplete")
    body.append(selected(base, "BasePlatformAdapter", {
        "_text_debounce_store", "_can_merge_text_debounce_events", "_flush_text_debounce_now"}))
    body.append(selected(runner, "GatewayRunner", {
        "_enqueue_fifo", "_queue_depth", "_queue_or_replace_pending_event"}))
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, *body], type_ignores=[]))
    namespace = {"asyncio": asyncio, "Enum": Enum, "logger": logging.getLogger("fixture")}
    exec(compile(module, "<pinned public FIFO methods>", "exec"), namespace)
    return namespace


async def verify_fifo(base, runner_source):
    ns = load_real_fifo(base, runner_source)
    adapter = ns["BasePlatformAdapter"]()
    adapter.name = "fixture"
    adapter._text_debounce = {}
    adapter._pending_messages = {}
    runner = ns["GatewayRunner"]()
    runner._queued_events = {}
    runner._BUSY_QUEUE_MAX_PENDING = 32
    runner._adapter_for_source = lambda source: adapter
    async def busy_handler(self, event, key):
        return False
    adapter._busy_session_handler = busy_handler.__get__(runner)
    source = SimpleNamespace(platform="fixture", user_id="synthetic", chat_id="fixture", chat_type="dm")
    async def flush(index, key="session", task=None):
        event = SimpleNamespace(text=f"question-{index}", message_type=ns["MessageType"].TEXT,
                                source=source, media_urls=[], media_types=[])
        adapter._text_debounce[key] = SimpleNamespace(event=event, task=task)
        require(await adapter._flush_text_debounce_now(key), "Flush declined")
    for index in range(10):
        await flush(index)
    queued = [adapter._pending_messages["session"], *runner._queued_events["session"]]
    require([event.text for event in queued] == [f"question-{index}" for index in range(10)],
            "Busy follow-ups lost boundaries or arrival order")
    require(not await adapter._flush_text_debounce_now("session"), "Empty flush repeated delivery")
    pending_timer = asyncio.create_task(asyncio.Event().wait())
    await flush("other", "other-session", pending_timer)
    await asyncio.gather(pending_timer, return_exceptions=True)
    require(pending_timer.cancelled(), "Flush did not cancel the pending debounce timer")
    require(runner._queue_depth("session", adapter=adapter) == 10, "Session queues leaked")
    for index in range(10, 36):
        await flush(index)
    queued = [adapter._pending_messages["session"], *runner._queued_events["session"]]
    delivered = [part for event in queued for part in event.text.splitlines()]
    require(len(queued) == 32 and sorted(delivered) == sorted(f"question-{index}" for index in range(36)),
            "Pending-cap fallback dropped or duplicated a message")
    print("PINNED_FIFO_OK: ordered bursts, isolation, empty flush, timer cancellation, cap fallback")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    require(args.source.is_dir(),
            f"{args.source} is not a directory; supply a directory holding "
            f"base.py and run.py from {PIN}")
    originals = {}
    for name, digest in HASHES.items():
        path = args.source / name
        # The common mistakes are pointing at a checkout instead of the two
        # files, or at the gateway/ tree. Name the revision and the layout the
        # command expects, so a refused run is actionable without reading this
        # file.
        require(path.is_file(),
                f"{path} is missing; supply base.py and run.py from {PIN} directly, "
                f"without the gateway/ subdirectories")
        originals[name] = path.read_bytes()
        require(hashlib.sha256(originals[name]).hexdigest() == digest,
                f"{name} does not match supported public revision {PIN}")
    with tempfile.TemporaryDirectory() as td:
        patched = {}
        for name, installer in (("base.py", "apply_debounce_fifo_patch.py"),
                                ("run.py", "apply_busy_overflow_router_patch.py")):
            target = Path(td) / name
            target.write_bytes(originals[name])
            for options, expected in ((("--check",), "APPLICABLE"), ((), "PATCHED_OK"),
                                      (("--check",), "ALREADY_PATCHED"),
                                      (("--check", "--reverse"), "REVERSIBLE"),
                                      (("--reverse",), "REVERSED_OK"), ((), "PATCHED_OK")):
                result = subprocess.run([sys.executable, str(ROOT / "patches" / installer), str(target), *options],
                                        capture_output=True, text=True, check=False)
                require(result.returncode == 0 and result.stdout.strip() == expected,
                        f"{name}: {result.stdout}{result.stderr}")
                if expected == "REVERSED_OK":
                    require(target.read_bytes() == originals[name], "Reverse changed original source bytes")
                print(f"{name}: {expected}")
            patched[name] = target.read_text(encoding="utf-8")
        asyncio.run(verify_fifo(patched["base.py"], patched["run.py"]))
    print(f"PINNED_LIFECYCLE_OK {PIN}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        print(f"VALIDATION_FAILED: {error}", file=sys.stderr)
        sys.exit(1)
