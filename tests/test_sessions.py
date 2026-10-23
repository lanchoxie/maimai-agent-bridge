"""session 分页、真实模型来源、配置继承和 QQ 命令的验证。"""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import asyncio
import json
import unittest

from plugins.maimai_agent_bridge.config import BridgeConfig
from plugins.maimai_agent_bridge.plugin import AgentBridgePlugin
from plugins.maimai_agent_bridge.service import BridgeService
from plugins.maimai_agent_bridge.sessions import read_turn_settings


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory()
        self.config = BridgeConfig()
        self.config.access.allowed_users = ["owner"]
        self.config.codex.allow_saved_threads = True
        self.service = BridgeService(self.config, Path(self.temporary.name), AsyncMock(return_value=True), Mock())
        await self.service.load()
        self.thread = {"id": "thread-id-123", "cwd": str(self.service.workspace), "name": "project", "model": "model-a",
                       "reasoningEffort": "high", "modelProvider": "test", "updatedAt": 1700000000}
        self.catalog = [{"id": "model-a", "model": "model-a", "supportedReasoningEfforts": [{"reasoningEffort": "high"}, {"reasoningEffort": "low"}]},
                        {"id": "model-b", "model": "model-b", "supportedReasoningEfforts": [{"reasoningEffort": "low"}]}]
        self.client = AsyncMock()
        self.client.__aenter__.return_value = self.client
        self.client.request.side_effect = self.respond
        self.connector = patch.object(self.service.sessions, "connect", return_value=self.client)
        self.connector.start()

    async def asyncTearDown(self):
        self.connector.stop()
        await self.service.close()
        self.temporary.cleanup()

    async def respond(self, method, params):
        if method == "thread/read":
            return {"thread": dict(self.thread)}
        if method == "model/list":
            return {"data": self.catalog}
        if method == "config/read":
            return {"config": {"model": "model-a", "model_reasoning_effort": "high"}}
        raise AssertionError(method)

    async def test_list_filters_before_pagination_and_shows_recorded_model(self):
        async def respond(method, params):
            self.assertEqual(method, "thread/list")
            self.assertEqual(params["sortKey"], "updated_at")
            self.assertEqual(params["searchTerm"], "project")
            self.assertTrue(params["archived"])
            if not params.get("cursor"):
                return {"data": [{**self.thread, "id": "not-allowed", "cwd": str(self.service.data_dir.parent), "name": "secret"}], "nextCursor": "next"}
            return {"data": [{**self.thread, "id": f"allowed-{i}"} for i in range(9)], "nextCursor": None}
        self.client.request.side_effect = respond
        first = await self.service.sessions.listing(1, "project", True)
        self.assertNotIn("secret", first)
        self.assertIn("model-a · effort=high", first)
        self.assertIn("allowed-7", first)
        self.assertNotIn("allowed-8", first)
        self.assertIn("sessions archived 2 project", first)
        second = await self.service.sessions.listing(2, "project", True)
        self.assertIn("allowed-8", second)
        self.assertNotIn("allowed-7", second)

    async def test_current_binding_is_scoped_and_info_separates_history_and_override(self):
        self.service.bindings[self.service.binding_key("group-a", "owner", "current")] = self.thread["id"]
        await self.service.sessions.set_options("group-a", "owner", model="model-b", effort="low")
        info = await self.service.sessions.info("group-a", "owner")
        self.assertIn("历史最新模型: model-a", info)
        self.assertIn("历史最新 effort: high", info)
        self.assertIn("下次插件调用: model-b / low", info)
        for stream, user in [("group-b", "owner"), ("group-a", "someone")]:
            with self.assertRaises(ValueError):
                self.service.sessions.resolve(stream, user)
            self.assertNotIn("model", self.service.sessions.choices(stream, user, self.thread["id"]))

    async def test_invalid_pair_and_failed_lookup_leave_preferences_unchanged(self):
        with self.assertRaises(ValueError):
            await self.service.sessions.set_options("group-a", "owner", self.thread["id"], model="model-b")
        self.assertFalse(self.service.preferences)
        self.client.request.side_effect = RuntimeError("server down")
        with self.assertRaises(RuntimeError):
            await self.service.sessions.set_options("group-a", "owner", self.thread["id"], model="model-b", effort="low")
        self.assertFalse(self.service.preferences)

    async def test_preferences_persist_inherit_and_queue_snapshots(self):
        await self.service.sessions.set_options("group-a", "owner", "default", model="model-b", effort="low")
        finish = asyncio.Event()
        async def execute(job):
            await finish.wait()
            job.result = "ok"
        with patch.object(self.service, "_run_codex", execute):
            job = await self.service.submit("group-a", "owner", "do it")
            task = self.service.tasks[job.id]
            await self.service.sessions.set_options("group-a", "owner", "default", model="model-a", effort="high")
            self.assertEqual((job.model, job.effort), ("model-b", "low"))
            finish.set()
            await task
        other = BridgeService(self.config, self.service.data_dir, AsyncMock(), Mock())
        await other.load()
        self.assertEqual(other.sessions.choices("group-a", "owner"), {"model": "model-a", "effort": "high"})
        await other.close()
        await self.service.sessions.set_options("group-a", "owner", "default", model="inherit", effort="inherit")
        self.assertEqual(self.service.sessions.choices("group-a", "owner"), {})

    async def test_history_pagination_only_displays_chat_and_actual_turn_settings(self):
        async def respond(method, params):
            if method == "thread/read":
                return {"thread": {**self.thread, "path": "verified-by-reader"}}
            self.assertEqual(method, "thread/turns/list")
            self.assertEqual(params["itemsView"], "full")
            if not params.get("cursor"):
                return {"data": [], "nextCursor": "older"}
            return {"data": [{"id": "turn-old", "items": [
                {"type": "userMessage", "content": [{"type": "text", "text": "hello"}]},
                {"type": "reasoning", "content": ["must not leak"]},
                {"type": "commandExecution", "aggregatedOutput": "private output"},
                {"type": "agentMessage", "text": "answer", "phase": "final_answer"},
            ]}], "nextCursor": None}
        self.client.request.side_effect = respond
        with patch("plugins.maimai_agent_bridge.sessions.read_turn_settings", return_value={"turn-old": {"model": "historical-model", "effort": "medium"}}):
            result = await self.service.sessions.history("group-a", "owner", self.thread["id"], 2)
        self.assertIn("historical-model / effort=medium", result)
        self.assertIn("你: hello", result)
        self.assertIn("Codex: answer", result)
        self.assertNotIn("must not leak", result)
        self.assertNotIn("private output", result)
        self.assertNotIn("model-a", result)

    async def test_history_access_still_checks_allowed_root(self):
        self.thread["cwd"] = str(self.service.data_dir.parent)
        with self.assertRaises(ValueError):
            await self.service.sessions.history("group-a", "owner", self.thread["id"])
        self.assertEqual(self.client.request.await_count, 1)

    async def test_command_parsing_and_spoof_protection(self):
        plugin = AgentBridgePlugin()
        plugin._plugin_config_instance = self.config
        plugin.service = self.service
        send = AsyncMock(return_value=True)
        plugin._ctx = SimpleNamespace(logger=Mock(), send=SimpleNamespace(text=send))
        context = {"stream_id": "group-a", "user_id": "owner", "platform": "qq"}
        for body in ["model model-b low default", "effort high default", "model model-a high default"]:
            result = await plugin.command_agent(**context, matched_groups={"body": body})
            self.assertEqual(result[0], body != "effort high default")
        key = self.service.binding_key("group-a", "owner", "default")
        self.assertEqual(self.service.preferences[key], {"model": "model-a", "effort": "high"})
        response = await plugin.agent_sessions(operation="info", target="default", action_data={"user_id": "owner"}, **context)
        self.assertFalse(response["success"])
        response = await plugin.agent_configure(model="model-b", effort="low", target="default",
                                                action_data={"model": "model-b", "effort": "low", "target": "default"}, **context)
        self.assertTrue(response["success"])

    async def test_agent_bind_requires_trusted_context_and_explicit_session_id(self):
        plugin = AgentBridgePlugin()
        plugin._plugin_config_instance = self.config
        plugin.service = self.service
        context = {"stream_id": "group-a", "user_id": "owner", "platform": "qq", "group_id": "group-a"}
        self.service.bind = AsyncMock(return_value="project")

        response = await plugin.agent_bind(operation="bind", thread_id="thread-id-123",
                                           action_data={"operation": "bind", "thread_id": "thread-id-123"},
                                           **context)
        self.assertTrue(response["success"])
        self.service.bind.assert_awaited_once_with("group-a", "owner", "thread-id-123")

        response = await plugin.agent_bind(operation="bind", thread_id="thread-id-123",
                                           action_data={"operation": "bind", "thread_id": "thread-id-123",
                                                        "user_id": "owner"}, **context)
        self.assertFalse(response["success"])
        self.assertEqual(self.service.bind.await_count, 1)

        response = await plugin.agent_bind(operation="bind", thread_id="thread-id-123",
                                           action_data={"operation": "bind", "thread_id": "thread-id-123"},
                                           stream_id="group-a", platform="qq")
        self.assertFalse(response["success"])
        self.assertIn("/agent", response["content"])

    async def test_legacy_state_without_preferences_or_model_fields_loads(self):
        path = self.service.data_dir / "state.json"
        path.write_text(json.dumps({"bindings": {}, "jobs": [{"id": "old", "stream_id": "group-a", "user_id": "owner",
            "agent": "codex", "mode": "independent", "prompt": "old", "state": "completed"}]}), encoding="utf-8")
        other = BridgeService(self.config, self.service.data_dir, AsyncMock(), Mock())
        await other.load()
        self.assertEqual(other.jobs["old"].model, "")
        self.assertFalse(other.preferences)
        await other.close()

    async def test_execution_sends_selected_model_effort_and_tracks_actual_session(self):
        selected_thread = {**self.thread, "model": "model-b", "reasoningEffort": "low"}
        events = asyncio.Queue()
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.events = events
        async def respond(method, params):
            if method == "thread/read":
                return {"thread": dict(selected_thread)}
            if method == "thread/resume":
                self.assertEqual(params["model"], "model-b")
                self.assertEqual(params["config"]["model_reasoning_effort"], "low")
                return {"thread": selected_thread, "model": "model-b", "reasoningEffort": "low"}
            if method == "turn/start":
                self.assertEqual(params["model"], "model-b")
                self.assertEqual(params["effort"], "low")
                self.assertEqual(params["sandboxPolicy"]["type"], "readOnly")
                events.put_nowait({"method": "item/completed", "params": {"threadId": self.thread["id"],
                                    "item": {"id": "answer", "type": "agentMessage", "text": "ok"}}})
                events.put_nowait({"method": "turn/completed", "params": {"threadId": self.thread["id"], "turn": {"id": "turn-1", "status": "completed"}}})
                return {"turn": {"id": "turn-1"}}
            raise AssertionError(method)
        client.request.side_effect = respond
        self.service.bindings[self.service.binding_key("group-a", "owner")] = self.thread["id"]
        self.service.preferences[self.service.binding_key("group-a", "owner", self.thread["id"])] = {"model": "model-b", "effort": "low"}
        with patch("plugins.maimai_agent_bridge.service.find_codex", return_value="codex"), patch(
            "plugins.maimai_agent_bridge.service.AppServer", return_value=client,
        ):
            job = await self.service.submit("group-a", "owner", "question", mode="desktop")
            await self.service.tasks[job.id]
        self.assertEqual(job.state, "completed")
        self.assertEqual((job.model, job.effort), ("model-b", "low"))
        self.assertEqual(self.service.sessions.resolve("group-a", "owner"), self.thread["id"])


class MetadataTests(unittest.TestCase):
    def test_rollout_context_verified_and_latest_context_for_each_turn(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "sessions" / "test.jsonl"
            path.parent.mkdir()
            rows = [{"type": "session_meta", "payload": {"id": "thread-1"}},
                    {"type": "turn_context", "payload": {"turn_id": "t1", "model": "m1", "effort": "low"}},
                    {"type": "turn_context", "payload": {"turn_id": "t2", "model": "m2", "effort": "high"}}]
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            result = read_turn_settings(str(path), "thread-1", {"t1", "t2"}, root)
            self.assertEqual(result["t1"], {"model": "m1", "effort": "low"})
            self.assertEqual(result["t2"], {"model": "m2", "effort": "high"})
            with self.assertRaises(ValueError):
                read_turn_settings(str(path), "other-thread", {"t1"}, root)
            with self.assertRaises(ValueError):
                read_turn_settings(str(root / "outside.jsonl"), "thread-1", {"t1"}, root)


if __name__ == "__main__":
    unittest.main()
