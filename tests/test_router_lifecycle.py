"""Offline races and lifecycle checks against the exact injected source."""
import ast
import asyncio
import logging
from pathlib import Path
import re
import time
import unittest
from types import SimpleNamespace


SCRIPT = Path(__file__).resolve().parents[1] / "patches/apply_busy_overflow_router_patch.py"
BLOCK = next(ast.literal_eval(node.value) for node in ast.parse(SCRIPT.read_text()).body
             if isinstance(node, ast.Assign)
             and any(isinstance(target, ast.Name) and target.id == "BLOCK" for target in node.targets))
CONFIG = {"busy_overflow_background": "independent"}


def cfg_get(config, section, key, default=None):
    return config.get(section, {}).get(key, default)


namespace = {"re": re, "asyncio": asyncio, "time": time,
             "logger": logging.getLogger(__name__), "cfg_get": cfg_get,
             "_load_gateway_runtime_config": lambda: {"display": CONFIG}}
exec("class Router:\n" + BLOCK, namespace)


class Runner(namespace["Router"]):
    def __init__(self):
        self._background_tasks = set()
        self.release = asyncio.Event()
        self.prompts = []
        self.acks = []
        self.adapter = SimpleNamespace(_send_with_retry=self.send)

    async def send(self, **kwargs):
        self.acks.append(kwargs)

    def _adapter_for_source(self, source):
        return self.adapter

    def _queue_depth(self, key, adapter):
        return 1

    def _reply_anchor_for_event(self, event):
        return event.message_id

    def _thread_metadata_for_source(self, source, anchor):
        return {"thread_id": source.thread_id}

    async def _run_background_task(self, prompt, source, task_id, **kwargs):
        self.prompts.append((prompt, task_id))
        await self.release.wait()


def event(text="What are the principal benefits of solar energy?"):
    return SimpleNamespace(text=text, internal=False, media_urls=[],
                           source=SimpleNamespace(chat_id="chat", thread_id="topic"),
                           message_id="msg", is_command=lambda: False)


class RouterLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        CONFIG.clear()
        CONFIG["busy_overflow_background"] = "independent"
        self.runner = Runner()

    async def asyncTearDown(self):
        self.runner.release.set()
        await asyncio.gather(*list(self.runner._background_tasks), return_exceptions=True)

    async def test_concurrent_handlers_obey_session_cap_and_release(self):
        results = await asyncio.gather(*[
            self.runner._maybe_route_overflow_to_background(event(), "session") for _ in range(20)])
        self.assertEqual(sum(results), 2)
        self.assertEqual(len(self.runner._overflow_router_tasks), 2)
        self.runner.release.set()
        await asyncio.gather(*list(self.runner._background_tasks))
        self.assertTrue(await self.runner._maybe_route_overflow_to_background(event(), "session"))
        self.assertEqual(len({task_id for _, task_id in self.runner.prompts}), len(self.runner.prompts))

    async def test_bad_limits_fail_closed(self):
        for value in (0, -1, 33, True, "2", None, 1.5):
            with self.subTest(value=value):
                CONFIG["busy_overflow_max_per_session"] = value
                self.assertFalse(await self.runner._maybe_route_overflow_to_background(event(), "session"))
        self.assertFalse(self.runner._background_tasks)

    async def test_other_sessions_have_independent_capacity(self):
        results = [await self.runner._maybe_route_overflow_to_background(event(), str(i)) for i in range(4)]
        self.assertEqual(results, [True] * 4)


if __name__ == "__main__":
    unittest.main()
