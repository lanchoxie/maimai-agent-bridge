"""Codex session 浏览和按 QQ 来源隔离的模型选择。"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set

import asyncio
import json
import os
import re

from .rpc import AppServer

if TYPE_CHECKING:
    from .service import BridgeService

EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
PAGE_SIZE = 8
HISTORY_SIZE = 4


def timestamp(value: Any) -> str:
    if value is None:
        return "未记录"
    return datetime.fromtimestamp(float(value), timezone(timedelta(hours=8))).strftime("%m-%d %H:%M")


def read_turn_settings(path: str, thread_id: str, turn_ids: Set[str], codex_dir: Optional[Path] = None) -> Dict[str, Dict[str, str]]:
    """仅从 Codex 自己的 rollout 目录读取每轮模型字段，不读取任意用户路径。"""
    source = Path(path).resolve()
    codex_dir = (codex_dir or Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))).resolve()
    roots = [codex_dir / "sessions", codex_dir / "archived_sessions"]
    if source.suffix != ".jsonl" or not any(source.is_relative_to(root.resolve()) for root in roots):
        raise ValueError("历史模型记录路径不在本机 Codex session 目录内")
    result = {}
    verified = False
    with source.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                if not line.endswith("\n"):
                    break  # 正在写入的末行尚未完成，下一次查看时再读。
                raise ValueError("历史模型记录中有损坏的 JSON 行")
            payload = row.get("payload", {})
            if row.get("type") == "session_meta":
                if payload.get("id") != thread_id:
                    raise ValueError("历史文件与请求的 session ID 不一致")
                verified = True
            elif verified and row.get("type") == "turn_context" and payload.get("turn_id") in turn_ids:
                result[payload["turn_id"]] = {"model": str(payload.get("model") or "未记录"),
                                               "effort": str(payload.get("effort") or "未记录")}
    if not verified:
        raise ValueError("历史文件缺少匹配的 session 元数据")
    return result


class Sessions:
    def __init__(self, service: "BridgeService") -> None:
        self.service = service

    def connect(self, instance_id: str = "local"):
        return self.service.codex_connection(instance_id)

    def resolve(self, stream_id: str, user_id: str, target: str = "current", instance_id: str = "") -> str:
        service = self.service
        instance_id = instance_id or service.instances.selected(stream_id, user_id)
        if target in {"current", "desktop", "session", "last"}:
            if target != "last":
                bound = service.bindings.get(service.binding_key(stream_id, user_id, target, instance_id), "")
                if bound:
                    return bound
            if target in {"current", "last"}:
                jobs = [job for job in service.jobs.values() if job.stream_id == stream_id and job.user_id == user_id
                        and job.agent == "codex" and job.instance_id == instance_id and job.thread_id]
                if jobs:
                    return max(jobs, key=lambda job: job.created_at).thread_id
            raise ValueError("还没有对应的 session；先用 /agent run、/agent session 或 /agent bind")
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", target):
            raise ValueError("session ID 格式不正确")
        return target

    async def read(self, client: AppServer, stream_id: str, user_id: str, target: str, instance_id: str = "local") -> Dict[str, Any]:
        thread_id = self.resolve(stream_id, user_id, target, instance_id)
        own = any(job.agent == "codex" and job.instance_id == instance_id and job.stream_id == stream_id and job.user_id == user_id and job.thread_id == thread_id
                  for job in self.service.jobs.values())
        if not own and not self.service.instances.get(instance_id).allow_saved_threads:
            raise ValueError("已有 session 浏览未启用")
        response = await client.request("thread/read", {"threadId": thread_id, "includeTurns": False})
        thread = response["thread"]
        self.service.check_root(thread["cwd"], instance_id)
        # thread/read 不提供可靠的归档分区；只由 thread/list 更新索引，
        # 避免查看已归档聊天时把它错误记录成未归档。
        return thread

    def choices(self, stream_id: str, user_id: str, thread_id: str = "", instance_id: str = "") -> Dict[str, str]:
        service = self.service
        instance_id = instance_id or service.instances.selected(stream_id, user_id)
        instance = service.instances.get(instance_id)
        selected = {"model": instance.model, "effort": instance.reasoning_effort}
        selected.update(service.preferences.get(service.binding_key(stream_id, user_id, "default", instance_id), {}))
        if thread_id:
            selected.update(service.preferences.get(service.binding_key(stream_id, user_id, thread_id, instance_id), {}))
        return {key: value for key, value in selected.items() if value}

    @staticmethod
    def page_number(page: int) -> int:
        if not 1 <= page <= 100:
            raise ValueError("页码范围是 1–100")
        return page

    async def listing(self, page: int = 1, query: str = "", archived: bool = False, instance_id: str = "local") -> str:
        self.page_number(page)
        if not self.service.instances.get(instance_id).allow_saved_threads:
            raise ValueError("已有 session 浏览尚未启用")
        offset = (page - 1) * PAGE_SIZE
        threads: List[Dict[str, Any]] = []
        cursor = None
        async with self.connect(instance_id) as client:
            for _ in range(100):
                params = {"limit": 50, "sortKey": "updated_at", "sortDirection": "desc", "archived": archived}
                if query:
                    params["searchTerm"] = query
                if cursor:
                    params["cursor"] = cursor
                response = await client.request("thread/list", params)
                for thread in response.get("data", []):
                    if not thread.get("cwd"):
                        continue
                    try:
                        self.service.check_root(thread["cwd"], instance_id)
                    except ValueError:
                        continue
                    threads.append(thread)
                cursor = response.get("nextCursor")
                if len(threads) > offset + PAGE_SIZE or not cursor:
                    break
            else:
                raise ValueError("历史数量过多，请加关键词缩小搜索范围")
        await self.service.instances.cache(instance_id, threads, archived)
        selected = threads[offset:offset + PAGE_SIZE]
        if not selected:
            return f"session 第 {page} 页没有记录（只显示允许目录中的聊天）"
        lines = [f"实例 {instance_id} · {'已归档' if archived else '最近'} session · 第 {page} 页 · 按更新时间排序"]
        for thread in selected:
            title = str(thread.get("name") or thread.get("preview") or "未命名").replace("\n", " ")[:80]
            lines.append(f"{thread['id']}\n{title}\n{timestamp(thread.get('updatedAt'))} · "
                         f"{thread.get('model') or '模型未记录'} · effort={thread.get('reasoningEffort') or '未记录'}")
        if len(threads) > offset + PAGE_SIZE:
            archived_arg = "archived " if archived else ""
            lines.append(f"下一页：/agent sessions {archived_arg}{page + 1}" + (f" {query}" if query else ""))
        return "\n\n".join(lines)

    async def catalog(self, client: AppServer) -> List[Dict[str, Any]]:
        models = []
        cursor = None
        while True:
            params = {"limit": 100, "includeHidden": False}
            if cursor:
                params["cursor"] = cursor
            response = await client.request("model/list", params)
            models.extend(response.get("data", []))
            cursor = response.get("nextCursor")
            if not cursor:
                return models

    async def models(self, page: int = 1, instance_id: str = "local") -> str:
        self.page_number(page)
        async with self.connect(instance_id) as client:
            models = await self.catalog(client)
            config = (await client.request("config/read", {"includeLayers": False}))["config"]
        lines = [f"实例 {instance_id} 模型目录 · 第 {page} 页", f"CLI 默认：{config.get('model') or '自动'} / {config.get('model_reasoning_effort') or '自动'}"]
        for model in models[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]:
            efforts = ", ".join(option["reasoningEffort"] for option in model.get("supportedReasoningEfforts", [])) or "未提供"
            lines.append(f"{model.get('model') or model['id']}\neffort: {efforts}；建议值: {model.get('defaultReasoningEffort') or '未提供'}")
        if len(models) > page * PAGE_SIZE:
            lines.append(f"下一页：/agent models {page + 1}")
        lines.append("目录由该实例 Codex 返回；是否能调用以实际请求为准，目录外的模型名也可明确指定。")
        return "\n\n".join(lines)

    async def effective(self, client: AppServer, stream_id: str, user_id: str,
                        thread: Optional[Dict[str, Any]] = None, instance_id: str = "local") -> Dict[str, str]:
        choices = self.choices(stream_id, user_id, thread["id"] if thread else "", instance_id)
        config = (await client.request("config/read", {"includeLayers": False}))["config"]
        return {"model": choices.get("model") or (thread or {}).get("model") or config.get("model") or "自动",
                "effort": choices.get("effort") or (thread or {}).get("reasoningEffort") or config.get("model_reasoning_effort") or "自动"}

    async def info(self, stream_id: str, user_id: str, target: str = "current") -> str:
        instance_id = self.service.instances.selected(stream_id, user_id)
        async with self.connect(instance_id) as client:
            thread = None if target == "default" else await self.read(client, stream_id, user_id, target, instance_id)
            selected = await self.effective(client, stream_id, user_id, thread, instance_id)
        if thread is None:
            return f"实例 {instance_id} · 本 QQ 会话中你的新任务默认配置\n模型: {selected['model']}\neffort: {selected['effort']}"
        title = str(thread.get("name") or thread.get("preview") or "未命名")[:150]
        active = [job.id for job in self.service.jobs.values() if job.instance_id == instance_id and job.thread_id == thread["id"]
                  and job.stream_id == stream_id and job.user_id == user_id and job.state in {"queued", "working", "input_required"}]
        return (f"实例: {instance_id}\nsession: {thread['id']}\n{title}\n更新时间: {timestamp(thread.get('updatedAt'))}\n"
                f"历史最新模型: {thread.get('model') or '未记录'}\n历史最新 effort: {thread.get('reasoningEffort') or '未记录'}\n"
                f"服务商: {thread.get('modelProvider') or '未记录'}\n目录: {thread['cwd']}\n"
                f"下次插件调用: {selected['model']} / {selected['effort']}\n"
                f"本插件活动任务: {', '.join(active) if active else '无'}\n桌面进程的实时状态不由此连接提供。")

    async def history(self, stream_id: str, user_id: str, target: str = "current", page: int = 1) -> str:
        self.page_number(page)
        instance_id = self.service.instances.selected(stream_id, user_id)
        instance = self.service.instances.get(instance_id)
        async with self.connect(instance_id) as client:
            thread = await self.read(client, stream_id, user_id, target, instance_id)
            cursor = None
            response = {}
            for number in range(1, page + 1):
                params = {"threadId": thread["id"], "limit": HISTORY_SIZE, "sortDirection": "desc", "itemsView": "full"}
                if cursor:
                    params["cursor"] = cursor
                response = await client.request("thread/turns/list", params)
                cursor = response.get("nextCursor")
                if number < page and not cursor:
                    return f"session {thread['id']} 没有第 {page} 页聊天记录"
        turns = response.get("data", [])
        settings = {}
        metadata_note = ""
        if instance.transport == "stdio" and thread.get("path") and turns:
            try:
                codex_dir = Path(instance.codex_home).expanduser() if instance.codex_home else None
                settings = await asyncio.to_thread(read_turn_settings, thread["path"], thread["id"], {turn['id'] for turn in turns}, codex_dir)
            except (OSError, ValueError) as exc:
                metadata_note = f"每轮模型元数据未取得: {exc}"
        elif instance.transport != "stdio":
            metadata_note = "远端每轮模型/effort 仅展示服务端提供的字段；不读取远端返回路径对应的本机文件。"
        lines = [f"实例 {instance_id} · session {thread['id']} · 聊天第 {page} 页（每页最近 {HISTORY_SIZE} 轮，页内按时间正序）"]
        for turn in reversed(turns):
            model = settings.get(turn["id"], {"model": turn.get("model") or "未记录", "effort": turn.get("reasoningEffort") or turn.get("effort") or "未记录"})
            lines.append(f"[{timestamp(turn.get('startedAt'))}] 模型={model.get('model', '未记录')} / effort={model.get('effort', '未记录')}")
            for item in turn.get("items", []):
                if item.get("type") == "userMessage":
                    content = []
                    for part in item.get("content", []):
                        if part.get("type") == "text":
                            content.append(part.get("text", ""))
                        else:
                            content.append("[附件]")
                    text = "\n".join(content)
                    label = "你"
                elif item.get("type") == "agentMessage":
                    text = item.get("text", "")
                    label = "Codex 进度" if item.get("phase") == "commentary" else "Codex"
                else:
                    continue  # 不把 reasoning、命令输出、系统提示当作对话正文发送。
                # QQ 展示窗口有限，明确标注截取，避免把局部内容当作完整历史。
                lines.append(f"{label}: {text[:2200]}" + ("\n[该条过长，已截取前 2200 字]" if len(text) > 2200 else ""))
        if not turns:
            lines.append("暂无聊天记录")
        if metadata_note:
            lines.append(metadata_note)
        if cursor:
            lines.append(f"更早记录：/agent history {thread['id']} {page + 1}")
        return "\n\n".join(lines)

    async def set_options(self, stream_id: str, user_id: str, target: str = "current",
                          model: Optional[str] = None, effort: Optional[str] = None) -> str:
        if model is not None and model != "inherit" and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}", model):
            raise ValueError("模型名格式不正确")
        if effort is not None and effort not in EFFORTS | {"inherit"}:
            raise ValueError("effort 可用 " + ", ".join(sorted(EFFORTS)) + " 或 inherit")
        instance_id = self.service.instances.selected(stream_id, user_id)
        instance = self.service.instances.get(instance_id)
        async with self.connect(instance_id) as client:
            thread = None if target == "default" else await self.read(client, stream_id, user_id, target, instance_id)
            key = self.service.binding_key(stream_id, user_id, thread["id"] if thread else "default", instance_id)
            proposed = dict(self.service.preferences.get(key, {}))
            for name, value in [("model", model), ("effort", effort)]:
                if value == "inherit":
                    proposed.pop(name, None)
                elif value is not None:
                    proposed[name] = value
            models = await self.catalog(client)
            # 不先修改内存或磁盘；验证失败时保留原配置。
            config_choices = {"model": instance.model, "effort": instance.reasoning_effort}
            if thread:
                config_choices.update(self.service.preferences.get(self.service.binding_key(stream_id, user_id, "default", instance_id), {}))
            config_choices.update(proposed)
            cli = (await client.request("config/read", {"includeLayers": False}))["config"]
            selected = {"model": config_choices.get("model") or (thread or {}).get("model") or cli.get("model") or "自动",
                        "effort": config_choices.get("effort") or (thread or {}).get("reasoningEffort") or cli.get("model_reasoning_effort") or "自动"}
            entry = next((item for item in models if selected["model"] in {item.get("id"), item.get("model")}), None)
            if entry is not None:
                supported = [item["reasoningEffort"] for item in entry.get("supportedReasoningEfforts", [])]
                if supported and selected["effort"] != "自动" and selected["effort"] not in supported:
                    raise ValueError(f"{selected['model']} 不支持 effort={selected['effort']}；可用 {', '.join(supported)}。可同时指定模型和 effort。")
        self.service.preferences[key] = proposed
        await self.service.save()
        scope = f"session {thread['id']}" if thread else "本 QQ 会话中你的新任务默认值"
        note = "\n该模型不在此实例目录中，调用支持情况以服务端结果为准。" if entry is None else ""
        return f"实例 {instance_id} · 已设置 {scope}\n下次插件调用: {selected['model']} / {selected['effort']}\n已经提交的任务保留原配置。{note}"
