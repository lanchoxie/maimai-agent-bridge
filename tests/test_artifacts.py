"""任务文件范围、回执、重启去重及 QQ 真实来源校验。"""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import asyncio
import json
import unittest

from plugins.maimai_agent_bridge.config import BridgeConfig, CodexInstance
from plugins.maimai_agent_bridge.plugin import AgentBridgePlugin
from plugins.maimai_agent_bridge.service import BridgeService, Job


class ArtifactTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory()
        self.config = BridgeConfig()
        self.config.access.allowed_users = ["owner"]
        self.sender = AsyncMock(return_value={"sent": True, "message_id": "file-message"})
        self.service = BridgeService(self.config, Path(self.temporary.name), AsyncMock(return_value=True),
                                     Mock(), send_file=self.sender)
        await self.service.load()
        self.job = Job("files-test", "group-a", "owner", "codex", "independent", "发送文件", state="completed",
                       workspace=str(self.service.workspace))
        self.service.jobs[self.job.id] = self.job
        self.file = self.service.workspace / "交付 文件.txt"
        self.file.write_text("文件传输测试，内容完整。", encoding="utf-8")

    async def asyncTearDown(self):
        await self.service.close()
        self.temporary.cleanup()

    async def test_upload_snapshot_original_stream_receipt_and_restart_deduplication(self):
        result = await self.service.artifacts.send(self.job, self.file.name)
        self.assertIn("已发送", result)
        payload, stream = self.sender.await_args.args
        self.assertEqual(stream, "group-a")
        self.assertEqual(payload["name"], self.file.name)
        self.assertTrue(payload["url"].startswith("file:///"))
        self.assertIn("%20", payload["url"])
        snapshot = next((self.service.data_dir / "exports").rglob("*.txt"))
        self.assertEqual(snapshot.read_bytes(), self.file.read_bytes())
        self.assertEqual(self.job.artifacts[0]["message_id"], "file-message")
        other = BridgeService(self.config, self.service.data_dir, self.service.notify, Mock(), send_file=self.sender)
        await other.load()
        try:
            await other.artifacts.send(other.jobs[self.job.id], self.file.name)
            await other.artifacts.send(other.jobs[self.job.id], str(self.file))
            self.sender.assert_awaited_once()
        finally:
            await other.close()

    async def test_markers_only_and_multiple_files(self):
        another = self.service.workspace / "image.png"
        another.write_bytes(b"test image bytes")
        self.job.result = (f"说明路径 {self.file}\n[[QQ_FILE:{self.file.name}]]\n"
                           f"[[QQ_FILE:{self.file.name}]]\n[[QQ_FILE:image.png]]")
        self.service.artifacts.collect(self.job)
        self.assertEqual(len(self.job.artifacts), 2)
        self.assertNotIn("[[QQ_FILE:", self.job.result)
        self.assertIn(str(self.file), self.job.result)
        await self.service.artifacts.send(self.job, automatic=True)
        self.assertEqual(self.sender.await_count, 2)

    async def test_outside_missing_directories_secrets_and_size_limit(self):
        outside = Path(self.temporary.name) / "private.txt"
        outside.write_text("private", encoding="utf-8")
        secret = self.service.workspace / ".env"
        secret.write_text("SECRET=value", encoding="utf-8")
        self.config.files.max_file_mb = 1
        large = self.service.workspace / "large.bin"
        large.write_bytes(b"a" * (1024 * 1024 + 1))
        for path in [str(outside), "../private.txt", "missing.txt", ".", ".env", "large.bin"]:
            with self.subTest(path=path), self.assertRaises((ValueError, FileNotFoundError)):
                await asyncio.to_thread(self.service.artifacts.snapshot, self.service.workspace, path, self.job.id)
        self.sender.assert_not_awaited()

    async def test_symlink_escape_rejected(self):
        outside = Path(self.temporary.name) / "private.txt"
        outside.write_text("private", encoding="utf-8")
        link = self.service.workspace / "linked.txt"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("本机不允许创建符号链接")
        with self.assertRaises(ValueError):
            self.service.artifacts.snapshot(self.service.workspace, link.name, self.job.id)

    async def test_real_send_failure_persisted_and_not_automatically_retried(self):
        for result in (False, None, {"sent": False}):
            with self.subTest(result=result):
                self.sender.return_value = result
                self.job.artifacts = []
                response = await self.service.artifacts.send(self.job, self.file.name)
                self.assertIn("发送失败", response)
                self.assertEqual(self.job.artifacts[0]["state"], "failed")
                count = self.sender.await_count
                await self.service.artifacts.send(self.job, automatic=True)
                self.assertEqual(self.sender.await_count, count)
        state = json.loads((self.service.data_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["jobs"][0]["artifacts"][0]["state"], "failed")

    async def test_restart_in_flight_upload_has_unknown_receipt_no_resend(self):
        self.job.artifacts = [{"path": self.file.name, "state": "sending"}]
        await self.service.save()
        other = BridgeService(self.config, self.service.data_dir, self.service.notify, Mock(), send_file=self.sender)
        await other.load()
        try:
            self.assertEqual(other.jobs[self.job.id].artifacts[0]["state"], "unknown")
            await other.artifacts.send(other.jobs[self.job.id], self.file.name)
            self.sender.assert_not_awaited()
        finally:
            await other.close()

    async def test_disabled_active_remote_and_file_count_limits(self):
        self.config.files.enabled = False
        with self.assertRaises(ValueError):
            await self.service.artifacts.send(self.job, self.file.name)
        self.config.files.enabled = True
        self.job.state = "working"
        with self.assertRaises(ValueError):
            await self.service.artifacts.send(self.job, self.file.name)
        self.job.state = "completed"
        self.config.instances.configured = [CodexInstance(id="remote", transport="websocket", workspace="/tmp/tasks")]
        self.job.instance_id = "remote"
        with self.assertRaisesRegex(ValueError, "远端路径"):
            await self.service.artifacts.send(self.job, self.file.name)
        self.job.instance_id = "local"
        self.config.files.max_files = 1
        await self.service.artifacts.send(self.job, self.file.name)
        with self.assertRaises(ValueError):
            await self.service.artifacts.send(self.job, "another.txt")

    async def test_tool_owner_scope_and_custom_capability(self):
        plugin = AgentBridgePlugin()
        plugin._plugin_config_instance = self.config
        plugin.service = self.service
        for stream, user in [("group-b", "owner"), ("group-a", "other")]:
            response = await plugin.agent_files(self.job.id, "send", self.file.name, stream_id=stream, user_id=user,
                                                platform="qq", action_data={})
            self.assertFalse(response["success"])
        self.sender.assert_not_awaited()
        response = await plugin.agent_files(self.job.id, "send", self.file.name, stream_id="group-a", user_id="owner",
                                            platform="qq", action_data={})
        self.assertTrue(response["success"])
        custom = AsyncMock(return_value={"sent": True, "message_id": "real-id"})
        with patch.object(AgentBridgePlugin, "ctx", new=property(lambda _: SimpleNamespace(send=SimpleNamespace(custom=custom)))):
            await plugin._send_file({"name": "测试.txt", "url": "file:///test.txt"}, "group-a")
        self.assertEqual(custom.await_args.args, ("file", {"name": "测试.txt", "url": "file:///test.txt"}, "group-a"))
        self.assertTrue(custom.await_args.kwargs["return_details"])

    async def test_completion_sends_files_and_records_failure_without_failing_codex(self):
        async def execute(job):
            job.workspace = str(self.service.workspace)
            job.result = f"完成\n[[QQ_FILE:{self.file.name}]]"
            self.service.artifacts.collect(job)
        with patch.object(self.service, "_run_codex", execute):
            job = await self.service.submit("group-a", "owner", "请交付文件")
            await self.service.tasks[job.id]
        self.assertEqual(job.state, "completed")
        self.assertEqual(job.artifacts[0]["state"], "sent")
        self.assertIn("sent", self.service.notify.await_args.args[0])
        self.assertNotIn("QQ_FILE", self.service.notify.await_args.args[0])
