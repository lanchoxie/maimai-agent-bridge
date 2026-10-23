"""后台任务生命周期、会话隔离和持久化。"""

from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List
from urllib.parse import urlsplit

import asyncio
import json
import re
import uuid

from .artifacts import Artifacts
from .config import BridgeConfig
from .instances import Instances
from .remote import RemoteClient, extract_text
from .rpc import AppServer, find_codex
from .sessions import EFFORTS, Sessions

ACTIVE = {"queued", "working", "input_required"}


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def redact(text: str) -> str:
    return re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "[API_KEY]", str(text))


@dataclass
class Job:
    id: str
    stream_id: str
    user_id: str
    agent: str
    mode: str
    prompt: str
    state: str = "queued"
    progress: str = "排队中"
    thread_id: str = ""
    turn_id: str = ""
    remote_task_id: str = ""
    context_id: str = ""
    result: str = ""
    error: str = ""
    created_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)
    notification_error: str = ""
    model: str = ""
    effort: str = ""
    instance_id: str = "local"
    last_checked_at: str = ""
    workspace: str = ""
    artifacts: List[Dict[str, Any]] = field(default_factory=list)
    artifact_error: str = ""


class BridgeService:
    def __init__(self, config: BridgeConfig, data_dir: Path,
                 notify: Callable[[str, str], Awaitable[Any]], logger: Any,
                 send_file: Callable[[Dict[str, Any], str], Awaitable[Any]] | None = None):
        self.config = config
        self.data_dir = data_dir
        self.notify = notify
        self.send_file = send_file
        self.logger = logger
        self.jobs: Dict[str, Job] = {}
        self.bindings: Dict[str, str] = {}
        self.preferences: Dict[str, Dict[str, str]] = {}
        self.instances = Instances(self)
        self.sessions = Sessions(self)
        self.artifacts = Artifacts(self)
        self.tasks: Dict[str, asyncio.Task] = {}
        self.starting_jobs: set[str] = set()
        self.connections: Dict[str, Any] = {}
        self.questions: Dict[str, Dict[str, Any]] = {}
        self.answers: Dict[str, asyncio.Future] = {}
        self.save_lock = asyncio.Lock()
        self.semaphore = asyncio.Semaphore(config.codex.max_concurrent)
        self.stopping = False

    @property
    def workspace(self) -> Path:
        return Path(self.config.codex.workspace).resolve() if self.config.codex.workspace else (self.data_dir / "workspace").resolve()

    async def load(self) -> None:
        if self.config.codex.reasoning_effort and self.config.codex.reasoning_effort not in EFFORTS:
            raise ValueError("codex.reasoning_effort 不是有效 effort 等级")
        names = set()
        for agent in self.config.remote.agents:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", agent.name) or agent.name == "codex" or agent.name in names:
                raise ValueError("远程 Agent 名称须为 1–40 位英文、数字、短横线或下划线，不得重复或使用 codex")
            names.add(agent.name)
            url = urlsplit(agent.endpoint)
            if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
                raise ValueError(f"远程 Agent {agent.name} 需要没有内嵌密码的 HTTP/HTTPS endpoint")
        def read():
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.workspace.mkdir(parents=True, exist_ok=True)
            path = self.data_dir / "state.json"
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        state = await asyncio.to_thread(read)
        self.bindings = state.get("bindings", {})
        self.preferences = state.get("preferences", {})
        self.instances.selections = state.get("instance_selections", {})
        await self.instances.load()
        for entry in state.get("jobs", []):
            job = Job(**entry)
            for artifact in job.artifacts:
                if artifact["state"] == "sending":
                    artifact["state"] = "unknown"
                    artifact["error"] = "插件重启，上传回执未知；请先在 QQ 核实，避免重复上传"
            if job.state in ACTIVE:
                # 重启后不重发任务，防止执行两次。保留 ID，用户可查进度或明确续聊。
                job.state = "interrupted"
                job.progress = "插件重启，任务监视已中断；请查验执行结果后决定是否继续"
                job.updated_at = now()
            self.jobs[job.id] = job
        await self.save()

    @staticmethod
    def _owner_jobs(jobs: Dict[str, Job], stream_id: str, user_id: str) -> List[Job]:
        """返回某个真实 QQ 来源的活动任务，任务状态以持久化记录为准。"""

        return [
            job for job in jobs.values()
            if job.stream_id == stream_id and job.user_id == user_id and job.state in ACTIVE
        ]

    async def refresh_active_sessions(self, stream_id: str, user_id: str) -> List[Job]:
        """刷新当前 QQ 来源的活动 session 状态并释放失去监视器的任务名额。

        插件任务由 ``self.tasks`` 持续监视。若持久化记录仍是活动状态但后台
        监视协程已经不存在（例如异常退出后恢复），不能继续占用活动 session
        名额；将其标为 interrupted，保留 thread_id 供用户用 history 检查。
        """

        changed = False
        active_jobs = self._owner_jobs(self.jobs, stream_id, user_id)
        for job in active_jobs:
            task = self.tasks.get(job.id)
            if task is not None and task.done():
                await asyncio.gather(task, return_exceptions=True)
            elif task is None:
                if job.id in self.starting_jobs:
                    continue
                job.state = "interrupted"
                job.progress = "后台监视已结束，任务状态未知；请用 history 检查该 session 后再继续"
                job.updated_at = now()
                changed = True
            job.last_checked_at = now()
        if active_jobs:
            changed = True
        if changed:
            await self.save()
        return self._owner_jobs(self.jobs, stream_id, user_id)

    async def _check_active_session_limit(self, stream_id: str, user_id: str) -> None:
        """提交前刷新状态，并限制单个 QQ 来源的活动 session 数。"""

        active_jobs = await self.refresh_active_sessions(stream_id, user_id)
        limit = self.config.codex.max_active_sessions
        if len(active_jobs) >= limit:
            details = ", ".join(f"{job.id}({job.state})" for job in active_jobs[:limit])
            raise ValueError(
                f"当前最多同时保留 {limit} 个活动 session，已有：{details}；"
                f"请先使用 /agent cancel 任务ID 关闭任务，或等待任务完成后再提交"
            )

    async def save(self) -> None:
        async with self.save_lock:
            values = list(self.jobs.values())
            finished = [job for job in values if job.state not in ACTIVE][-100:]
            active = [job for job in values if job.state in ACTIVE]
            self.jobs = {job.id: job for job in finished + active}
            payload = json.dumps({"bindings": self.bindings, "preferences": self.preferences,
                                  "instance_selections": self.instances.selections,
                                  "jobs": [asdict(job) for job in self.jobs.values()]}, ensure_ascii=False, indent=2)
            def write():
                path = self.data_dir / "state.json"
                temporary = self.data_dir / "state.json.tmp"
                temporary.write_text(payload, encoding="utf-8")
                temporary.replace(path)
            writing = asyncio.create_task(asyncio.to_thread(write))
            try:
                await asyncio.shield(writing)
            except asyncio.CancelledError:
                # 等磁盘写入结束后再释放锁，避免下一次写入竞争同一个临时文件。
                await writing
                raise

    @staticmethod
    def binding_key(stream_id: str, user_id: str, mode: str = "desktop", instance_id: str = "local") -> str:
        prefix = "" if instance_id == "local" else f"@{instance_id}:"
        return f"{stream_id}:{user_id}:{prefix}{mode}"

    @asynccontextmanager
    async def codex_connection(self, instance_id: str = "local"):
        # 文件查找、环境准备等工作放在线程中；RPC 始终运行在当前事件循环。
        if instance_id == "local":
            executable = await asyncio.to_thread(find_codex, self.config.codex.executable)
            client = AppServer(executable)
        else:
            client = await asyncio.to_thread(self.instances.connect, instance_id, AppServer)
        async with client:
            yield client

    def require_scope(self, stream_id: str, user_id: str, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if job is None or job.stream_id != stream_id or job.user_id != user_id:
            raise ValueError("此会话中没有这个属于你的任务")
        return job

    def check_root(self, directory: str, instance_id: str = "local"):
        return self.instances.check_root(directory, instance_id)

    async def list_saved_threads(self) -> str:
        return redact(await self.sessions.listing())

    async def bind(self, stream_id: str, user_id: str, thread_id: str) -> str:
        instance_id = self.instances.selected(stream_id, user_id)
        if not self.instances.get(instance_id).allow_saved_threads:
            raise ValueError("已有聊天续聊尚未启用；需在本地打开 codex.allow_saved_threads")
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", thread_id):
            raise ValueError("聊天 ID 格式不正确")
        async with self.codex_connection(instance_id) as client:
            response = await client.request("thread/read", {"threadId": thread_id, "includeTurns": False})
            thread = response["thread"]
            self.check_root(thread["cwd"], instance_id)
            if thread.get("status", {}).get("type") == "active":
                raise ValueError("该聊天正在执行任务，空闲后再绑定")
        self.bindings[self.binding_key(stream_id, user_id, instance_id=instance_id)] = thread_id
        self.bindings[self.binding_key(stream_id, user_id, "current", instance_id)] = thread_id
        await self.save()
        return str(thread.get("name") or thread.get("preview") or thread_id)[:120]

    async def unbind(self, stream_id: str, user_id: str) -> None:
        instance_id = self.instances.selected(stream_id, user_id)
        old = self.bindings.pop(self.binding_key(stream_id, user_id, instance_id=instance_id), None)
        current_key = self.binding_key(stream_id, user_id, "current", instance_id)
        if self.bindings.get(current_key) == old:
            self.bindings.pop(current_key, None)
        await self.save()

    async def submit(self, stream_id: str, user_id: str, prompt: str,
                     agent: str = "codex", mode: str = "independent", previous_id: str = "") -> Job:
        if self.stopping:
            raise RuntimeError("插件正在停止，暂不接受任务")
        if not prompt.strip() or len(prompt) > 20000:
            raise ValueError("任务内容不能为空，且不能超过 20000 字")
        if mode not in {"independent", "desktop", "session"}:
            raise ValueError("mode 可用 independent、session、desktop")
        if agent != "codex" and agent not in {item.name for item in self.config.remote.agents}:
            raise ValueError("未配置这个 Agent")
        await self._check_active_session_limit(stream_id, user_id)
        if sum(job.state in ACTIVE for job in self.jobs.values()) >= self.config.codex.max_queued:
            raise ValueError("任务队列已满，请等现有任务完成")
        thread_id = ""
        context_id = ""
        instance_id = "local"
        if agent == "codex" and not previous_id:
            instance_id = self.instances.selected(stream_id, user_id)
        if previous_id:
            previous = self.require_scope(stream_id, user_id, previous_id)
            if previous.state in ACTIVE:
                raise ValueError("任务仍在执行或等待输入，请使用 reply/cancel")
            agent, mode = previous.agent, previous.mode
            instance_id = previous.instance_id
            thread_id, context_id = previous.thread_id, previous.context_id
        elif mode == "desktop":
            if not self.instances.get(instance_id).allow_saved_threads:
                raise ValueError("已有桌面聊天续聊未启用")
            thread_id = self.bindings.get(self.binding_key(stream_id, user_id, instance_id=instance_id), "")
            if not thread_id:
                raise ValueError("先使用 /agent bind 聊天ID 明确绑定已有聊天")
        elif mode == "session":
            thread_id = self.bindings.get(self.binding_key(stream_id, user_id, "session", instance_id), "")
        if mode == "desktop" and not self.instances.get(instance_id).allow_saved_threads:
            raise ValueError("已有桌面聊天续聊未启用")
        if agent != "codex" and (agent not in {item.name for item in self.config.remote.agents} or mode != "independent"):
            raise ValueError("远端 Agent 必须已配置，并使用 independent 模式")
        if thread_id and any(job.agent == agent and job.instance_id == instance_id and job.thread_id == thread_id and job.state in ACTIVE for job in self.jobs.values()):
            raise ValueError("这段会话已有任务在执行，请等待完成")
        if mode == "session" and any(job.instance_id == instance_id and job.mode == mode and job.stream_id == stream_id and job.user_id == user_id and job.state in ACTIVE for job in self.jobs.values()):
            raise ValueError("这个长期会话已有任务在执行")
        job = Job(id=uuid.uuid4().hex[:12], stream_id=stream_id, user_id=user_id,
                  agent=agent, mode=mode, prompt=prompt, thread_id=thread_id, context_id=context_id, instance_id=instance_id)
        if agent == "codex":
            self.instances.get(instance_id)
            selected = self.sessions.choices(stream_id, user_id, thread_id, instance_id)
            job.model = selected.get("model", "")
            job.effort = selected.get("effort", "")
        self.jobs[job.id] = job
        self.starting_jobs.add(job.id)
        try:
            await self.save()
            self.tasks[job.id] = asyncio.create_task(self._run(job))
        finally:
            self.starting_jobs.discard(job.id)
        return job

    async def update(self, job: Job, state: str = "", progress: str = "") -> None:
        if state:
            job.state = state
        if progress:
            job.progress = self.instances.scrub(progress)[:2000]
        job.updated_at = now()
        job.last_checked_at = now()
        await self.save()

    async def _run(self, job: Job) -> None:
        try:
            async with self.semaphore:
                await self.update(job, "working", "正在连接 Agent")
                async with asyncio.timeout(self.config.codex.task_timeout_seconds):
                    if job.agent == "codex":
                        await self._run_codex(job)
                    else:
                        await self._run_remote(job)
                await self.update(job, "completed", "执行完成")
        except asyncio.CancelledError:
            state = "interrupted" if self.stopping else "canceled"
            await self.update(job, state, "插件停止，已中断" if self.stopping else "用户已取消")
        except TimeoutError:
            job.error = f"任务超过 {self.config.codex.task_timeout_seconds} 秒的执行期限"
            await self.update(job, "failed", job.error)
        except Exception as exc:
            job.error = self.instances.scrub(str(exc))[:4000]
            self.logger.error(f"Agent 任务 {job.id} 失败: {job.error}")
            await self.update(job, "failed", job.error)
        finally:
            self.connections.pop(job.id, None)
            self.questions.pop(job.id, None)
            self.answers.pop(job.id, None)
        try:
            if not self.stopping:
                if job.state == "completed" and job.artifacts and self.config.files.auto_send:
                    try:
                        await self.artifacts.send(job, automatic=True)
                    except Exception as exc:
                        job.artifact_error = self.instances.scrub(str(exc))[:1000]
                        await self.save()
                await self.deliver(job, self.format_job(job))
        finally:
            self.tasks.pop(job.id, None)

    async def deliver(self, job: Job, text: str) -> None:
        try:
            clean = self.instances.scrub(text)
            for start in range(0, len(clean), 2800):
                sent = await self.notify(clean[start:start + 2800], job.stream_id)
                if sent is False or isinstance(sent, dict) and not sent.get("sent", False):
                    raise RuntimeError("QQ 消息发送返回失败")
        except Exception as exc:
            job.notification_error = self.instances.scrub(str(exc))[:1000]
            self.logger.error(f"任务 {job.id} 结果回传失败，结果仍可查询: {job.notification_error}")
            await self.save()

    async def _run_codex(self, job: Job) -> None:
        instance = self.instances.get(job.instance_id)
        workspace = instance.workspace
        if instance.transport == "stdio":
            await asyncio.to_thread(Path(workspace).mkdir, parents=True, exist_ok=True)
        async with self.codex_connection(job.instance_id) as client:
            self.connections[job.id] = client
            options: Dict[str, Any] = {"approvalPolicy": "never", "sandbox": "read-only"}
            options["sandbox"] = self.config.codex.sandbox
            if job.thread_id:
                # thread/resume 接的是已保存的会话；不会劫持桌面进程的 stdio。
                info = await client.request("thread/read", {"threadId": job.thread_id, "includeTurns": False})
                self.check_root(info["thread"]["cwd"], job.instance_id)
                if info["thread"].get("status", {}).get("type") == "active":
                    raise ValueError("该聊天正在执行任务")
                job.model = job.model or info["thread"].get("model") or ""
                job.effort = job.effort or info["thread"].get("reasoningEffort") or ""
            if job.model:
                options["model"] = job.model
            if job.effort:
                options["config"] = {"model_reasoning_effort": job.effort}
            if job.thread_id:
                response = await client.request("thread/resume", {"threadId": job.thread_id, "excludeTurns": True, **options})
            else:
                response = await client.request("thread/start", {"cwd": workspace, **options})
                job.thread_id = response["thread"]["id"]
                if job.mode == "session":
                    self.bindings[self.binding_key(job.stream_id, job.user_id, "session", job.instance_id)] = job.thread_id
            job.model = response.get("model") or response["thread"].get("model") or job.model
            job.effort = response.get("reasoningEffort") or response["thread"].get("reasoningEffort") or job.effort
            job.workspace = str(self.check_root(response["thread"]["cwd"], job.instance_id))
            self.bindings[self.binding_key(job.stream_id, job.user_id, "current", job.instance_id)] = job.thread_id
            await self.update(job, progress="Codex 已接收任务")
            turn_params = {
                "threadId": job.thread_id, "input": [{"type": "text", "text": job.prompt + (
                    self.artifacts.prompt() if instance.transport == "stdio" else ""
                )}],
                "approvalPolicy": "never",
                "sandboxPolicy": ({"type": "readOnly"} if self.config.codex.sandbox == "read-only" else {
                    "type": "workspaceWrite", "writableRoots": [response["thread"]["cwd"]], "networkAccess": False,
                }),
            }
            if job.model:
                turn_params["model"] = job.model
            if job.effort:
                turn_params["effort"] = job.effort
            response = await client.request("turn/start", turn_params)
            job.turn_id = response["turn"]["id"]
            await self.save()
            messages: Dict[str, str] = {}
            try:
                while True:
                    event = await client.events.get()
                    method, params = event.get("method", ""), event.get("params", {})
                    if method == "bridge/disconnected":
                        raise ConnectionError(params["message"])
                    if params.get("threadId") not in {None, job.thread_id}:
                        continue
                    if "id" in event:
                        await self._ask_codex(job, event)
                    elif method == "item/completed":
                        item = params.get("item", {})
                        if item.get("type") == "agentMessage":
                            if item.get("phase") == "commentary":
                                await self.update(job, progress=item.get("text", ""))
                            else:
                                messages[item["id"]] = item.get("text", "")
                        elif item.get("type") in {"commandExecution", "fileChange", "mcpToolCall", "webSearch"}:
                            await self.update(job, progress=f"Codex: {item['type']} {item.get('status', '')}")
                    elif method == "turn/completed" and params.get("turn", {}).get("id") == job.turn_id:
                        turn = params["turn"]
                        if turn.get("status") != "completed":
                            raise RuntimeError(str(turn.get("error") or turn.get("status")))
                        job.result = self.instances.scrub("\n\n".join(messages.values()))[-30000:]
                        if instance.transport == "stdio":
                            self.artifacts.collect(job)
                        if not job.result and not job.artifacts:
                            raise RuntimeError("Codex 已结束，但没有返回最终文本")
                        recorded = (await client.request("thread/read", {"threadId": job.thread_id, "includeTurns": False}))["thread"]
                        job.model = recorded.get("model") or job.model
                        job.effort = recorded.get("reasoningEffort") or job.effort
                        return
                    elif method == "error":
                        await self.update(job, progress=str(params.get("error", {})))
            except BaseException:
                if job.turn_id:
                    try:
                        await client.request("turn/interrupt", {"threadId": job.thread_id, "turnId": job.turn_id}, timeout=5)
                    except Exception:
                        self.logger.warning(f"任务 {job.id} 的中断确认未收到，请在 Codex 中查验")
                raise

    async def _ask_codex(self, job: Job, event: Dict[str, Any]) -> None:
        method = event["method"]
        if method not in {"item/tool/requestUserInput", "item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
            await self.connections[job.id].send({"id": event["id"], "error": {"code": -32601, "message": "桥接插件不支持这个交互"}})
            return
        self.questions[job.id] = event
        await self.update(job, "input_required", json.dumps(event.get("params", {}), ensure_ascii=False)[:1800])
        await self.deliver(job, f"Agent 任务 {job.id} 等待输入：\n{job.progress}\n使用 /agent reply {job.id} 你的回答；审批使用 /agent approve {job.id} approve 或 deny")

    async def reply(self, job: Job, text: str, approval: bool = False) -> None:
        if job.state != "input_required":
            raise ValueError("任务当前没有等待输入")
        if job.agent != "codex":
            self.answers[job.id].set_result(text)
        else:
            event = self.questions[job.id]
            if event["method"].endswith("requestApproval"):
                if not approval or text not in {"approve", "deny"}:
                    raise ValueError("需要本人通过 /agent approve 任务ID approve|deny 回答审批")
                result = {"decision": "accept" if text == "approve" else "decline"}
            else:
                questions = event.get("params", {}).get("questions", [])
                if len(questions) == 1:
                    answers = {questions[0]["id"]: {"answers": [text]}}
                else:
                    raw_answers = json.loads(text)
                    answers = {question["id"]: {"answers": [str(raw_answers[question["id"]])]} for question in questions}
                result = {"answers": answers}
            await self.connections[job.id].send({"id": event["id"], "result": result})
            self.questions.pop(job.id, None)
        await self.update(job, "working", "已提交回答")

    async def _run_remote(self, job: Job) -> None:
        config = next(item for item in self.config.remote.agents if item.name == job.agent)
        client = await asyncio.to_thread(RemoteClient, config)
        self.connections[job.id] = client
        try:
            payload = await client.send(job.prompt, job.context_id)
            while True:
                job.context_id = payload.get("contextId", job.context_id)
                if "status" not in payload:
                    job.result = extract_text(payload)
                    return
                job.remote_task_id = payload["id"]
                state = payload.get("status", {}).get("state", "").lower().removeprefix("task_state_").replace("-", "_")
                await self.update(job, progress=f"{job.agent}: {state}")
                if state == "completed":
                    job.result = extract_text(payload)
                    return
                if state in {"failed", "canceled", "rejected", "auth_required"}:
                    raise RuntimeError(f"远端 {state}: {extract_text(payload)}")
                if state not in {"submitted", "working", "input_required"}:
                    raise RuntimeError(f"远端返回未知任务状态: {state}")
                if state == "input_required":
                    self.answers[job.id] = asyncio.get_running_loop().create_future()
                    await self.update(job, "input_required", extract_text(payload))
                    await self.deliver(job, f"Agent 任务 {job.id} 需要补充：{job.progress}\n/agent reply {job.id} 你的回答")
                    answer = await self.answers[job.id]
                    payload = await client.send(answer, job.context_id, job.remote_task_id)
                else:
                    await asyncio.sleep(3)
                    payload = await client.get(job.remote_task_id)
        except asyncio.CancelledError:
            if job.remote_task_id:
                try:
                    await client.cancel(job.remote_task_id)
                except Exception:
                    job.error = "远端未确认取消，请到远端查验任务状态"
            raise
        finally:
            await client.close()

    async def cancel(self, job: Job) -> None:
        task = self.tasks.get(job.id)
        if task is None or task.done():
            raise ValueError("任务已结束，不能取消")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        # create_task 后马上取消时，协程可能尚未进入 try/finally。
        if job.state in ACTIVE:
            await self.update(job, "canceled", "用户已取消")
            self.tasks.pop(job.id, None)

    async def close(self) -> None:
        self.stopping = True
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for job in list(self.jobs.values()):
            if job.state in ACTIVE:
                await self.update(job, "interrupted", "插件停止，已中断")
        self.tasks.clear()

    @staticmethod
    def format_job(job: Job) -> str:
        text = f"Agent 任务 {job.id} · {job.agent} · {job.state}\n{job.progress}"
        if job.agent == "codex":
            text += f"\n实例: {job.instance_id}"
        if job.thread_id:
            text += f"\nsession: {job.thread_id}"
        if job.model or job.effort:
            text += f"\n模型: {job.model or '自动'} / effort: {job.effort or '自动'}"
        if job.result:
            text += "\n" + job.result
        if job.error and job.error != job.progress:
            text += "\n" + job.error
        if job.artifacts or job.artifact_error:
            text += "\n" + Artifacts.listing(job)
        if job.notification_error:
            text += "\n文字回传失败：" + job.notification_error
        return text
