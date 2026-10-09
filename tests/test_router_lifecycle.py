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
BLOCK = next(ast.literal_eval(node.value) for node in ast.parse(SCRIPT.read_text(encoding="utf-8")).body
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
        done, _ = await asyncio.wait({task}, timeout=1.0)
        self.assertIn(task, done, "Cancellation was swallowed at acknowledgment completion")
        self.assertTrue(task.cancelled())
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

    async def test_registration_failure_cancels_the_task_and_does_not_generate(self):
        # create_task schedules the follow-up before it is recorded. If
        # registering it then raises, the caller queues the event too, and
        # the orphan keeps running.
        class Exploding(set):
            def add(self, item):
                raise RuntimeError("registry full")

        self.runner._background_tasks = Exploding()
        with self.assertRaises(RuntimeError):
            await self.runner._maybe_route_overflow_to_background(event(), "session")
        pending = asyncio.all_tasks() - {asyncio.current_task()}
        if pending:
            await asyncio.wait(pending, timeout=0.5)
        self.assertFalse(self.runner.prompts)
        self.assertFalse(self.runner._overflow_router_tasks)
        self.assertFalse(self.runner._background_tasks)

    async def test_task_factory_failure_leaves_no_owned_work(self):
        with patch("asyncio.create_task", side_effect=RuntimeError("closed")):
            with self.assertRaises(RuntimeError):
                await self.runner._maybe_route_overflow_to_background(event(), "session")
        self.assertFalse(self.runner._background_tasks)
        self.assertFalse(self.runner._overflow_router_tasks)

    async def test_reply_context_stays_queued_even_in_all_mode(self):
        CONFIG["busy_overflow_background"] = "all"
        quoted = event()
        quoted.reply_to_message_id = "earlier-message"
        self.assertFalse(await self.runner._maybe_route_overflow_to_background(quoted, "session"))

    async def test_nontext_classifier_input_is_conservative(self):
        for value in (None, 123, ["What is the capital of Mongolia?"]):
            self.assertFalse(self.runner._classify_busy_followup(value))

    async def test_ack_completion_racing_owner_cancellation_never_starts_generation(self):
        async def racing_send(**kwargs):
            owner = next(iter(self.runner._background_tasks))
            asyncio.get_running_loop().call_soon(owner.cancel)
        self.runner.adapter._send_with_retry = racing_send
        self.assertTrue(await self.runner._maybe_route_overflow_to_background(event(), "session"))
        owner = next(iter(self.runner._background_tasks))
        done, _ = await asyncio.wait({owner}, timeout=1.0)
        self.assertIn(owner, done, "Owner cancellation must not be swallowed")
        self.assertTrue(owner.cancelled())
        self.assertFalse(self.runner.prompts)
        self.assertFalse(self.runner._overflow_router_tasks)

    async def test_ack_timeout_cancels_child_and_runs_generation_once(self):
        canceled = asyncio.Event()
        async def blocked(**kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                canceled.set()
        self.runner.adapter._send_with_retry = blocked
        self.runner._OVR_ACK_TIMEOUT_SECONDS = 0.01
        self.runner.release.set()
        self.assertTrue(await self.runner._maybe_route_overflow_to_background(event(), "session"))
        owner = next(iter(self.runner._background_tasks))
        done, _ = await asyncio.wait({owner}, timeout=1.0)
        self.assertIn(owner, done)
        owner.result()
        self.assertTrue(canceled.is_set())
        self.assertEqual(len(self.runner.prompts), 1)
        self.assertFalse(self.runner._overflow_router_tasks)

    async def test_logger_failure_does_not_dispatch_and_queue(self):
        # The platform caller queues the event when the busy handler raises.
        # logger.info used to run after the task was registered, so a missing
        # logger did both.
        saved = namespace.pop("logger")
        queued = []
        try:
            try:
                if await self.runner._maybe_route_overflow_to_background(event(), "session"):
                    return
            except Exception:
                queued.append("foreground")
        finally:
            namespace["logger"] = saved
        self.assertEqual(queued, ["foreground"])
        self.assertFalse(self.runner._background_tasks)
        self.assertFalse(getattr(self.runner, "_overflow_router_tasks", {}))

    async def test_cancellation_resistant_ack_keeps_ownership_until_it_finishes(self):
        canceled, release = asyncio.Event(), asyncio.Event()

        async def resistant(**kwargs):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                canceled.set()
                await release.wait()

        self.runner.adapter._send_with_retry = resistant
        self.runner._OVR_ACK_TIMEOUT_SECONDS = 0.01
        CONFIG["busy_overflow_max_per_session"] = 1
        self.assertTrue(await self.runner._maybe_route_overflow_to_background(event(), "session"))
        owner = next(iter(self.runner._background_tasks))
        try:
            await asyncio.wait_for(canceled.wait(), timeout=1.0)
            self.assertFalse(owner.done())
            self.assertFalse(self.runner.prompts)
            self.assertIn(owner, self.runner._overflow_router_tasks)
            self.assertFalse(await self.runner._maybe_route_overflow_to_background(event(), "session"))
        finally:
            # The deliberately resistant child always gets a bounded cleanup.
            release.set()
            self.runner.release.set()
            await asyncio.wait_for(asyncio.gather(owner, return_exceptions=True), timeout=1.0)
        owner.result()
        self.assertEqual(len(self.runner.prompts), 1)
        self.assertFalse(self.runner._background_tasks)
        self.assertFalse(self.runner._overflow_router_tasks)


if __name__ == "__main__":
    unittest.main()
