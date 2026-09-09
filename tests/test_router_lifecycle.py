"""Offline races and lifecycle checks against the exact injected source."""
import ast
import asyncio
import logging
from pathlib import Path
import re
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch


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

    async def test_total_cap_holds_across_concurrent_sessions(self):
        CONFIG["busy_overflow_max_total"] = 3
        results = await asyncio.gather(*[
            self.runner._maybe_route_overflow_to_background(event(), str(i)) for i in range(30)])
        self.assertEqual(sum(results), 3)
        task = next(iter(self.runner._background_tasks))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(await self.runner._maybe_route_overflow_to_background(event(), "new"))
        self.assertEqual(len(self.runner._overflow_router_tasks), 3)

    async def test_invalid_total_limit_disables_parallel_work(self):
        for value in (0, -1, 129, True, "8", None):
            CONFIG["busy_overflow_max_total"] = value
            self.assertFalse(await self.runner._maybe_route_overflow_to_background(event(), "session"))

    async def test_ack_failure_does_not_duplicate_or_drop_dispatch(self):
        async def fail(**kwargs):
            raise OSError("offline")
        self.runner.adapter._send_with_retry = fail
        self.assertTrue(await self.runner._maybe_route_overflow_to_background(event(), "session"))
        self.runner.release.set()
        await asyncio.gather(*list(self.runner._background_tasks))
        self.assertEqual(len(self.runner.prompts), 1)
        self.assertFalse(self.runner._overflow_router_tasks)

    async def test_cancel_during_ack_cancels_owned_task_and_releases_capacity(self):
        started = asyncio.Event()
        async def blocked(**kwargs):
            started.set()
            await asyncio.Event().wait()
        self.runner.adapter._send_with_retry = blocked
        self.assertTrue(await self.runner._maybe_route_overflow_to_background(event(), "session"))
        await started.wait()
        task = next(iter(self.runner._background_tasks))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertFalse(self.runner.prompts)
        self.assertFalse(self.runner._overflow_router_tasks)
        self.assertFalse(self.runner._background_tasks)

    async def test_failed_task_is_observed_and_capacity_released(self):
        async def fail(*args, **kwargs):
            raise RuntimeError("failure")
        self.runner._run_background_task = fail
        with self.assertLogs(namespace["logger"], level="WARNING") as logs:
            self.assertTrue(await self.runner._maybe_route_overflow_to_background(event(), "session"))
            await asyncio.gather(*list(self.runner._background_tasks), return_exceptions=True)
        self.assertIn("RuntimeError", logs.output[0])
        self.assertFalse(self.runner._overflow_router_tasks)

    async def test_task_factory_failure_leaves_no_owned_work(self):
        with patch("asyncio.create_task", side_effect=RuntimeError("closed")):
            with self.assertRaises(RuntimeError):
                await self.runner._maybe_route_overflow_to_background(event(), "session")
        self.assertFalse(self.runner._background_tasks)
        self.assertFalse(self.runner._overflow_router_tasks)


if __name__ == "__main__":
    unittest.main()
