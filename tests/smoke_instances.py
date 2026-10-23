"""真实 CLI 的只读联调：本机索引、带认证 WebSocket、独立 home/API 环境。"""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import asyncio
import logging
import os
import secrets
import socket
import subprocess
import tomllib

from plugins.maimai_agent_bridge.config import BridgeConfig, CodexInstance
from plugins.maimai_agent_bridge.service import BridgeService


async def main():
    root = Path(__file__).resolve().parents[1]
    config = BridgeConfig(**tomllib.loads((root / "config.toml").read_text(encoding="utf-8")))
    config.instances.max_index_entries = 100
    service = BridgeService(config, root / ".runtime/instances-smoke", AsyncMock(return_value=True), logging.getLogger("smoke"))
    await service.load()
    process = None
    token_file = service.data_dir / "ws-token.txt"
    try:
        await service.instances.probe("local")
        print("REAL_STDIO_OK", flush=True)
        print(await service.instances.refresh("local"), flush=True)
        with socket.socket() as available:
            available.bind(("127.0.0.1", 0))
            port = available.getsockname()[1]
        token = secrets.token_urlsafe(48)
        token_file.write_text(token, encoding="utf-8")
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        process = await asyncio.create_subprocess_exec(
            config.codex.executable, "app-server", "--listen", f"ws://127.0.0.1:{port}",
            "--ws-auth", "capability-token", "--ws-token-file", str(token_file),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **options,
        )
        for _ in range(80):
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                await writer.wait_closed()
                break
            except OSError:
                if process.returncode is not None:
                    _, error = await process.communicate()
                    raise RuntimeError(error.decode(errors="replace").replace(token, "[TOKEN]"))
                await asyncio.sleep(0.25)
        else:
            raise TimeoutError("真实 WebSocket 服务未就绪")
        config.instances.configured.append(CodexInstance(
            id="smoke-ws", transport="websocket", endpoint=f"ws://127.0.0.1:{port}", token_env="BRIDGE_SMOKE_WS_TOKEN",
            workspace=str(service.workspace), allowed_roots=config.codex.allowed_roots, allow_saved_threads=True,
        ))
        with patch.dict(os.environ, {"BRIDGE_SMOKE_WS_TOKEN": token}):
            service.instances.validate()
            await service.instances.probe("smoke-ws")
            await service.instances.refresh("smoke-ws")
            assert "smoke-ws" in await service.instances.cached("smoke-ws")
        print("REAL_AUTHENTICATED_WEBSOCKET_AND_INDEX_OK", flush=True)
        isolated = service.data_dir / "isolated-codex-home"
        isolated.mkdir(exist_ok=True)
        (isolated / "config.toml").write_text('model = "gpt-6.1-sol"\nmodel_reasoning_effort = "low"\n', encoding="utf-8")
        config.instances.configured.append(CodexInstance(
            id="smoke-api", executable=config.codex.executable, codex_home=str(isolated),
            api_base_url="http://127.0.0.1:1/v1", api_key_env="BRIDGE_SMOKE_API_KEY",
        ))
        with patch.dict(os.environ, {"BRIDGE_SMOKE_API_KEY": "test-credential-no-inference"}):
            async with service.codex_connection("smoke-api") as client:
                settings = (await client.request("config/read", {"includeLayers": False}))["config"]
                assert settings["model_provider"] == "maimai_bridge"
                assert settings["model_reasoning_effort"] == "low"
                assert settings["model_providers"]["maimai_bridge"]["env_key"] == "BRIDGE_SMOKE_API_KEY"
        print("REAL_ISOLATED_HOME_PROVIDER_ENV_OK", flush=True)
        print("SMOKE_PASSED: no model inference and no QQ messages", flush=True)
    finally:
        await service.close()
        if process is not None and process.returncode is None:
            process.terminate()
            await process.communicate()
        token_file.unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
