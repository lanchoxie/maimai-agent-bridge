"""Agent 桥接插件配置。"""

from typing import List, Literal

from maibot_sdk import Field, PluginConfigBase


class PluginSettings(PluginConfigBase):
    __ui_label__ = "插件"
    enabled: bool = Field(default=True, description="启用插件")
    config_version: str = Field(default="0.5.0", description="配置版本")


class AccessSettings(PluginConfigBase):
    __ui_label__ = "调用权限"
    allowed_users: List[str] = Field(default_factory=list, description="允许派任务的 QQ 号，留空禁止所有调用")
    allowed_groups: List[str] = Field(default_factory=list, description="允许的 QQ 群，留空表示允许上述用户所在的群和私聊")


class CodexSettings(PluginConfigBase):
    __ui_label__ = "Codex"
    executable: str = Field(default="", description="codex.exe 绝对路径；留空自动查找")
    workspace: str = Field(default="", description="独立任务工作目录；留空使用插件的数据目录")
    model: str = Field(default="", description="留空使用 Codex 本人的模型配置，不使用麦麦聊天模型的 API")
    reasoning_effort: str = Field(default="", description="留空继承 session 或 CLI 的 effort；也可在 QQ 中按 session 设置")
    sandbox: Literal["read-only", "workspace-write"] = Field(default="read-only", description="Codex 文件访问模式")
    allowed_roots: List[str] = Field(default_factory=list, description="已有聊天的工作目录必须在这些目录内；留空只允许独立任务目录")
    allow_saved_threads: bool = Field(default=False, description="允许继续已保存的桌面聊天。仅在桌面聊天空闲时使用；不代表连接到桌面正在运行的进程")
    task_timeout_seconds: int = Field(default=1800, ge=30, le=14400, description="单任务最长执行时间，秒")
    max_concurrent: int = Field(default=3, ge=1, le=4, description="最多同时执行的任务数")
    max_active_sessions: int = Field(default=3, ge=1, le=10, description="每个 QQ 来源最多保留的活动 session 数；超出时需先关闭旧任务")
    max_queued: int = Field(default=10, ge=1, le=50, description="最多排队任务数")


class RemoteAgent(PluginConfigBase):
    name: str = Field(default="", description="Agent 名称，使用英文、数字、短横线或下划线")
    endpoint: str = Field(default="", description="A2A 服务基地址，固定由配置提供")
    protocol: Literal["1.0-rest", "0.3-jsonrpc"] = Field(default="1.0-rest", description="远端协议版本及传输")
    token_env: str = Field(default="", description="鉴权令牌所在环境变量名；不把密钥写到聊天中")


class RemoteSettings(PluginConfigBase):
    __ui_label__ = "远程 A2A"
    agents: List[RemoteAgent] = Field(default_factory=list, description="允许连接的远程 Agent")


class FileSettings(PluginConfigBase):
    __ui_label__ = "任务文件回传"
    enabled: bool = Field(default=True, description="允许将本机 Codex 任务目录中的产物发回原 QQ 会话")
    auto_send: bool = Field(default=True, description="任务完成后发送 Codex 明确标记的交付文件；普通文字路径不会上传")
    max_file_mb: int = Field(default=20, ge=1, le=100, description="单个文件最大大小，MB")
    max_files: int = Field(default=5, ge=1, le=10, description="每个任务最多回传的文件数")


class EnvironmentRef(PluginConfigBase):
    name: str = Field(default="", description="传给本地 Codex 的环境变量名")
    source_env: str = Field(default="", description="从麦麦进程的哪个环境变量读取值；不填写密钥本身")


class CodexInstance(PluginConfigBase):
    id: str = Field(default="", description="实例 ID，英文、数字、短横线或下划线；local 保留给原来的本机配置")
    transport: Literal["stdio", "ssh", "websocket"] = Field(default="stdio", description="本地进程、SSH 远程进程或 WebSocket 服务")
    executable: str = Field(default="", description="本地 codex.exe 或远端 Codex 可执行文件路径")
    codex_home: str = Field(default="", description="该实例的 Codex 配置和 session 目录；留空继承目标机器默认值")
    workspace: str = Field(default="", description="任务工作目录；远程实例必须填写目标机器上的绝对路径")
    allowed_roots: List[str] = Field(default_factory=list, description="允许浏览的 session 工作目录，使用目标机器的路径")
    allow_saved_threads: bool = Field(default=False, description="允许浏览和续聊该实例的已有 session")
    model: str = Field(default="", description="留空继承此实例自己的模型")
    reasoning_effort: str = Field(default="", description="留空继承此实例自己的 effort")
    endpoint: str = Field(default="", description="WebSocket 的 ws://localhost 或 wss://服务器 地址；不是模型 API 地址")
    token_env: str = Field(default="", description="WebSocket Bearer 令牌所在的本机环境变量名")
    ssh_target: str = Field(default="", description="SSH 的 user@host 或已配置的 SSH Host 别名，使用密钥登录")
    ssh_port: int = Field(default=22, ge=1, le=65535, description="SSH 端口")
    ssh_identity_file: str = Field(default="", description="本机 SSH 私钥路径；留空使用 SSH 默认配置")
    conda_executable: str = Field(default="", description="可选 conda 可执行文件；配合 conda_prefix 使用 conda run")
    conda_prefix: str = Field(default="", description="Conda 环境目录；本地不填 conda_executable 时为原生 Codex 设置环境 PATH")
    api_base_url: str = Field(default="", description="可选 Responses 兼容模型 API 基地址，stdio/ssh 可用；WebSocket 在服务端配置")
    api_key_env: str = Field(default="", description="模型 API Key 的环境变量名。stdio 从本机取值；ssh 使用远端已有环境变量")
    environment: List[EnvironmentRef] = Field(default_factory=list, description="额外本地环境变量引用；远程凭据在远端配置")


class InstancesSettings(PluginConfigBase):
    __ui_label__ = "Codex 实例与索引"
    configured: List[CodexInstance] = Field(default_factory=list, description="手动配置的本地、Conda、SSH 或 WebSocket Codex 实例")
    buffer_dir: str = Field(default="", description="索引缓存目录；留空使用插件数据目录下的 buffer，不保存密钥和聊天正文")
    scan_roots: List[str] = Field(default_factory=list, description="额外本机安装目录或 Conda 环境目录；只扫描有限层数")
    max_index_entries: int = Field(default=1000, ge=10, le=5000, description="每实例最多缓存的 session 索引条数")


class BridgeConfig(PluginConfigBase):
    plugin: PluginSettings = Field(default_factory=PluginSettings)
    access: AccessSettings = Field(default_factory=AccessSettings)
    codex: CodexSettings = Field(default_factory=CodexSettings)
    instances: InstancesSettings = Field(default_factory=InstancesSettings)
    files: FileSettings = Field(default_factory=FileSettings)
    remote: RemoteSettings = Field(default_factory=RemoteSettings)
