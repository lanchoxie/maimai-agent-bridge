"""只回传任务目录内明确指定的本机产物，并持久化上传回执。"""

from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List

import asyncio
import hashlib
import os
import re
import stat

if TYPE_CHECKING:
    from .service import BridgeService, Job


FILE_MARKER = re.compile(r"^\s*\[\[QQ_FILE:([^\r\n]+?)\]\]\s*$", re.MULTILINE)
PRIVATE_NAMES = {"auth.json", "credentials.json", "id_rsa", "id_ed25519"}
PRIVATE_DIRS = {".git", ".codex", ".ssh", ".venv", "node_modules"}


class Artifacts:
    def __init__(self, service: "BridgeService") -> None:
        self.service = service
        self.lock = asyncio.Lock()

    def prompt(self) -> str:
        if not self.service.config.files.enabled or self.service.send_file is None:
            return ""
        return (
            "\n\n[QQ 文件交付说明]\n"
            "本轮通过 QQ 桥接执行。若用户要求交付、下载或发送文件，先完成并验证文件，"
            "然后在最终回复中为每个交付文件独立写一行 [[QQ_FILE:相对于当前工作目录的文件路径]]。"
            f"最多 {self.service.config.files.max_files} 个文件，单个不超过 {self.service.config.files.max_file_mb} MB。"
            "插件会将文件上传回发起任务的 QQ 会话，你不需要自己调用 QQ。"
            "只标记用户要求的交付文件；不要标记凭据、配置、依赖或仅用于说明的路径。"
            "目录请先打包为 zip。无需文件交付时不要输出标记。"
        )

    def collect(self, job: "Job") -> None:
        """只接受独立行标记，不扫描正文路径或递归遍历工作目录。"""
        matches = FILE_MARKER.findall(job.result)
        paths = list(dict.fromkeys(value.strip() for value in matches))
        limit = self.service.config.files.max_files
        if len(paths) > limit:
            job.artifact_error = f"交付文件超过 {limit} 个，仅登记前 {limit} 个"
        for path in paths[:limit]:
            if not any(item["path"] == path for item in job.artifacts):
                job.artifacts.append({"path": path, "state": "pending"})
        job.result = FILE_MARKER.sub("", job.result).strip()

    async def workspace(self, job: "Job") -> Path:
        if job.agent != "codex" or self.service.instances.get(job.instance_id).transport != "stdio":
            raise ValueError("文件回传目前只支持本机 Codex；远端路径不会作为本机文件读取")
        if not job.workspace:
            if not job.thread_id:
                raise ValueError("该任务没有可确认的工作目录")
            async with self.service.codex_connection(job.instance_id) as client:
                thread = (await client.request("thread/read", {"threadId": job.thread_id, "includeTurns": False}))["thread"]
                job.workspace = str(self.service.check_root(thread["cwd"], job.instance_id))
                await self.service.save()
        return self.service.check_root(job.workspace, job.instance_id)

    def snapshot(self, root: Path, path: str, job_id: str) -> Dict[str, Any]:
        """验证真实路径并读取有大小上限的快照，避免上传过程中源文件变化。"""
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = root / candidate
        target = candidate.resolve(strict=True)
        if not target.is_relative_to(root):
            raise ValueError("文件必须位于该任务的工作目录内")
        relative = target.relative_to(root)
        lowered = target.name.lower()
        if (any(part.lower() in PRIVATE_DIRS for part in relative.parts)
                or lowered in PRIVATE_NAMES or lowered == ".env" or lowered.startswith(".env.")
                or target.suffix.lower() in {".pem", ".key"}):
            raise ValueError("凭据和内部目录不支持文件回传")
        limit = self.service.config.files.max_file_mb * 1024 * 1024
        if not target.is_file():
            raise ValueError("只能发送普通文件；目录请先打包为 zip")
        with target.open("rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError("只能发送普通文件")
            data = source.read(limit + 1)
        if len(data) > limit:
            raise ValueError(f"文件超过 {self.service.config.files.max_file_mb} MB 上限")
        digest = hashlib.sha256(data).hexdigest()
        export = self.service.data_dir / "exports" / job_id / digest / target.name
        export.parent.mkdir(parents=True, exist_ok=True)
        export.write_bytes(data)
        return {"path": relative.as_posix(), "name": target.name, "size": len(data),
                "sha256": digest, "url": export.resolve().as_uri()}

    async def send(self, job: "Job", path: str = "", automatic: bool = False) -> str:
        if not self.service.config.files.enabled or self.service.send_file is None:
            raise ValueError("插件未启用文件回传能力")
        if job.state != "completed":
            raise ValueError("请等待任务完成后发送文件")
        root = await self.workspace(job)
        async with self.lock:
            if path:
                entry = next((item for item in job.artifacts if item["path"] == path), None)
                if entry is None:
                    if len(job.artifacts) >= self.service.config.files.max_files:
                        raise ValueError(f"每个任务最多回传 {self.service.config.files.max_files} 个文件")
                    entry = {"path": path, "state": "pending"}
                    job.artifacts.append(entry)
                entries = [entry]
            else:
                entries = list(job.artifacts)
            if not entries:
                raise ValueError("此任务没有登记交付文件；请指定任务目录内的文件路径")
            lines: List[str] = []
            for entry in entries:
                if entry["state"] in {"sent", "sending", "unknown"}:
                    lines.append(f"{entry['path']}：{entry['state']}，不重复发送")
                    continue
                if automatic and entry["state"] != "pending":
                    continue
                try:
                    snapshot = await asyncio.to_thread(self.snapshot, root, entry["path"], job.id)
                    duplicate = next((item for item in job.artifacts if item is not entry
                                      and item["path"] == snapshot["path"]
                                      and item.get("sha256") == snapshot["sha256"]
                                      and item["state"] in {"sent", "sending", "unknown"}), None)
                    if duplicate is not None:
                        job.artifacts.remove(entry)
                        lines.append(f"{snapshot['path']}：{duplicate['state']}，不重复发送")
                        await self.service.save()
                        continue
                    entry.update(snapshot, state="sending", error="")
                    await self.service.save()
                    # Host 的 FileComponent 使用 url 字段传递本地 file URI，
                    # SnowLuma/NapCat 适配器再转换为上传动作或 file 消息段。
                    result = await self.service.send_file(
                        {"name": entry["name"], "size": entry["size"], "url": entry["url"]}, job.stream_id,
                    )
                    if result is not True and not (isinstance(result, dict) and result.get("sent") is True):
                        raise RuntimeError("QQ 文件发送返回失败")
                    entry["state"] = "sent"
                    entry["message_id"] = result.get("message_id") if isinstance(result, dict) else None
                    lines.append(f"已发送：{entry['name']}（{entry['size']} 字节）")
                except Exception as exc:
                    entry["state"] = "failed"
                    entry["error"] = self.service.instances.scrub(str(exc))[:1000]
                    lines.append(f"{entry['path']}：发送失败，{entry['error']}")
                await self.service.save()
            return "\n".join(lines)

    @staticmethod
    def listing(job: "Job") -> str:
        lines = [f"任务 {job.id} 的交付文件："]
        lines.extend(f"{item['path']} · {item['state']}" + (f" · {item['error']}" if item.get("error") else "")
                     for item in job.artifacts)
        if job.artifact_error:
            lines.append(job.artifact_error)
        return "\n".join(lines) if job.artifacts or job.artifact_error else "此任务尚未登记交付文件"
