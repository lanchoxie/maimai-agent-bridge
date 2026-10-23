"""Codex App Server stdio 客户端；不通过 shell 拼接用户输入。"""

from pathlib import Path
from typing import Any, Dict, List, Optional

import asyncio
import json
import os
import shutil
import subprocess


def find_codex(configured: str = "") -> str:
    if configured:
        path = Path(configured).resolve()
        if not path.is_file() or path.suffix.lower() != ".exe" and os.name == "nt":
            raise ValueError("Codex 路径必须指向已安装的 codex.exe")
        return str(path)
    executable = shutil.which("codex.exe" if os.name == "nt" else "codex")
    if not executable:
        raise FileNotFoundError("未找到 Codex CLI，请在插件配置中填写 executable")
    return executable


class AppServer:
    """每个任务使用独立的 App Server 连接，持续接收完成/失败事件。"""

    def __init__(self, executable: str = "", *, command: Optional[List[str]] = None,
                 environment: Optional[Dict[str, str]] = None, endpoint: str = "", token: str = ""):
        self.executable = executable
        self.command = command
        self.environment = environment
        self.endpoint = endpoint
        self.token = token
        self.websocket = None
        self.process: Optional[asyncio.subprocess.Process] = None
        self.events: asyncio.Queue = asyncio.Queue()
        self.pending: Dict[int, asyncio.Future] = {}
        self.readers: List[asyncio.Task] = []
        self.sequence = 0
        self.write_lock = asyncio.Lock()
        self.stderr_tail: List[str] = []

    async def start(self) -> None:
        if self.endpoint:
            from websockets.asyncio.client import connect
            headers = {"Authorization": f"Bearer {self.token}"} if self.token else None
            self.websocket = await connect(self.endpoint, additional_headers=headers, open_timeout=20,
                                           max_size=8 * 1024 * 1024, proxy=None)
            self.readers = [asyncio.create_task(self._read())]
        else:
            options: Dict[str, Any] = {}
            if os.name == "nt":
                options["creationflags"] = subprocess.CREATE_NO_WINDOW
            self.process = await asyncio.create_subprocess_exec(
                *(self.command or [self.executable, "app-server", "--listen", "stdio://"]),
                env=self.environment, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=8 * 1024 * 1024, **options,
            )
            self.readers = [asyncio.create_task(self._read()), asyncio.create_task(self._read_stderr())]
        await self.request("initialize", {
            "clientInfo": {"name": "maimai_agent_bridge", "version": "0.5.0"},
            "capabilities": {"experimentalApi": True},
        })
        await self.send({"method": "initialized"})

    async def send(self, payload: Dict[str, Any]) -> None:
        async with self.write_lock:
            frame = json.dumps(payload, ensure_ascii=False)
            if self.websocket is not None:
                await self.websocket.send(frame)
            else:
                if self.process is None or self.process.stdin is None:
                    raise RuntimeError("Codex 连接尚未启动")
                self.process.stdin.write((frame + "\n").encode("utf-8"))
                await self.process.stdin.drain()

    async def request(self, method: str, params: Dict[str, Any], timeout: float = 45) -> Dict[str, Any]:
        self.sequence += 1
        request_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.send({"id": request_id, "method": method, "params": params})
            response = await asyncio.wait_for(future, timeout)
            if "error" in response:
                raise RuntimeError(f"{method}: {response['error'].get('message', response['error'])}")
            return response.get("result", {})
        finally:
            self.pending.pop(request_id, None)

    async def _frames(self):
        if self.websocket is not None:
            async for frame in self.websocket:
                yield frame
        else:
            assert self.process is not None and self.process.stdout is not None
            while line := await self.process.stdout.readline():
                yield line

    async def _read(self) -> None:
        try:
            async for line in self._frames():
                message = json.loads(line)
                if "method" not in message and message.get("id") in self.pending:
                    future = self.pending[message["id"]]
                    if not future.done():
                        future.set_result(message)
                else:
                    await self.events.put(message)
            detail = "\n" + "\n".join(self.stderr_tail[-3:]) if self.stderr_tail else ""
            raise ConnectionError("Codex App Server 连接已关闭" + detail)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(exc)
            await self.events.put({"method": "bridge/disconnected", "params": {"message": str(exc)}})

    async def _read_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        while line := await self.process.stderr.readline():
            self.stderr_tail.append(line.decode("utf-8", errors="replace").strip())
            self.stderr_tail = self.stderr_tail[-20:]

    async def close(self) -> None:
        if self.websocket is not None:
            await self.websocket.close()
            self.websocket = None
        if self.process is not None and self.process.returncode is None:
            if self.process.stdin is not None:
                self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        for task in self.readers:
            task.cancel()
        await asyncio.gather(*self.readers, return_exceptions=True)
        self.readers.clear()
        self.token = ""
        self.environment = None

    async def __aenter__(self):
        try:
            await self.start()
        except BaseException:
            await self.close()
            raise
        return self

    async def __aexit__(self, *args):
        await self.close()
