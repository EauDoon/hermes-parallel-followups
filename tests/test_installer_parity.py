#!/usr/bin/env python3
"""The two installers are standalone copies of one design. Keep them in step.

Each script has to stay a single drop-in file, so the shared helpers are
duplicated rather than imported. These checks fail when one copy drifts from
the other, and when the injected code starts to load a module-level name that
the installer's precondition list does not require. Such a name would pass
every install gate and then raise NameError at runtime, after the follow-up
had already left the queue it came from.
"""
import ast
import builtins
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_patch_installers import ROOT, string_constants, unpatched_source


ROUTER = ROOT / "patches" / "apply_busy_overflow_router_patch.py"
DEBOUNCE = ROOT / "patches" / "apply_debounce_fifo_patch.py"
SHARED_HELPERS = (
    "checked_read", "guard_target", "parses", "recovery_copy_conflict",
    "write_backup_exclusive", "_target_binds", "_binds",
)
TAIL_TARGETS = ["candidate", "bytecode", "backup_path"]


def module(path):
    return ast.parse(path.read_text(encoding="utf-8"))


def functions(tree):
    return {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}


def tuple_constants(tree):
    """Module-level tuple assignments, such as REQUIRED_RUNTIME."""
    return {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign) and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name) and isinstance(node.value, ast.Tuple)
    }


class _ClearRecoveryPathArguments(ast.NodeTransformer):
    """The router's recovery_path() takes no argument; the debounce one does."""

    def visit_Call(self, node):
        self.generic_visit(node)
        if isinstance(node.func, ast.Name) and node.func.id == "recovery_path":
            node.args, node.keywords = [], []
        return node


def staging_tail(tree):
    """Module statements from `candidate = bytecode = backup_path = None` on."""
    for index, node in enumerate(tree.body):
        if (isinstance(node, ast.Assign)
                and [getattr(target, "id", None) for target in node.targets] == TAIL_TARGETS):
            tail = ast.Module(body=tree.body[index:], type_ignores=[])
            return [ast.dump(statement) for statement in _ClearRecoveryPathArguments().visit(tail).body]
    raise AssertionError("staging tail not found")


def free_names(tree):
    """Names loaded in ``tree`` that nothing inside it binds, minus builtins.

    Scope is ignored on purpose: a name stored anywhere in the snippet counts
    as bound everywhere in it. That can only hide a free name, never invent
    one, so a mismatch below is always real.
    """
    loaded, bound = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            (loaded if isinstance(node.ctx, ast.Load) else bound).add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            bound.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
    return loaded - bound - set(dir(builtins))


def router_injected_names(constants):
    """Module-level names the injected router block and hook load."""
    block = free_names(ast.parse("class _T:\n" + constants["BLOCK"]))
    # The hook runs inside the busy handler that ANCHOR opens, so that
    # method's parameters are bound there. Names the unpatched hook already
    # loads exist in any target the hook anchor matched.
    handler = ast.parse(constants["ANCHOR"].strip() + "\n    pass\n").body[0]
    wrapper = "async def _f(%s):\n" % ", ".join(argument.arg for argument in handler.args.args)
    hook = (free_names(ast.parse(wrapper + constants["HOOK_NEW"]))
            - free_names(ast.parse(wrapper + constants["HOOK_OLD"])))
    return block | hook


def debounce_injected_names(constants):
    """Names the injected flush body loads that the replaced body did not."""
    return (free_names(ast.parse("def _f():\n" + constants["NEW"]))
            - free_names(ast.parse("def _f():\n" + constants["OLD"])))


def run_installer(script, target, directory):
    return subprocess.run(
        [sys.executable, str(script), str(target)],
        check=False, capture_output=True, text=True,
        env={**os.environ, "PYTHONPYCACHEPREFIX": str(directory / "pycache")},
    )


class InstallerParityTests(unittest.TestCase):
    def test_shared_helpers_are_identical(self):
        router, debounce = functions(module(ROUTER)), functions(module(DEBOUNCE))
        for name in SHARED_HELPERS:
            with self.subTest(helper=name):
                self.assertIn(name, router)
                self.assertIn(name, debounce)
                self.assertEqual(ast.dump(router[name]), ast.dump(debounce[name]),
                                 "%s differs between the installers" % name)

    def test_staging_and_replacement_tails_are_identical(self):
        self.assertEqual(staging_tail(module(ROUTER)), staging_tail(module(DEBOUNCE)))

    def test_router_preconditions_are_the_names_its_injected_code_loads(self):
        required = tuple_constants(module(ROUTER))
        self.assertEqual(
            router_injected_names(string_constants(ROUTER)),
            set(required["REQUIRED_IMPORTS"] + required["REQUIRED_RUNTIME"]),
        )

    def test_debounce_preconditions_are_the_names_its_injected_code_loads(self):
        self.assertEqual(
            debounce_injected_names(string_constants(DEBOUNCE)),
            set(tuple_constants(module(DEBOUNCE))["REQUIRED_RUNTIME"]),
        )

    def test_a_new_name_in_the_injected_block_is_caught(self):
        # The derivation above must notice a module-level name added to the
        # block, or the two equality checks prove nothing.
        constants = string_constants(ROUTER)
        anchor = "        import secrets\n"
        self.assertEqual(constants["BLOCK"].count(anchor), 1)
        constants["BLOCK"] = constants["BLOCK"].replace(anchor, anchor + "        os.getpid()\n", 1)
        self.assertIn("os", router_injected_names(constants))

    @unittest.skipUnless(hasattr(ast, "TryStar"), "except* needs Python 3.11 or newer")
    def test_bindings_inside_except_star_count(self):
        source = "try:\n    import re\nexcept* OSError:\n    logger = None\n"
        for script in (ROUTER, DEBOUNCE):
            namespace = {"ast": ast}
            helpers = [functions(module(script))[name] for name in ("_target_binds", "_binds")]
            exec(compile(ast.Module(body=helpers, type_ignores=[]), str(script), "exec"), namespace)
            body = ast.parse(source).body
            with self.subTest(installer=script.name):
                self.assertTrue(namespace["_binds"](body, "re"))
                self.assertTrue(namespace["_binds"](body, "logger"))
                self.assertFalse(namespace["_binds"](body, "time"))

    def test_both_installers_accept_what_they_both_bind(self):
        # The router accepted only a plain name target, so a tuple-assigned
        # logger that the debounce installer accepted was refused. It also
        # required `import os`, which its injected code never uses.
        router = unpatched_source(
            string_constants(ROUTER), "HOOK_OLD", "_maybe_route_overflow_to_background")
        debounce = unpatched_source(
            string_constants(DEBOUNCE), "OLD", "_queue_or_replace_pending_event")
        cases = {
            "router-tuple-logger": (ROUTER, router.replace(
                "logger = logging.getLogger('fixture')",
                "logger, _unused = logging.getLogger('x'), None", 1)),
            "router-without-os": (ROUTER, router.replace("\nimport os\n", "\n", 1)),
            "debounce-tuple-logger": (DEBOUNCE, debounce.replace(
                "logger = logging.getLogger(__name__)",
                "logger, _unused = logging.getLogger('x'), None", 1)),
        }
        for name, (script, source) in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as td:
                directory = Path(td)
                self.assertNotEqual(source, router if script == ROUTER else debounce)
                target = directory / "target.py"
                target.write_text(source, encoding="utf-8")
                result = run_installer(script, target, directory)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stdout.strip(), "PATCHED_OK")


if __name__ == "__main__":
    unittest.main()
