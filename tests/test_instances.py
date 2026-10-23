"""验证多实例隔离、远程实际传输和只含元数据的缓存；不发送 QQ 消息。"""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import asyncio
import json
import os
import unittest

from websockets.asyncio.server import serve

from plugins.maimai_agent_bridge.config import BridgeConfig, CodexInstance, EnvironmentRef
from plugins.maimai_agent_bridge.instances import discover
from plugins.maimai_agent_bridge.plugin import AgentBridgePlugin
from plugins.maimai_agent_bridge.service import BridgeService, Job


class InstanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory()
        self.config = BridgeConfig()
        self.config.codex.allow_saved_threads = True
        self.config.access.allowed_users = ["owner"]
        self.remote = CodexInstance(id="server", transport="ssh", ssh_target="user@server",
                                    workspace="/srv/jobs", allowed_roots=["/srv/project"], allow_saved_threads=True)
        self.config.instances.configured = [self.remote]
        self.scanner = patch("plugins.maimai_agent_bridge.instances.discover", return_value=[])
        self.scanner.start()
        self.service = BridgeService(self.config, Path(self.temporary.name), AsyncMock(return_value=True), Mock())
        await self.service.load()

    async def asyncTearDown(self):
        await self.service.close()
        self.scanner.stop()
        self.temporary.cleanup()

    async def test_selection_same_thread_ids_preferences_and_continuation_are_isolated(self):
        for instance, effort in [("local", "low"), ("server", "high")]:
            self.service.bindings[self.service.binding_key("group", "owner", "current", instance)] = "same-thread-id"
            self.service.preferences[self.service.binding_key("group", "owner", "same-thread-id", instance)] = {"effort": effort}
        await self.service.instances.select("group", "owner", "server")
        self.assertEqual(self.service.instances.selected("other-group", "owner"), "local")
        self.assertEqual(self.service.sessions.choices("group", "owner", "same-thread-id"), {"effort": "high"})
        self.assertEqual(self.service.sessions.choices("group", "owner", "same-thread-id", "local"), {"effort": "low"})
        previous = Job("old", "group", "owner", "codex", "independent", "old", state="completed",
                       thread_id="same-thread-id", instance_id="server")
        self.service.jobs[previous.id] = previous
        await self.service.instances.select("group", "owner", "local")
        blocker = asyncio.Event()
        async def execute(job):
            await blocker.wait()
            job.result = "ok"
        with patch.object(self.service, "_run_codex", execute):
            job = await self.service.submit("group", "owner", "continue", previous_id="old")
            task = self.service.tasks[job.id]
            self.assertEqual((job.instance_id, job.effort), ("server", "high"))
            # 同一个 UUID 在另一台机器执行，不应误判成同一聊天正在运行。
            local = await self.service.submit("group", "owner", "local")
            local_task = self.service.tasks[local.id]
            blocker.set()
            await asyncio.gather(task, local_task)
        saved = json.loads((self.service.data_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["instance_selections"][self.service.binding_key("group", "owner", "instance")], "local")
        self.assertEqual(next(row for row in saved["jobs"] if row["id"] == job.id)["instance_id"], "server")

    async def test_buffer_excludes_content_credentials_and_rechecks_access_and_identity(self):
        self.remote.token_env = "BRIDGE_TEST_TOKEN"
        token = "private-token-not-for-cache"
        thread = {"id": "saved-id", "cwd": "/srv/project/code", "name": "title " + token, "updatedAt": 1700000000,
                  "model": "model-a", "reasoningEffort": "high", "preview": "must-not-cache-chat",
                  "turns": [{"secret": "must-not-cache-turn"}], "api_key": token}
        with patch.dict(os.environ, {"BRIDGE_TEST_TOKEN": token}):
            await self.service.instances.cache("server", [thread])
            await self.service.instances.cache("local", [{**thread, "cwd": str(self.service.workspace), "name": "local title"}])
            cached = await self.service.instances.cached("server")
            self.assertIn("saved-id", cached)
            self.assertNotIn("local title", cached)
            raw = (self.service.instances.buffer_dir / "sessions-server.json").read_text(encoding="utf-8")
            for value in (token, "must-not-cache-chat", "must-not-cache-turn", "api_key"):
                self.assertNotIn(value, raw)
        self.remote.allowed_roots = []
        self.assertNotIn("saved-id", await self.service.instances.cached("server"))
        self.remote.allowed_roots = ["/srv/project"]
        self.remote.ssh_target = "user@different-server"
        self.assertNotIn("saved-id", await self.service.instances.cached("server"))
        with self.assertRaises(ValueError):
            self.service.check_root("/srv/project/../../etc", "server")
        self.remote.allow_saved_threads = False
        with self.assertRaises(ValueError):
            await self.service.instances.cached("server")

    async def test_local_conda_api_environment_and_ssh_secret_stays_off_command_line(self):
        executable = Path(self.temporary.name) / "codex.exe"
        executable.touch()
        prefix = Path(self.temporary.name) / "conda-env"
        prefix.mkdir()
        item = CodexInstance(id="conda", executable=str(executable), conda_prefix=str(prefix),
                             codex_home=str(prefix / ".codex"), api_base_url="https://example.test/v1",
                             api_key_env="BRIDGE_API_KEY",
                             environment=[EnvironmentRef(name="OTHER_TOKEN", source_env="LOCAL_TOKEN")])
        self.config.instances.configured.append(item)
        self.service.instances.validate()
        with patch.dict(os.environ, {"BRIDGE_API_KEY": "secret-api-test", "LOCAL_TOKEN": "other-secret-test"}):
            client = self.service.instances.connect("conda")
            self.assertIn("secret-api-test", client.environment.values())
            self.assertEqual(client.environment["OTHER_TOKEN"], "other-secret-test")
            self.assertEqual(client.environment["CODEX_HOME"], str(prefix / ".codex"))
            self.assertNotIn("secret-api-test", " ".join(client.command))
            self.assertTrue(client.environment["PATH"].startswith(str(prefix)))
            self.assertNotEqual(os.environ.get("CODEX_HOME"), client.environment["CODEX_HOME"])
            self.assertNotIn("other-secret-test", self.service.instances.scrub("error other-secret-test"))
            self.remote.api_base_url = item.api_base_url
            self.remote.api_key_env = "BRIDGE_API_KEY"
            self.remote.conda_prefix = "/opt/envs/my env;$(echo injected)"
            self.remote.codex_home = "/opt/codex home"
            with patch("plugins.maimai_agent_bridge.instances.shutil.which", return_value="ssh"):
                ssh = self.service.instances.connect("server")
            self.assertNotIn("secret-api-test", " ".join(ssh.command))
            self.assertIsNone(ssh.environment)  # API 密钥来自远端环境，不走 SSH argv。
            import shlex
            remote_argv = shlex.split(ssh.command[-1])
            self.assertIn(self.remote.conda_prefix, remote_argv)
            self.assertIn("BatchMode=yes", ssh.command)
            self.assertIn("StrictHostKeyChecking=yes", ssh.command)
        item.api_key_env = "MISSING_TEST_KEY_234"
        with self.assertRaisesRegex(ValueError, "MISSING_TEST_KEY_234"):
            self.service.instances.connect("conda")

    async def test_authenticated_websocket_runs_task_reads_history_and_detects_disconnect(self):
        requests = []
        token = "test-websocket-bearer"
        thread = {"id": "remote-thread-id", "cwd": "/srv/jobs", "model": "remote-model",
                  "reasoningEffort": "low", "path": str(self.service.data_dir / "must-not-read.jsonl")}
        async def handler(socket):
            self.assertEqual(socket.request.headers["Authorization"], "Bearer " + token)
            async for frame in socket:
                message = json.loads(frame)
                method = message["method"]
                requests.append(message)
                if method == "initialized":
                    continue
                if method == "initialize":
                    result = {"userAgent": "fake-remote"}
                elif method in {"thread/start", "thread/read", "thread/resume"}:
                    result = {"thread": thread}
                elif method == "turn/start":
                    result = {"turn": {"id": "turn-1"}}
                elif method == "thread/turns/list":
                    result = {"data": [{"id": "turn-1", "items": [{"type": "agentMessage", "text": "remote answer"}]}]}
                elif method == "config/read":
                    result = {"config": {"model": "remote-model"}}
                elif method == "disconnect-test":
                    await socket.close()
                    return
                else:
                    raise AssertionError(method)
                await socket.send(json.dumps({"id": message["id"], "result": result}))
                if method == "turn/start":
                    await socket.send(json.dumps({"method": "item/completed", "params": {"threadId": thread["id"],
                        "item": {"type": "agentMessage", "id": "reply-1", "text": "remote answer"}}}))
                    await socket.send(json.dumps({"method": "turn/completed", "params": {"threadId": thread["id"],
                        "turn": {"id": "turn-1", "status": "completed"}}}))
        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            item = CodexInstance(id="ws", transport="websocket", endpoint=f"ws://127.0.0.1:{port}",
                                 token_env="WS_TEST_TOKEN", workspace="/srv/jobs", allow_saved_threads=True)
            self.config.instances.configured.append(item)
            self.service.instances.validate()
            with patch.dict(os.environ, {"WS_TEST_TOKEN": token}):
                await self.service.instances.select("group", "owner", "ws")
                job = await self.service.submit("group", "owner", "a prompt; $(not a shell command)")
                await self.service.tasks[job.id]
                self.assertEqual((job.state, job.instance_id, job.result), ("completed", "ws", "remote answer"))
                self.assertEqual(self.service.notify.await_args.args[1], "group")
                with patch("plugins.maimai_agent_bridge.sessions.read_turn_settings", side_effect=AssertionError("must not read local file")):
                    history = await self.service.sessions.history("group", "owner")
                self.assertIn("Codex: remote answer", history)
                self.assertIn("未记录", history)
                async with self.service.codex_connection("ws") as client:
                    with self.assertRaises(ConnectionError):
                        await client.request("disconnect-test", {})
        turn = next(row for row in requests if row["method"] == "turn/start")
        self.assertEqual(turn["params"]["input"][0]["text"], job.prompt)
        self.assertEqual(turn["params"]["sandboxPolicy"], {"type": "readOnly"})

    async def test_commands_and_tools_use_real_user_context(self):
        plugin = AgentBridgePlugin()
        plugin._plugin_config_instance = self.config
        plugin.service = self.service
        plugin._ctx = SimpleNamespace(logger=Mock(), send=SimpleNamespace(text=AsyncMock(return_value=True)))
        context = {"stream_id": "group", "user_id": "owner", "platform": "qq"}
        for body in ("instances", "instance server", "index", "instance local", "scan"):
            result = await plugin.command_agent(**context, matched_groups={"body": body})
            self.assertTrue(result[0], result[1])
        result = await plugin.agent_instances(operation="select", target="server", action_data={"target": "server", "user_id": "owner"}, **context)
        self.assertFalse(result["success"])
        self.assertEqual(self.service.instances.selected("group", "owner"), "local")
        result = await plugin.agent_instances(operation="select", target="server", action_data={"operation": "select", "target": "server"}, **context)
        self.assertTrue(result["success"])
        with self.assertRaises(ValueError):
            await self.service.instances.select("group", "owner", "../evil")


class DiscoveryTests(unittest.TestCase):
    def test_conda_registration_and_native_npm_binary_are_discovered_without_running(self):
        with TemporaryDirectory() as temporary:
            home = Path(temporary)
            prefix = home / "miniconda3/envs/test"
            (prefix / "conda-meta").mkdir(parents=True)
            executable = prefix / "codex.exe"
            executable.write_bytes(b"not executable; scan must not launch")
            npm = home / "npm/node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/vendor/x86/bin/codex.exe"
            npm.parent.mkdir(parents=True)
            npm.touch()
            with patch("plugins.maimai_agent_bridge.instances.Path.home", return_value=home), patch.dict(os.environ, {"PATH": "", "CONDA_PREFIX": "", "CONDA_EXE": ""}):
                first = discover([str(home / "npm")])
                second = discover([str(home / "npm")])
            self.assertEqual(first, second)
            self.assertEqual(len(first), 2)
            self.assertEqual(next(row for row in first if row["executable"] == str(executable))["conda_prefix"], str(prefix))
