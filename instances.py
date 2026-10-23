"""Codex 实例发现、固定配置连接和不含凭据的 session 索引。"""

from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any, Dict, List
from urllib.parse import urlsplit

import asyncio
import hashlib
import ipaddress
import json
import ntpath
import os
import posixpath
import re
import shlex
import shutil
import uuid

from .config import CodexInstance
from .rpc import AppServer, find_codex

if TYPE_CHECKING:
    from .service import BridgeService

IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,40}")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def discover(scan_roots: List[str]) -> List[Dict[str, str]]:
    """只枚举常见安装位置和 Conda 已登记环境，不执行扫描到的程序。"""
    candidates = []
    prefixes: Dict[str, str] = {}
    user_dir = Path.home()
    for value in scan_roots:
        prefixes[str(Path(value).expanduser().resolve())] = "配置的扫描目录"
    for name in ("miniconda3", "anaconda3", "miniforge3", "mambaforge", ".conda"):
        root = user_dir / name
        if root.is_dir():
            prefixes[str(root)] = "Conda 常见目录"
    registered = user_dir / ".conda" / "environments.txt"
    if registered.is_file():
        for value in registered.read_text(encoding="utf-8").splitlines()[:200]:
            if value.strip():
                prefixes[str(Path(value.strip()).expanduser().resolve())] = "Conda 登记环境"
    if os.environ.get("CONDA_PREFIX"):
        prefixes[str(Path(os.environ["CONDA_PREFIX"]).resolve())] = "当前 Conda 环境"
    conda = os.environ.get("CONDA_EXE") or shutil.which("conda")
    if conda:
        root = Path(conda).resolve().parent.parent
        if root.is_dir():
            prefixes[str(root)] = "Conda 可执行文件所在环境"
    # PATH 上的每个安装分别记录，避免只找出第一个 Codex。
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if directory:
            prefixes.setdefault(str(Path(directory).resolve()), "PATH")
    if os.name == "nt":
        prefixes.setdefault(str(user_dir / "AppData/Roaming/npm"), "npm 全局安装")
        bundle = user_dir / "AppData/Local/OpenAI/Codex/bin"
        if bundle.is_dir():
            for executable in list(bundle.glob("*/codex.exe"))[:50]:
                candidates.append((executable, "", "桌面应用附带 CLI"))
    else:
        for root in (user_dir / ".local/bin", Path("/usr/local/bin"), Path("/opt/conda")):
            prefixes.setdefault(str(root), "常见安装目录")
    for value, source in list(prefixes.items()):
        root = Path(value)
        envs = root / "envs"
        if envs.is_dir():
            for prefix in list(envs.iterdir())[:200]:
                if prefix.is_dir():
                    prefixes.setdefault(str(prefix), "Conda envs 环境")
    patterns = ("codex.exe", "bin/codex", "bin/codex.exe", "Scripts/codex.exe",
                "node_modules/@openai/codex/node_modules/@openai/codex-*/vendor/*/bin/codex.exe",
                "node_modules/@openai/codex/vendor/*/bin/codex.exe",
                "lib/node_modules/@openai/codex/node_modules/@openai/codex-*/vendor/*/bin/codex",
                "node_modules/@openai/codex/node_modules/@openai/codex-*/vendor/*/bin/codex")
    for value, source in list(prefixes.items())[:500]:
        root = Path(value)
        if root.is_file() and (root.name == "codex.exe" or os.name != "nt" and root.name == "codex"):
            candidates.append((root, "", source))
            continue
        if not root.is_dir():
            continue
        prefix = str(root) if (root / "conda-meta").is_dir() else ""
        for pattern in patterns:
            for executable in list(root.glob(pattern))[:20]:
                if executable.is_file():
                    candidates.append((executable, prefix, source))
    result = {}
    for executable, prefix, source in candidates:
        absolute = str(executable.resolve())
        key = os.path.normcase(absolute)
        instance_id = "scan-" + hashlib.sha256(key.encode()).hexdigest()[:12]
        result[instance_id] = {"id": instance_id, "executable": absolute,
                               "conda_prefix": prefix, "source": source}
    return sorted(result.values(), key=lambda item: item["executable"])


