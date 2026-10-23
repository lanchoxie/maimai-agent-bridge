"""固定配置的 A2A 远端客户端，支持 1.0 REST 与 0.3 JSON-RPC。"""

from typing import Any, Dict
from urllib.parse import quote, urlsplit

import os
import uuid

import httpx

from .config import RemoteAgent


class RemoteClient:
    def __init__(self, config: RemoteAgent):
        self.config = config
        url = urlsplit(config.endpoint)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            raise ValueError("远程 endpoint 必须是没有内嵌密码的 HTTP/HTTPS 地址")
        headers = {"Content-Type": "application/json"}
        if config.protocol == "1.0-rest":
            headers.update({"A2A-Version": "1.0", "Content-Type": "application/a2a+json"})
        if config.token_env:
            token = os.environ.get(config.token_env, "")
            if not token:
                raise ValueError(f"未设置远端鉴权环境变量 {config.token_env}")
            headers["Authorization"] = f"Bearer {token}"
        self.client = httpx.AsyncClient(headers=headers, timeout=45, follow_redirects=False)
        self.base = config.endpoint.rstrip("/")

    async def _jsonrpc(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        response = await self.client.post(self.base, json={
            "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params,
        })
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise RuntimeError(f"A2A {method}: {payload['error'].get('message', '未知错误')}")
        return payload["result"]

    async def send(self, text: str, context_id: str = "", task_id: str = "") -> Dict[str, Any]:
        is_v1 = self.config.protocol == "1.0-rest"
        message = {
            "messageId": str(uuid.uuid4()), "role": "ROLE_USER" if is_v1 else "user",
            "parts": [{"text": text} if is_v1 else {"kind": "text", "text": text}],
        }
        if not is_v1:
            message["kind"] = "message"
        if context_id:
            message["contextId"] = context_id
        if task_id:
            message["taskId"] = task_id
        if not is_v1:
            return await self._jsonrpc("message/send", {"message": message, "configuration": {"blocking": False}})
        response = await self.client.post(self.base + "/message:send", json={
            "message": message, "configuration": {"returnImmediately": True, "acceptedOutputModes": ["text/plain"]},
        })
        response.raise_for_status()
        payload = response.json()
        return payload.get("task", payload.get("message", payload))

    async def get(self, task_id: str) -> Dict[str, Any]:
        if self.config.protocol == "0.3-jsonrpc":
            return await self._jsonrpc("tasks/get", {"id": task_id, "historyLength": 0})
        response = await self.client.get(self.base + "/tasks/" + quote(task_id, safe=""))
        response.raise_for_status()
        payload = response.json()
        return payload.get("task", payload)

    async def cancel(self, task_id: str) -> None:
        if self.config.protocol == "0.3-jsonrpc":
            await self._jsonrpc("tasks/cancel", {"id": task_id})
        else:
            response = await self.client.post(self.base + "/tasks/" + quote(task_id, safe="") + ":cancel", json={})
            response.raise_for_status()

    async def close(self) -> None:
        await self.client.aclose()


def extract_text(payload: Dict[str, Any]) -> str:
    parts = list(payload.get("parts", []))
    for artifact in payload.get("artifacts", []):
        parts.extend(artifact.get("parts", []))
    if not parts:
        parts.extend(payload.get("status", {}).get("message", {}).get("parts", []))
    return "\n".join(part["text"] for part in parts if isinstance(part.get("text"), str))
