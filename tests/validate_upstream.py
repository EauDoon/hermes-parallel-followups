#!/usr/bin/env python3
"""Validate a pinned public source snapshot using disposable copies and no imports.

Provide a directory containing the original base.py, run.py and authz_mixin.py
from PIN below. Nothing is downloaded by this command, and the source directory
is read-only. A static contract check first confirms every upstream member the
patches call. Selected real FIFO/debounce methods then execute with synthetic
events and no I/O. This is not a full Hermes startup or live-model integration
test.
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
    "authz_mixin.py": "bfe908efbe0504d3803571195cee92ac6717d9c5a0eda81e79549f8bff61b11f",
}
# Where each fixture file lives in the upstream repository at PIN.
SOURCES = {
    "base.py": "gateway/platforms/base.py",
    "run.py": "gateway/run.py",
    "authz_mixin.py": "gateway/authz_mixin.py",
}
ROOT = Path(__file__).resolve().parents[1]
FIXTURE_FILES = ", ".join(HASHES)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def class_named(tree, class_name, filename):
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return node
    raise ValueError(f"{filename}: class {class_name} is missing; the patches are verified only "
                     f"against {PIN}")


def selected(source, class_name, methods, filename="fixture"):
    original = class_named(ast.parse(source), class_name, filename)
    body = [node for node in original.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in methods]
    require({node.name for node in body} == set(methods), "Pinned method selection incomplete")
    definition = ast.parse(f"class {class_name}: pass").body[0]
    definition.body = body
    return definition


# --- Static contract -------------------------------------------------------
# The lifecycle below executes only the FIFO and debounce methods. The patches
# also call other upstream members, and reach the runner through wiring the
# lifecycle stubs. If any of these drifts, the router loses a dispatched
# follow-up, or the debounce flush silently falls back to the newline merge.
# Each entry: member, positional arguments, keyword names, and who calls it.
ADAPTER_CALLS = (
    ("_send_with_retry", 0, ("chat_id", "content", "reply_to", "metadata"), "the router acknowledgment"),
    ("set_busy_session_handler", 1, (), "the runner wiring the debounce flush uses"),
)
RUNNER_CALLS = (
    ("_handle_active_session_busy_message", 2, (), "the adapter busy handler"),
    ("_run_background_task", 3, ("event_message_id",), "the router dispatch"),
    ("_thread_metadata_for_source", 2, (), "the router acknowledgment"),
    ("_reply_anchor_for_event", 1, (), "the router dispatch"),
    ("_queue_depth", 1, ("adapter",), "the router and the debounce flush"),
    ("_queue_or_replace_pending_event", 2, (), "the debounce flush"),
)
MIXIN_CALLS = (
    ("_adapter_for_source", 1, (), "the router and the debounce flush"),
)


def method_named(cls, name, filename):
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise ValueError(f"{filename}: {cls.name}.{name} is missing; the patches are verified only "
                     f"against {PIN}")


def is_staticmethod(function):
    return any(isinstance(decorator, ast.Name) and decorator.id == "staticmethod"
               for decorator in function.decorator_list)


def call_mismatch(function, positional, keywords):
    """Why calling the bound method this way would raise TypeError, or None."""
    arguments = function.args
    parameters = arguments.posonlyargs + arguments.args
    first_default = len(parameters) - len(arguments.defaults)
    names = [parameter.arg for parameter in parameters]
    optional = {name for index, name in enumerate(names) if index >= first_default}
    positional_only = len(arguments.posonlyargs)
    if not is_staticmethod(function):
        if not names:
            return "takes no self parameter"
        names = names[1:]
        positional_only = max(0, positional_only - 1)
    if positional > len(names) and arguments.vararg is None:
        return f"takes {len(names)} positional arguments where the patch passes {positional}"
    supplied = set(names[:positional])
    keyword_only = {parameter.arg: default is not None
                    for parameter, default in zip(arguments.kwonlyargs, arguments.kw_defaults)}
    for keyword in keywords:
        if keyword in supplied:
            return f"would receive {keyword} twice"
        if keyword in names[positional_only:] or keyword in keyword_only:
            supplied.add(keyword)
        elif arguments.kwarg is None:
            return f"has no parameter {keyword}"
    missing = [name for name in names if name not in optional and name not in supplied]
    missing += [name for name, has_default in keyword_only.items()
                if not has_default and name not in supplied]
    if missing:
        return "requires " + ", ".join(missing) + ", which the patch does not pass"
    return None


def require_calls(cls, calls, filename):
    for name, positional, keywords, caller in calls:
        problem = call_mismatch(method_named(cls, name, filename), positional, keywords)
        require(problem is None, f"{filename}: {cls.name}.{name} {problem}; {caller} relies on it "
                                 f"(verified only against {PIN})")


def is_self_attribute(node, attribute):
    return (isinstance(node, ast.Attribute) and node.attr == attribute
            and isinstance(node.value, ast.Name) and node.value.id == "self")


def assigned_values(scope, attribute):
    """Values assigned to self.<attribute> anywhere in scope (Assign or AnnAssign)."""
    values = []
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign):
            values.extend(node.value for target in node.targets if is_self_attribute(target, attribute))
        elif isinstance(node, ast.AnnAssign) and node.value is not None and is_self_attribute(node.target, attribute):
            values.append(node.value)
    return values


def require_assigned(cls, attribute, caller, filename):
    require(assigned_values(cls, attribute),
            f"{filename}: {cls.name} no longer assigns self.{attribute}; {caller} relies on it "
            f"(verified only against {PIN})")


def module_statements(body):
    """Statements that run at module scope, including inside if, try and with."""
    for node in body:
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for field in ("body", "orelse", "finalbody"):
            yield from module_statements(getattr(node, field, None) or [])
        for handler in getattr(node, "handlers", None) or []:
            yield from module_statements(handler.body)


def mixin_binding(tree):
    """The name run.py binds GatewayAuthorizationMixin to at module scope, or None."""
    for node in module_statements(tree.body):
        if isinstance(node, ast.ImportFrom) and node.module == "gateway.authz_mixin" and node.level == 0:
            for alias in node.names:
                if alias.name == "GatewayAuthorizationMixin":
                    return alias.asname or alias.name
    return None


def verify_contract(base, run, authz):
    """Statically confirm every upstream member the patches call. Executes nothing."""
    base_tree, run_tree, authz_tree = ast.parse(base), ast.parse(run), ast.parse(authz)

    adapter = class_named(base_tree, "BasePlatformAdapter", "base.py")
    require_calls(adapter, ADAPTER_CALLS, "base.py")
    # The debounce flush reaches the runner as _busy_session_handler.__self__,
    # so the handler has to be stored exactly as it was passed in.
    setter = method_named(adapter, "set_busy_session_handler", "base.py")
    setter_parameters = [parameter.arg for parameter in setter.args.posonlyargs + setter.args.args]
    require(len(setter_parameters) == 2
            and any(isinstance(value, ast.Name) and value.id == setter_parameters[1]
                    for value in assigned_values(setter, "_busy_session_handler")),
            f"base.py: BasePlatformAdapter.set_busy_session_handler no longer stores its handler "
            f"as self._busy_session_handler; the debounce flush relies on it (verified only against {PIN})")
    require_assigned(adapter, "_text_debounce", "the router's debounce-buffer count", "base.py")

    runner = class_named(run_tree, "GatewayRunner", "run.py")
    require_calls(runner, RUNNER_CALLS, "run.py")
    require_assigned(runner, "_background_tasks", "the router dispatch", "run.py")
    wiring = [node for node in ast.walk(run_tree) if isinstance(node, ast.Call)
              and isinstance(node.func, ast.Attribute) and node.func.attr == "set_busy_session_handler"]
    require(wiring, f"run.py: nothing calls set_busy_session_handler; the debounce flush relies on "
                    f"that wiring (verified only against {PIN})")
    for call in wiring:
        require(len(call.args) == 1 and not call.keywords
                and is_self_attribute(call.args[0], "_handle_active_session_busy_message"),
                f"run.py: line {call.lineno} passes set_busy_session_handler something other than "
                f"self._handle_active_session_busy_message, so _busy_session_handler.__self__ may not "
                f"be the runner the debounce flush needs (verified only against {PIN})")

    # _adapter_for_source is inherited. It resolves to the mixin only when
    # the runner does not define it and the mixin comes first in its bases.
    mixin = class_named(authz_tree, "GatewayAuthorizationMixin", "authz_mixin.py")
    if any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_adapter_for_source"
           for node in runner.body):
        require_calls(runner, MIXIN_CALLS, "run.py")
    else:
        bound_as = mixin_binding(run_tree)
        first_base = runner.bases[0] if runner.bases else None
        require(bound_as is not None and isinstance(first_base, ast.Name) and first_base.id == bound_as,
                f"run.py: GatewayRunner does not inherit _adapter_for_source from "
                f"GatewayAuthorizationMixin (imported from gateway.authz_mixin as its first base); "
                f"the router and the debounce flush rely on it (verified only against {PIN})")
        require_calls(mixin, MIXIN_CALLS, "authz_mixin.py")


def load_real_fifo(base, runner):
    base_tree = ast.parse(base)
    globals_needed = {"MessageType", "merge_pending_message_event", "_platform_name"}
    body = [node for node in base_tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef))
            and node.name in globals_needed]
    require({node.name for node in body} == globals_needed, "Pinned base symbols incomplete")
    body.append(selected(base, "BasePlatformAdapter", {
        "_text_debounce_store", "_can_merge_text_debounce_events", "_flush_text_debounce_now"}, "base.py"))
    body.append(selected(runner, "GatewayRunner", {
        "_enqueue_fifo", "_queue_depth", "_queue_or_replace_pending_event"}, "run.py"))
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
    # The real method lives in authz_mixin.py. verify_contract checks its
    # signature and that GatewayRunner inherits it; this run only needs the
    # source to resolve back to the same adapter.
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    args = parser.parse_args(argv)
    require(args.source.is_dir(),
            f"{args.source} is not a directory; supply a directory holding "
            f"{FIXTURE_FILES} from {PIN}")
    originals = {}
    for name, digest in HASHES.items():
        path = args.source / name
        # The common mistakes are pointing at a checkout instead of the
        # files, or at the gateway/ tree. Name the revision and the layout the
        # command expects, so a refused run is actionable without reading this
        # file.
        require(path.is_file(),
                f"{path} is missing; supply {FIXTURE_FILES} from {PIN} directly, "
                f"without the gateway/ subdirectories")
        originals[name] = path.read_bytes()
        require(hashlib.sha256(originals[name]).hexdigest() == digest,
                f"{name} does not match supported public revision {PIN}")
    verify_contract(originals["base.py"], originals["run.py"], originals["authz_mixin.py"])
    print("PINNED_CONTRACT_OK: adapter, runner and mixin members the patches call")
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


def cli(argv=None):
    try:
        main(argv)
    except (OSError, ValueError) as error:
        print(f"VALIDATION_FAILED: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(cli())