class Instances:
    def __init__(self, service: "BridgeService") -> None:
        self.service = service
        self.discovered: Dict[str, CodexInstance] = {}
        self.selections: Dict[str, str] = {}
        self.index_lock = asyncio.Lock()

    @property
    def buffer_dir(self) -> Path:
        configured = self.service.config.instances.buffer_dir
        return Path(configured).expanduser().resolve() if configured else self.service.data_dir / "buffer"

    def get(self, instance_id: str = "local") -> CodexInstance:
        base = self.service.config.codex
        if instance_id == "local":
            return CodexInstance(id="local", executable=base.executable, workspace=str(self.service.workspace),
                                 allowed_roots=base.allowed_roots, allow_saved_threads=base.allow_saved_threads,
                                 model=base.model, reasoning_effort=base.reasoning_effort)
        for item in self.service.config.instances.configured:
            if item.id == instance_id:
                if item.transport == "stdio" and not item.workspace:
                    return item.model_copy(update={"workspace": str((self.service.data_dir / "workspace" / item.id).resolve())})
                return item
        if instance_id in self.discovered:
            # 扫描只提供安装位置；文件访问权限仍继承管理员设置。
            return self.discovered[instance_id].model_copy(update={
                "workspace": str(self.service.workspace), "allowed_roots": base.allowed_roots,
                "allow_saved_threads": base.allow_saved_threads,
            })
        raise ValueError("未知 Codex 实例；使用 /agent instances 或先 /agent scan")

    def selected(self, stream_id: str, user_id: str) -> str:
        instance_id = self.selections.get(self.service.binding_key(stream_id, user_id, "instance"), "local")
        self.get(instance_id)  # 配置被删除时明确报错，不悄悄改用别的机器。
        return instance_id

    def validate(self) -> None:
        names = {"local"}
        for item in self.service.config.instances.configured:
            if not IDENTIFIER.fullmatch(item.id) or item.id in names or item.id.startswith("scan-"):
                raise ValueError("Codex 实例 ID 不可重复，local 和 scan- 前缀保留")
            names.add(item.id)
            if item.reasoning_effort:
                from .sessions import EFFORTS
                if item.reasoning_effort not in EFFORTS:
                    raise ValueError(f"实例 {item.id} 的 reasoning_effort 不正确")
            for value in (item.token_env, item.api_key_env):
                if value and not ENV_NAME.fullmatch(value):
                    raise ValueError(f"实例 {item.id} 的密钥字段应填写环境变量名")
            for ref in item.environment:
                if not ENV_NAME.fullmatch(ref.name) or not ENV_NAME.fullmatch(ref.source_env):
                    raise ValueError("environment 的 name/source_env 必须是环境变量名")
                if ref.name.upper() in {"HOME", "USERPROFILE", "CODEX_HOME"}:
                    raise ValueError("配置目录请用 codex_home，environment 不允许覆盖 HOME/USERPROFILE/CODEX_HOME")
            if item.api_base_url:
                self.validate_url(item.api_base_url, {"http", "https"})
                if not item.api_key_env:
                    raise ValueError("配置 api_base_url 时必须指定 api_key_env")
            if item.transport != "stdio":
                if not item.workspace:
                    raise ValueError(f"远程实例 {item.id} 必须配置目标机器的 workspace")
                self.remote_path(item.workspace)
                for value in item.allowed_roots:
                    self.remote_path(value)
                if item.environment:
                    raise ValueError("environment 仅用于本地进程，远程环境变量请在服务器设置")
            if item.transport == "ssh":
                if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@:\[\]-]{0,250}", item.ssh_target):
                    raise ValueError("ssh_target 需要 user@host 或 SSH Host 别名")
                if "\\" in item.workspace or re.match(r"^[A-Za-z]:", item.workspace):
                    raise ValueError("SSH 启动目前支持 POSIX 服务器；Windows 远端可配置 WebSocket")
            if item.transport == "websocket":
                parsed = self.validate_url(item.endpoint, {"ws", "wss"})
                try:
                    loopback = ipaddress.ip_address(parsed.hostname).is_loopback
                except ValueError:
                    loopback = parsed.hostname == "localhost"
                if parsed.scheme == "ws" and not loopback:
                    raise ValueError("远端 WebSocket 使用 wss://，ws:// 仅允许本机或 SSH 转发的地址")
                if any((item.api_base_url, item.api_key_env, item.codex_home,
                        item.executable, item.conda_prefix, item.conda_executable)):
                    raise ValueError("WebSocket 的 CLI/Conda/模型 API 配置须在服务端设置")

    @staticmethod
    def validate_url(value: str, schemes: set):
        parsed = urlsplit(value)
        if parsed.scheme not in schemes or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("连接地址需要正确协议和主机名，不在 URL 中携带凭据、查询参数或 fragment")
        return parsed

    @staticmethod
    def remote_path(value: str):
        # 按目标操作系统解析路径，绝不把远程 /home/... 当成本机 C:\\home。
        if re.match(r"^[A-Za-z]:[\\/]", value) or value.startswith("\\\\"):
            path = PureWindowsPath(ntpath.normpath(value))
        else:
            path = PurePosixPath(posixpath.normpath(value))
        if not path.is_absolute():
            raise ValueError("目标机器目录必须是绝对路径")
        return path

    def check_root(self, directory: str, instance_id: str = "local"):
        item = self.get(instance_id)
        if item.transport == "stdio":
            target = Path(directory).resolve()
            roots = [Path(root).resolve() for root in [item.workspace, *item.allowed_roots] if root]
        else:
            target = self.remote_path(directory)
            roots = [self.remote_path(root) for root in [item.workspace, *item.allowed_roots] if root]
        if not any(target == root or target.is_relative_to(root) for root in roots):
            raise ValueError(f"该 Codex 聊天的工作目录不在实例 {instance_id} 的 allowed_roots 中")
        return target

    @staticmethod
    def secret(name: str) -> str:
        value = os.environ.get(name, "")
        if not value:
            raise ValueError(f"麦麦进程缺少环境变量 {name}；设置后需重启麦麦")
        return value

    def scrub(self, text: str) -> str:
        from .service import redact
        text = redact(text)
        for item in self.service.config.instances.configured:
            names = [item.token_env, *(ref.source_env for ref in item.environment)]
            if item.transport == "stdio":
                names.append(item.api_key_env)
            for name in names:
                value = os.environ.get(name, "") if name else ""
                if value:
                    text = text.replace(value, "[CREDENTIAL]")
        return text

    def connect(self, instance_id: str = "local", factory=AppServer) -> AppServer:
        item = self.get(instance_id)
        if item.transport == "websocket":
            return factory(endpoint=item.endpoint, token=self.secret(item.token_env) if item.token_env else "")
        args = ["app-server", "--listen", "stdio://"]
        if item.api_base_url:
            # -c 的值是 TOML 字符串；JSON 字符串转义兼容这类 TOML 基本字符串。
            overrides = {"model_provider": "maimai_bridge", "model_providers.maimai_bridge.name": "MaiMai instance",
                         "model_providers.maimai_bridge.base_url": item.api_base_url,
                         "model_providers.maimai_bridge.env_key": item.api_key_env,
                         "model_providers.maimai_bridge.wire_api": "responses"}
            for key, value in overrides.items():
                args += ["-c", f"{key}={json.dumps(value)}"]
            args += ["-c", "model_providers.maimai_bridge.requires_openai_auth=false"]
        elif item.api_key_env:
            raise ValueError("api_key_env 需配合 api_base_url；使用现有 provider 可通过 environment 映射其密钥变量")
        if item.transport == "ssh":
            ssh = shutil.which("ssh")
            if not ssh:
                raise FileNotFoundError("未安装 SSH 客户端")
            remote = [item.executable or "codex", *args]
            if item.conda_prefix:
                remote = [item.conda_executable or "conda", "run", "--no-capture-output", "-p", item.conda_prefix, *remote]
            if item.codex_home:
                remote = ["env", f"CODEX_HOME={item.codex_home}", *remote]
            command = [ssh, "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "StrictHostKeyChecking=yes",
                       "-p", str(item.ssh_port)]
            if item.ssh_identity_file:
                command += ["-i", str(Path(item.ssh_identity_file).expanduser().resolve())]
            # 仅拼接管理员的固定配置；聊天内容始终走 JSONL stdin，不进入 SSH 命令。
            command += [item.ssh_target, shlex.join(remote)]
            return factory(command=command)
        executable = find_codex(item.executable)
        if instance_id == "local":
            return factory(executable)  # 保留原单实例接口。
        environment = dict(os.environ)
        if item.codex_home:
            environment["CODEX_HOME"] = str(Path(item.codex_home).expanduser().resolve())
        if item.api_key_env:
            environment[item.api_key_env] = self.secret(item.api_key_env)
        for ref in item.environment:
            environment[ref.name] = self.secret(ref.source_env)
        command = [executable, *args]
        if item.conda_prefix:
            prefix = Path(item.conda_prefix).expanduser().resolve()
            if not prefix.is_dir():
                raise ValueError("Conda 环境目录不存在")
            if item.conda_executable:
                conda = Path(item.conda_executable).expanduser().resolve()
                if not conda.is_file() or os.name == "nt" and conda.suffix.lower() != ".exe":
                    raise ValueError("conda_executable 需要可执行文件绝对路径，Windows 使用 conda.exe")
                command = [str(conda), "run", "--no-capture-output", "-p", str(prefix), *command]
            else:
                directories = [prefix, prefix / "Scripts", prefix / "Library/bin"] if os.name == "nt" else [prefix / "bin"]
                environment["PATH"] = os.pathsep.join(map(str, directories)) + os.pathsep + environment.get("PATH", "")
                environment["CONDA_PREFIX"] = str(prefix)
        return factory(command=command, environment=environment)

    async def scan(self) -> str:
        rows = await asyncio.to_thread(discover, self.service.config.instances.scan_roots)
        self.discovered = {row["id"]: CodexInstance(id=row["id"], executable=row["executable"], conda_prefix=row["conda_prefix"])
                           for row in rows}
        await self.write_json("discovery.json", {"scanned_at": utc_now(), "installations": rows})
        return "本机扫描完成（未执行发现的程序）\n" + ("\n\n".join(
            f"{row['id']} · {row['source']}\n{row['executable']}" + (f"\nConda: {row['conda_prefix']}" if row['conda_prefix'] else "")
            for row in rows) or "常见目录未找到其他 Codex，可在 scan_roots 增加安装目录") + "\n\n切换：/agent instance 实例ID"

    async def load(self) -> None:
        self.validate()
        await self.scan()

    async def select(self, stream_id: str, user_id: str, instance_id: str) -> str:
        self.get(instance_id)
        self.selections[self.service.binding_key(stream_id, user_id, "instance")] = instance_id
        await self.service.save()
        return f"已选择 Codex 实例 {instance_id}。此 QQ 来源的 session 和模型设置按实例分别保存。\n/agent recent 查看该实例；/agent run 任务内容 发起任务。"

    def inventory(self, stream_id: str, user_id: str) -> str:
        current = self.selections.get(self.service.binding_key(stream_id, user_id, "instance"), "local")
        ids = ["local", *(item.id for item in self.service.config.instances.configured), *self.discovered]
        lines = [f"当前实例: {current}"]
        for instance_id in ids:
            item = self.get(instance_id)
            location = item.endpoint if item.transport == "websocket" else item.ssh_target if item.transport == "ssh" else item.executable or "PATH 自动查找"
            lines.append(f"{instance_id} · {item.transport}\n{location}")
        lines.append("/agent scan 重新扫描；/agent instance 实例ID 切换；/agent probe 检查所选实例连接")
        return "\n\n".join(lines)

    async def write_json(self, filename: str, payload: Dict[str, Any]) -> None:
        content = json.dumps(payload, ensure_ascii=False, indent=2)
        def write():
            self.buffer_dir.mkdir(parents=True, exist_ok=True)
            path = self.buffer_dir / filename
            temporary = self.buffer_dir / (filename + "." + uuid.uuid4().hex + ".tmp")
            temporary.write_text(content, encoding="utf-8")
            temporary.replace(path)
        writing = asyncio.create_task(asyncio.to_thread(write))
        try:
            await asyncio.shield(writing)
        except asyncio.CancelledError:
            await writing
            raise

    async def read_index(self, instance_id: str) -> Dict[str, Any]:
        identity = self.identity(instance_id)
        def read():
            path = self.buffer_dir / f"sessions-{instance_id}.json"
            empty = {"instance_id": instance_id, "identity": identity, "partitions": {}}
            if not path.exists():
                return empty
            index = json.loads(path.read_text(encoding="utf-8"))
            return index if index.get("identity") == identity else empty
        return await asyncio.to_thread(read)

    def identity(self, instance_id: str) -> str:
        item = self.get(instance_id)
        fields = (item.transport, item.executable, item.codex_home, item.endpoint,
                  item.ssh_target, item.ssh_port, item.ssh_identity_file, item.conda_prefix, item.api_base_url)
        return hashlib.sha256(json.dumps(fields).encode()).hexdigest()

    async def cache(self, instance_id: str, threads: List[Dict[str, Any]], archived: bool = False,
                    replace: bool = False, complete: bool = False) -> None:
        rows = []
        for thread in threads:
            self.check_root(thread["cwd"], instance_id)
            # 只缓存索引字段，不落盘 preview、turns、请求头、模型配置或凭据。
            row = {key: thread.get(key) for key in ("id", "cwd", "name", "model", "reasoningEffort", "modelProvider", "updatedAt", "path")}
            for key, value in row.items():
                if isinstance(value, str):
                    row[key] = self.scrub(value)
            rows.append(row)
        async with self.index_lock:
            index = await self.read_index(instance_id)
            partition = "archived" if archived else "active"
            old = index["partitions"].get(partition, {})
            merged = {} if replace else {row["id"]: row for row in old.get("sessions", [])}
            merged.update({row["id"]: row for row in rows})
            values = sorted(merged.values(), key=lambda row: row.get("updatedAt") or 0, reverse=True)
            limit = self.service.config.instances.max_index_entries
            index["partitions"][partition] = {"checked_at": utc_now(), "complete": complete and len(values) <= limit,
                                               "sessions": values[:limit]}
            await self.write_json(f"sessions-{instance_id}.json", index)

    async def refresh(self, instance_id: str) -> str:
        if not self.get(instance_id).allow_saved_threads:
            raise ValueError("该实例的已有 session 浏览尚未启用")
        counts = []
        async with self.service.codex_connection(instance_id) as client:
            for archived in (False, True):
                rows, cursor = [], None
                for _ in range(100):
                    params = {"limit": 50, "sortKey": "updated_at", "sortDirection": "desc", "archived": archived}
                    if cursor:
                        params["cursor"] = cursor
                    response = await client.request("thread/list", params)
                    for thread in response.get("data", []):
                        if not thread.get("cwd"):
                            continue
                        try:
                            self.check_root(thread["cwd"], instance_id)
                        except ValueError:
                            continue
                        rows.append(thread)
                    cursor = response.get("nextCursor")
                    if not cursor or len(rows) >= self.service.config.instances.max_index_entries:
                        break
                await self.cache(instance_id, rows, archived, replace=True, complete=not cursor)
                counts.append(len(rows[:self.service.config.instances.max_index_entries]))
        return f"实例 {instance_id} 索引已刷新：未归档 {counts[0]}，已归档 {counts[1]}。\n/agent index 查看缓存。"

    async def cached(self, instance_id: str, page: int = 1) -> str:
        from .sessions import PAGE_SIZE, Sessions, timestamp
        Sessions.page_number(page)
        if not self.get(instance_id).allow_saved_threads:
            raise ValueError("该实例的已有 session 浏览尚未启用")
        index = await self.read_index(instance_id)
        rows, checked = [], []
        for name, partition in index["partitions"].items():
            checked.append(f"{name}: {partition['checked_at']} · {'完整' if partition.get('complete') else '部分'}")
            for row in partition["sessions"]:
                try:
                    self.check_root(row["cwd"], instance_id)
                except ValueError:
                    continue
                rows.append({**row, "archived": name == "archived"})
        rows.sort(key=lambda row: row.get("updatedAt") or 0, reverse=True)
        lines = [f"实例 {instance_id} 的缓存索引 · 第 {page} 页（非实时连接状态）", *checked]
        for row in rows[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]:
            lines.append(f"{row['id']} · {'已归档' if row['archived'] else '未归档'}\n{row.get('name') or '未命名'}\n"
                         f"{row['cwd']}\n{row.get('model') or '未记录'} / {row.get('reasoningEffort') or '未记录'} · {timestamp(row.get('updatedAt'))}")
        if not rows:
            lines.append("暂无索引，使用 /agent index refresh 从该实例刷新")
        if len(rows) > page * PAGE_SIZE:
            lines.append(f"下一页：/agent index {page + 1}")
        return "\n\n".join(lines)

    async def probe(self, instance_id: str) -> str:
        async with self.service.codex_connection(instance_id) as client:
            result = await client.request("config/read", {"includeLayers": False})
            config = result["config"]
        return f"实例 {instance_id} 连接正常\n模型: {config.get('model') or '自动'}\neffort: {config.get('model_reasoning_effort') or '自动'}\n此检查未发起模型推理。"
