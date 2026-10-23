"""任务隔离、生命周期和两种 A2A 传输的离线测试，不向 QQ 发送消息。"""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import asyncio
import json
import unittest

import httpx

from plugins.maimai_agent_bridge.config import BridgeConfig, RemoteAgent
from plugins.maimai_agent_bridge.plugin import AgentBridgePlugin
from plugins.maimai_agent_bridge.remote import RemoteClient
from plugins.maimai_agent_bridge.service import BridgeService, Job


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory()
        self.config = BridgeConfig()
        self.config.access.allowed_users = ["owner"]
        self.notify = AsyncMock(return_value=True)
        self.service = BridgeService(self.config, Path(self.temporary.name), self.notify, Mock())
        await self.service.load()

    async def asyncTearDown(self):
        await self.service.close()
        self.temporary.cleanup()

    async def test_completion_persistence_and_origin(self):
        async def execute(job):
            job.result = "answer"
            job.thread_id = "test-thread"
        with patch.object(self.service, "_run_codex", execute):
            job = await self.service.submit("group-a", "owner", "question")
            await self.service.tasks[job.id]
        self.assertEqual(job.state, "completed")
        self.notify.assert_awaited_once_with(self.service.format_job(job), "group-a")
        for stream, user in [("group-b", "owner"), ("group-a", "another")]:
            with self.assertRaises(ValueError):
                self.service.require_scope(stream, user, job.id)
        other = BridgeService(self.config, self.service.data_dir, self.notify, Mock())
        await other.load()
        self.assertEqual(other.jobs[job.id].result, "answer")
        await other.close()

    async def test_failure_visible_and_does_not_report_success(self):
        with patch.object(self.service, "_run_codex", AsyncMock(side_effect=RuntimeError("upstream HTTP 400"))):
            job = await self.service.submit("group-a", "owner", "question")
            await self.service.tasks[job.id]
        self.assertEqual(job.state, "failed")
        self.assertIn("HTTP 400", job.error)
        self.assertIn("failed", self.notify.await_args.args[0])

    async def test_immediate_cancel_and_shutdown(self):
        self.service._run_codex = AsyncMock(side_effect=AssertionError("must not start"))
        job = await self.service.submit("group-a", "owner", "question")
        await self.service.cancel(job)
        self.assertEqual(job.state, "canceled")
        self.service._run_codex.assert_not_awaited()
        another = await self.service.submit("group-a", "owner", "question")
        await self.service.close()
        self.assertEqual(another.state, "interrupted")
        with self.assertRaises(RuntimeError):
            await self.service.submit("group-a", "owner", "question")

    async def test_concurrent_limit_and_queue_cancel(self):
        self.service.semaphore = asyncio.Semaphore(1)
        started = asyncio.Event()
        finish = asyncio.Event()
        async def execute(job):
            started.set()
            await finish.wait()
            job.result = "ok"
        with patch.object(self.service, "_run_codex", execute):
            first = await self.service.submit("group-a", "owner", "one")
            first_task = self.service.tasks[first.id]
            await started.wait()
            second = await self.service.submit("group-b", "owner", "two")
            await asyncio.sleep(0)
            self.assertEqual(second.state, "queued")
            await self.service.cancel(second)
            self.assertEqual(second.state, "canceled")
            finish.set()
            await first_task

    async def test_active_session_limit_is_per_qq_source(self):
        finish = asyncio.Event()

        async def execute(job):
            await finish.wait()

        with patch.object(self.service, "_run_codex", execute):
            jobs = [await self.service.submit("group-a", "owner", f"task-{index}") for index in range(3)]
            with self.assertRaisesRegex(ValueError, "最多同时保留 3 个活动 session") as error:
                await self.service.submit("group-a", "owner", "too-many")
            self.assertIn("/agent cancel", str(error.exception))
            other_source = await self.service.submit("group-b", "owner", "allowed")
            for job in [*jobs, other_source]:
                await self.service.cancel(job)

    async def test_refresh_releases_orphaned_persisted_session(self):
        job = Job("orphan", "group-a", "owner", "codex", "independent", "question", state="working")
        self.service.jobs[job.id] = job
        await self.service.save()
        active = await self.service.refresh_active_sessions("group-a", "owner")
        self.assertEqual(active, [])
        self.assertEqual(job.state, "interrupted")
        self.assertTrue(job.last_checked_at)
        other = BridgeService(self.config, self.service.data_dir, self.notify, Mock())
        await other.load()
        self.assertEqual(other.jobs[job.id].state, "interrupted")
        self.assertEqual(other.jobs[job.id].last_checked_at, job.last_checked_at)
        await other.close()

    async def test_restart_never_replays_active_jobs(self):
        job = Job("unfinished", "group-a", "owner", "codex", "independent", "question", state="working")
        self.service.jobs[job.id] = job
        await self.service.save()
        other = BridgeService(self.config, self.service.data_dir, self.notify, Mock())
        await other.load()
        self.assertEqual(other.jobs[job.id].state, "interrupted")
        self.assertFalse(other.tasks)
        await other.close()

    async def test_saved_list_filters_workspaces_before_exposing_titles(self):
        self.config.codex.allow_saved_threads = True
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.request.return_value = {"data": [
            {"id": "allowed-thread", "cwd": str(self.service.workspace), "preview": "allowed"},
            {"id": "private-thread", "cwd": str(self.service.data_dir.parent), "preview": "must not leak"},
        ]}
        with patch.object(self.service.sessions, "connect", return_value=client):
            result = await self.service.list_saved_threads()
        self.assertIn("allowed-thread", result)
        self.assertNotIn("private-thread", result)
        self.assertNotIn("must not leak", result)

    async def test_resumed_thread_permission_and_serialization(self):
        self.config.codex.allow_saved_threads = True
        self.service.bindings[self.service.binding_key("group-a", "owner")] = "saved-thread"
        finish = asyncio.Event()
        async def execute(job):
            await finish.wait()
        with patch.object(self.service, "_run_codex", execute):
            first = await self.service.submit("group-a", "owner", "one", mode="desktop")
            with self.assertRaises(ValueError):
                await self.service.submit("group-a", "owner", "two", mode="desktop")
            await self.service.cancel(first)
        self.config.codex.allow_saved_threads = False
        with self.assertRaises(ValueError):
            await self.service.submit("group-a", "owner", "two", previous_id=first.id)
        with self.assertRaises(ValueError):
            self.service.check_root(str(self.service.data_dir.parent))

    async def test_notifications_chunked_and_failure_retrievable(self):
        job = Job("result", "group-a", "owner", "codex", "independent", "question", result="x" * 6500)
        await self.service.deliver(job, job.result)
        self.assertEqual("".join(call.args[0] for call in self.notify.await_args_list), job.result)
        self.assertTrue(all(call.args[1] == "group-a" for call in self.notify.await_args_list))
        self.notify.return_value = False
        await self.service.deliver(job, "test")
        self.assertTrue(job.notification_error)
        self.assertEqual(len(job.result), 6500)

    async def test_approval_is_explicit_and_one_action_only(self):
        job = Job("approval", "group-a", "owner", "codex", "independent", "question", state="input_required")
        client = SimpleNamespace(send=AsyncMock())
        self.service.connections[job.id] = client
        self.service.questions[job.id] = {"id": 19, "method": "item/commandExecution/requestApproval"}
        with self.assertRaises(ValueError):
            await self.service.reply(job, "approve")
        await self.service.reply(job, "approve", approval=True)
        client.send.assert_awaited_once_with({"id": 19, "result": {"decision": "accept"}})
        self.assertEqual(job.state, "working")

    async def test_tool_rejects_spoofed_context_and_empty_allowlist(self):
        plugin = AgentBridgePlugin()
        plugin._plugin_config_instance = self.config
        plugin.service = self.service
        context = {"user_id": "owner", "stream_id": "group-a", "platform": "qq"}
        for bad in [{"user_id": "owner"}, {"stream_id": "group-b"}, {"action_data": {}}]:
            result = await plugin.agent_status(**context, action_data=bad)
            self.assertFalse(result["success"])
        result = await plugin.agent_status(**context, action_data={})
        self.assertTrue(result["success"])
        self.config.access.allowed_users = []
        result = await plugin.agent_status(**context, action_data={})
        self.assertFalse(result["success"])
        components = {c["name"]: c for c in plugin.get_components()}
        self.assertEqual(components["agent_submit"]["metadata"]["invoke_method"], "plugin.invoke_action")
        self.assertTrue(plugin.build_config_schema(plugin_id="local.maimai-agent-bridge"))


class RemoteTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_wire_formats_poll_and_cancel(self):
        for protocol in ["1.0-rest", "0.3-jsonrpc"]:
            with self.subTest(protocol=protocol):
                seen = []
                def respond(request, protocol=protocol, seen=seen):
                    seen.append(request)
                    body = json.loads(request.content) if request.content else {}
                    if protocol == "0.3-jsonrpc":
                        method = body["method"]
                        if method == "message/send":
                            self.assertEqual(body["params"]["message"]["role"], "user")
                        result = {"id": "remote-task", "contextId": "context", "status": {"state": "completed"}}
                        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})
                    self.assertEqual(request.headers["A2A-Version"], "1.0")
                    if request.url.path.endswith("message:send"):
                        self.assertEqual(body["message"]["role"], "ROLE_USER")
                        self.assertEqual(body["message"]["parts"], [{"text": "hello"}])
                        self.assertEqual(body["configuration"]["returnImmediately"], True)
                        self.assertNotIn("blocking", body["configuration"])
                    return httpx.Response(200, json={"task": {"id": "remote-task", "status": {"state": "TASK_STATE_COMPLETED"}}})
                client = RemoteClient(RemoteAgent(name="other", endpoint="https://agent.test", protocol=protocol))
                await client.client.aclose()
                headers = {"A2A-Version": "1.0"} if protocol == "1.0-rest" else {}
                client.client = httpx.AsyncClient(transport=httpx.MockTransport(respond), headers=headers)
                self.assertEqual((await client.send("hello"))["id"], "remote-task")
                self.assertEqual((await client.get("remote-task"))["id"], "remote-task")
                await client.cancel("remote-task")
                self.assertEqual(len(seen), 3)
                await client.close()

    async def test_remote_input_required_continuation(self):
        with TemporaryDirectory() as folder:
            config = BridgeConfig()
            config.remote.agents = [RemoteAgent(name="other", endpoint="https://agent.test")]
            need_input = asyncio.Event()
            async def notify(text, stream):
                need_input.set()
                return True
            service = BridgeService(config, Path(folder), notify, Mock())
            await service.load()
            remote = SimpleNamespace(
                send=AsyncMock(side_effect=[
                    {"id": "r1", "contextId": "c1", "status": {"state": "input-required", "message": {"parts": [{"text": "which?"}]}}},
                    {"id": "r1", "contextId": "c1", "status": {"state": "TASK_STATE_COMPLETED"}, "artifacts": [{"parts": [{"text": "done"}]}]},
                ]), close=AsyncMock(), cancel=AsyncMock(),
            )
            with patch("plugins.maimai_agent_bridge.service.RemoteClient", return_value=remote):
                job = await service.submit("group-a", "owner", "hello", agent="other")
                task = service.tasks[job.id]
                await asyncio.wait_for(need_input.wait(), 3)
                self.assertEqual(job.state, "input_required")
                await service.reply(job, "this")
                await task
            remote.send.assert_awaited_with("this", "c1", "r1")
            self.assertEqual(job.result, "done")
            self.assertEqual(job.state, "completed")
            await service.close()


if __name__ == "__main__":
    unittest.main()
