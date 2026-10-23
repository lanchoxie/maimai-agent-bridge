"""手动真实 Codex 联调，结果只写终端，不向 QQ 发消息。"""

from pathlib import Path

import asyncio
import json
import logging
import tomllib

from plugins.maimai_agent_bridge.config import BridgeConfig
from plugins.maimai_agent_bridge.service import BridgeService


async def main():
    root = Path(__file__).resolve().parents[1]
    config = BridgeConfig.model_validate(tomllib.loads((root / "config.toml").read_text(encoding="utf-8")))
    config.codex.task_timeout_seconds = 180
    async def notify(text, stream):
        print(text, flush=True)
        return True
    service = BridgeService(config, root / ".runtime" / "smoke", notify, logging.getLogger("smoke"))
    await service.load()
    try:
        await service.sessions.set_options("smoke-only", "local-test", "default", effort="low")
        first = await service.submit("smoke-only", "local-test", "只回复 BRIDGE_OK，不调用任何工具。", mode="independent")
        await service.tasks[first.id]
        if first.state != "completed":
            raise RuntimeError(first.error)
        title = await service.bind("smoke-only", "local-test", first.thread_id)
        print(json.dumps({"bound_test_thread": first.thread_id, "title": title}, ensure_ascii=False), flush=True)
        print(await service.sessions.set_options("smoke-only", "local-test", model="gpt-6-sol", effort="low"), flush=True)
        second = await service.submit("smoke-only", "local-test", "上一条 assistant 消息回复的英文标记是什么？只输出该标记，不调用任何工具。", mode="desktop")
        await service.tasks[second.id]
        if second.state != "completed" or "BRIDGE_OK" not in second.result or second.model != "gpt-6-sol" or second.effort != "low":
            raise RuntimeError(second.error or "续聊没有保留上下文")
        history = await service.sessions.history("smoke-only", "local-test")
        if first.model not in history or second.model not in history or "effort=low" not in history:
            raise RuntimeError("历史记录未能取得实际模型与 effort")
        print(await service.sessions.info("smoke-only", "local-test"), flush=True)
        print("SMOKE_PASSED: independent + resume + model/effort switch + current + history", flush=True)
    finally:
        await service.close()


if __name__ == "__main__":
    asyncio.run(main())
