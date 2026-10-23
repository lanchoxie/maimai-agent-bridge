# 麦麦 Agent 桥接

麦麦通过工具或 `/agent` 命令提交任务，立即返回任务编号；任务在后台完成或失败后自动回传原 QQ 会话。插件包含 Codex App Server 适配器和远程 A2A 客户端，支持 session 浏览、模型/effort 切换、多实例和本机任务文件回传。

**0.5.0 为公开测试版本。** 已在 Python 3.12、MaiBot 1.3.9、SDK 2.12.0 上联调。自然语言工具需要宿主传入真实消息发送者的身份；部分版本需参考[宿主上下文说明](docs/host-context.md)修复，`/agent` 命令可独立使用。

仓库：[lanchoxie/maimai-agent-bridge](https://github.com/lanchoxie/maimai-agent-bridge) · [版本记录](CHANGELOG.md) · [发布说明](docs/releasing.md)

## 启用

在 MaiBot 的 `plugins/` 目录安装本仓库，目录可命名为 `maimai_agent_bridge`。通过 Git 安装时，在 MaiBot 根目录执行：

```text
git clone https://github.com/lanchoxie/maimai-agent-bridge.git plugins/maimai_agent_bridge
uv pip install -r plugins/maimai_agent_bridge/requirements.txt
```

也可下载 GitHub Release 的源码包，将解压后包含 `_manifest.json` 和 `plugin.py` 的目录放入 `plugins/`。插件自身依赖 `httpx`、`websockets`；额外需要本机或远端已安装并登录的 Codex CLI，QQ 侧需已连接可用的适配器。

首次加载由 Runner 生成 `config.toml`，也可事先复制 `config.example.toml`。把 `access.allowed_users` 填为实际操作人的 QQ 号。空列表禁止所有调用；不按昵称鉴权。必要时配置 `codex.executable` 为 Codex 原生程序的绝对路径，Windows 使用 `codex.exe`。重启或在插件页面重新加载。

Codex 默认继承本机 CLI 的登录和模型配置，独立于麦麦主聊天模型。默认只读文件。需要写代码时在本地把 `sandbox` 改成 `workspace-write`；写权限限于工作目录。任务工作目录默认是插件持久化数据目录下的 `workspace`。

请只授权可信操作者：这些用户可委托已登录的 Codex 执行任务，使用其模型额度，并在配置的目录权限内读写文件。模型调用是否可用取决于该 Codex 实例自己的登录和服务商，不由麦麦聊天模型配置决定。

## 使用

```text
/agent run 请列出这个工作目录的文件并简要解释
/agent session 我们开始讨论一个待办事项项目，先记住名字是小水獭
/agent session 刚才项目叫什么？
/agent status
/agent status 任务ID
/agent continue 任务ID 请展开第二点
/agent cancel 任务ID
```

也可以明确对麦麦说“交给 Codex 帮我……”，由模型调用 `agent_submit`。模型不知道真实进度时用 `agent_status`，不要重复提交任务。

## 已保存的桌面聊天

当前桌面 App 的内部 stdio 不是外部 API。这里的 `desktop` 是通过官方 `thread/read` / `thread/resume` **续聊同一个已保存聊天 ID**，不是向正在运行的桌面进程注入按键，也不保证桌面界面立即刷新。

在本地配置 `codex.allow_saved_threads = true`，并把需要的项目目录加入 `codex.allowed_roots`。先用 `chats` 查看允许目录中最近的聊天，再复制 ID 绑定：

```text
/agent chats
/agent bind 目标聊天ID
/agent desktop 帮我接着刚才的分析，补充验证步骤
/agent unbind
```

已登录的噜噜也可以按自然语言完成同样的流程：先用 `agent_sessions` 搜索并核对目标 session，再在用户明确要求后用 `agent_bind` 绑定，最后以 `agent_submit` 的 `mode="desktop"` 继续。绑定仍会检查 session 所在目录是否获准，以及该 session 是否空闲。

同一聊天必须保持空闲，避免桌面和 QQ 两端同时写历史。独立 App Server 无法可靠读取另一个桌面进程的实时忙碌状态；插件只能检查本连接已知状态、串行执行自己管理的同一聊天任务。需要实时共享正在运行的聊天，应再接入桌面公开的共享服务端点。插件默认不擅自打开或暴露桌面控制端口。

已有聊天绑定按“真实 QQ 会话 + 用户”保存，任务结果也仅向该来源回传。取消、查进度和续聊都检查相同归属。不能从群 A 查询群 B 的任务。

同一用户在不同群默认也不会自动共用 session。在其他已授权群明确绑定同一个 session ID 后，可以使用 `/agent desktop` 继续；两个群应选择相同的 Codex 实例。同一个 session 的插件任务串行执行，结果回到本轮发起任务的群。`allowed_groups` 非空时，只有列出的群可调用；留空允许已授权用户所在的群及私聊。

## 浏览 session 和切换模型（0.2.0）

| QQ 命令 | 作用 |
| --- | --- |
| `/agent sessions` | 最近 session，按更新时间排序，显示 ID、标题、最后使用的模型和 effort |
| `/agent sessions 2` | 第二页；`recent`、`chats` 是相同命令的别名 |
| `/agent sessions 1 关键词` | 搜索历史 session |
| `/agent sessions archived 1` | 已归档的 session |
| `/agent current` | 当前 QQ 来源选中的 session、实际模型及下次调用配置 |
| `/agent info sessionID` | 指定 session 的信息 |
| `/agent history` | 当前 session 最近四轮聊天 |
| `/agent history sessionID 2` | 指定 session 更早的聊天，每页四轮 |
| `/agent models` | 本机 Codex 模型目录及每个模型支持的 effort，支持页码 |
| `/agent model 模型名 high` | 当前 session 下次插件调用使用该模型和 high effort |
| `/agent effort medium` | 只改当前 session 下次插件调用的 effort |
| `/agent model 模型名 high default` | 本 QQ 来源中新任务的默认配置 |
| `/agent effort inherit` | 清除当前 session 的 effort 覆盖 |

“当前”指本 QQ 会话中本用户最近绑定或由插件开始执行的 Codex session；它不跟随桌面当前选中的聊天标签。绑定会立即切换当前 session；独立任务开始后会成为当前 session。`desktop` 指已绑定的聊天，`session` 指 `/agent session` 创建的长期会话，`last` 指此 QQ 来源最近的插件任务。`info`、`history`、模型/effort 设置的目标支持这些名称或具体 session ID；配置目标 `default` 仅影响本 QQ 来源的新任务，不修改全局 Codex 配置。

模型/effort 的优先级为：此 session 的覆盖 → 此 QQ 来源的新任务默认值 → 插件本地配置 → session 历史配置 → CLI 配置。设置保存在插件数据目录，已提交任务保留提交时的插件设置。可以在模型命令中同时给出 effort，避免新模型不支持旧 effort；目录中的模型会校验其支持的等级，目录外模型允许明确指定并提示以服务端调用结果为准。模型目录不是账号调用权限的保证。

历史信息区分“历史最新实际使用值”和“下次插件调用值”。正文分页使用 `thread/turns/list`，每轮的历史模型/effort 从本机 Codex 的对应 rollout `turn_context` 元数据读取；缺失时明确显示“未记录”，不会用当前默认模型猜测。只输出用户和 assistant 可见消息，不输出 reasoning、系统提示或工具执行结果；长消息会标注截取。模型选择从下次插件调用生效，不同步修改桌面进程的选择器。

浏览仍只限 `allowed_roots` 内的工作目录和授权 QQ 用户。历史浏览允许该用户主动选择这些目录中的 session，并把所选内容回传发起命令的 QQ 会话。自然语言对应 `agent_sessions`（浏览）、`agent_bind`（按明确要求绑定或解除绑定）和 `agent_configure`（按用户明确要求设置）工具；绑定后使用 `agent_submit` 的 `mode="desktop"` 继续对话。

## 本地发现、多实例和 buffer（0.3.0）

实例发现、连接和索引功能在本插件中实现。原 `[codex]` 配置保留为 `local` 实例，旧任务和绑定继续使用它。插件加载时扫描一次，之后可主动重新扫描：

```text
/agent scan
/agent instances
/agent instance scan-扫描结果里的ID
/agent probe
/agent recent
/agent index refresh
/agent index
/agent instance local
```

扫描包括 PATH、npm、桌面应用附带 CLI、常见 Conda 目录和 `~/.conda/environments.txt` 登记环境；不执行发现的程序，不遍历整块硬盘。其他安装目录或环境前缀填到 `instances.scan_roots`。扫描结果的 `scan-...` ID 根据程序路径稳定生成，可以直接选择；其允许目录继承 `[codex]`。需要独立的认证、Codex home 或工作目录时，添加手动实例。

每个 QQ 来源和用户单独选择实例；session 绑定、当前 session、模型和 effort 按实例隔离。已提交任务保留实例归属，`continue` 总是回到原实例。切换服务器后再用 `recent`、`history`、`model`、`effort`，它们作用于新选中的实例。删除所选实例后明确报错，可以 `/agent instance local` 恢复选择。

`agent_instances` 工具提供 `list/scan/select/probe/index/refresh`，沿用 QQ 白名单和真实调用上下文校验。连接地址、可执行文件和环境变量来源只能在管理员配置中修改，不接受聊天提供的任意地址或命令。

### 本地和 Conda

完整字段见 `config.example.toml`。添加配置块时，删除模板的 `configured = []`：

```toml
[instances]
buffer_dir = "" # 默认在插件数据目录的 buffer 内
scan_roots = ["D:/miniconda3", "D:/tools"]
max_index_entries = 1000

[[instances.configured]]
id = "work"
transport = "stdio"
executable = "D:/tools/codex.exe"
conda_prefix = "D:/miniconda3/envs/work"
workspace = "D:/projects/work"
allowed_roots = ["D:/projects"]
allow_saved_threads = true
codex_home = "C:/Users/me/.codex-work"
```

`conda_prefix` 可选，留空就是普通本地实例。填写 `conda_executable` 时使用 `conda run --no-capture-output -p ...`；不填写则把环境目录加入子进程 PATH，适合原生 Codex CLI。需要环境激活脚本中的变量时用 `conda run`。Windows 的 Codex/Conda 可执行文件应使用原生 `.exe`，扫描会查找 npm 包里的原生 Codex；不直接执行 `.cmd/.ps1` 包装脚本。

`codex_home` 仅传给此子进程，区分各实例的登录、配置和 session 存储。空值继承当前进程环境和 Codex 默认目录。不同工作目录、Conda 环境本身不等于不同 Codex home；如果都继承同一个 home，它们能看到同一组已保存 session。当前安装版本的 App Server 不支持 CLI 的 `--profile`，需要不同配置时使用各自的 `codex_home/config.toml`。

### API Key 和远程连接

**模型 API 地址**和**Codex 服务器地址**是两个不同设置。`stdio/ssh` 实例可以继承目标 Codex 的已有登录和 provider，或者配置 Responses 兼容的模型服务：

```toml
# 写在相应 [[instances.configured]] 块中
api_base_url = "https://provider.example/v1"
api_key_env = "WORK_CODEX_API_KEY"
model = "服务支持的模型名"
```

API 必须兼容 Codex 使用的 Responses 协议，仅提供 Chat Completions 接口不够。插件用进程参数选择 provider，密钥放环境变量，不改全局 Codex 配置。`stdio` 的 `api_key_env` 来自麦麦进程；更改系统环境变量后需要重启麦麦。已有 provider 使用其他变量名时，可配置 `environment = [{name = "PROVIDER_API_KEY", source_env = "WORK_CODEX_API_KEY"}]` 映射给子进程，不必覆盖 provider。

SSH 适用于 POSIX 服务器，Windows 服务器可用 WebSocket。先在本机配置正常的 SSH 密钥登录并确认主机指纹，再添加：

```toml
[[instances.configured]]
id = "server"
transport = "ssh"
ssh_target = "me@server.example"
ssh_port = 22
executable = "/home/me/.local/bin/codex"
workspace = "/home/me/projects"
allowed_roots = ["/home/me/projects"]
allow_saved_threads = true
# 可选：ssh_identity_file、codex_home、conda_executable、conda_prefix
```

插件通过 SSH 启动远端 stdio App Server；聊天内容只经 stdin 的 JSON 消息传送。SSH 启用 BatchMode 和已有主机指纹校验，不会卡在密码提示上。远端需要绝对工作目录；模型登录/API Key 在远端配置，SSH 的 `api_key_env` 指远端进程已能读取的变量，不把本机 API Key 拼到 SSH 命令中。非交互 SSH 不保证加载交互 shell 的配置，Conda 建议填完整程序路径。

已有 Codex WebSocket App Server 则配置：

```toml
[[instances.configured]]
id = "ws-server"
transport = "websocket"
endpoint = "wss://codex.example.com"
token_env = "REMOTE_CODEX_TOKEN"
workspace = "/home/me/projects"
allowed_roots = ["/home/me/projects"]
allow_saved_threads = true
```

WebSocket 连接在握手时发送 Bearer token。登录、模型服务、Codex home 和 Conda 环境在服务端设置，客户端不要同时填写这些启动字段。服务端官方启动入口是 `codex app-server --listen ws://127.0.0.1:端口`，支持 `--ws-auth capability-token --ws-token-file /绝对路径`；可通过 SSH 端口转发使用本机 `ws://127.0.0.1:端口`，远程直连使用配置了 TLS 的 `wss://`。此传输在官方文档中标为实验功能。`/agent probe` 仅验证 RPC 连接和读取模型配置，不保证模型推理额度或远端命令权限。

### 索引目录

`buffer/discovery.json` 保存本机安装索引；`buffer/sessions-实例ID.json` 保存 session ID、名称、目录、记录文件路径、模型、effort、更新时间和刷新时间。每个实例单独缓存，连接配置身份变化时旧缓存失效。缓存不保存认证信息、聊天正文或完整模型配置；任务及实际请求结果仍在插件原有 `state.json` 中按生命周期保存。

浏览在线 session 会增量记录索引；`index refresh` 分别刷新未归档与已归档 session，有数量上限并标注是否完整。刷新需要实例在线，失败直接显示错误；`index` 明确展示缓存和时间，不代表当前在线状态。读取缓存仍会检查当前允许目录和浏览开关。缓存不用于重放任务。

远端聊天正文从远端 RPC 获取，不会把远端返回的文件路径当成本机文件打开。每轮历史模型字段如果远端没有提供，会显示“未记录”；session 的最新模型/effort 仍可正常查看。

接口依据：[官方 OpenAI App Server 文档](https://learn.chatgpt.com/docs/app-server)、[官方高级配置](https://learn.chatgpt.com/docs/config-file/config-advanced)。

## 任务文件回传

本机 Codex 任务可将交付文件上传回发起任务的 QQ 群或私聊。可自然语言要求麦麦“把任务 X 的 zip 文件发到群里”，工具名为 `agent_files`，也可直接使用：

```text
/agent files 任务ID
/agent file 任务ID dist/game.zip
/agent file 任务ID
```

`files` 查看已登记文件和发送状态；`file` 指定路径上传，留空发送已登记产物。只允许当前用户在当前会话创建的已完成任务，文件必须位于该任务的实际工作目录内，目录需要先打包。旧任务会从真实 Codex session 中确认目录；SSH/WebSocket/A2A 远端文件传输尚未实现。

默认启用 `[files] enabled = true`、`auto_send = true`，最多 5 个文件、每个 20 MB，可在插件配置中调整。插件在本机 Codex 请求末尾附带交付说明：需要给用户文件时，在最终结果中独立输出 `[[QQ_FILE:相对文件路径]]`。只有这些标记会登记为自动交付产物，正文里的普通路径不会上传。关闭 `auto_send` 后仍可手动发送。

文件先验证真实路径、大小和敏感文件名，再在插件 `exports/任务ID/内容哈希/` 下保留上传快照，避免原文件变化。`.env`、常见私钥及 `.git/.codex/.ssh/.venv/node_modules` 内文件不允许回传。发送结果持久化到任务状态；同一任务的已发送产物不重复上传，失败可明确手动重试。上传中重启会标记回执未知，不会自动重发，需要先核对 QQ。上传快照保留在插件数据目录，可在不再需要任务文件后自行清理。

此功能仅为桥接插件增加 `send.custom` 能力，通过 Host 文件组件与 QQ 适配器发送，不修改 MaiBot 主程序。

## 远程 A2A

支持 **A2A 1.0 HTTP+JSON/REST** 与 **A2A 0.3 JSON-RPC** 客户端。必须手动配置服务基地址及版本，不自动发现任意聊天文本中的 URL。1.0 地址不包含 `/message:send`；0.3 地址为 Agent Card 指定的 JSON-RPC 服务 URL。鉴权令牌从配置指定的环境变量读取。

```toml
[[remote.agents]]
name = "research"
endpoint = "http://127.0.0.1:9100"
protocol = "1.0-rest"
token_env = "RESEARCH_AGENT_TOKEN"
```

```text
/agent send research 调查本周项目进展
```

支持即时文本回复和异步任务轮询；需要补充信息时回传 QQ，使用 `/agent reply 任务ID 回答`。请求取消会发送远端取消指令；未得到确认会明确提示。文件产物传输、Agent Card 自动发现和 Webhook 尚未实现。

## 状态及错误

任务最长 30 分钟，可在配置中调整。这个期限只约束后台 Agent，不占用麦麦单轮工具等待。任务状态、最终结果、聊天绑定保存在 `data/plugins/local.maimai-agent-bridge/state.json`，其中包含任务对应的 Codex `thread_id` 和最近检查时间。默认每个“QQ 会话 + 用户”最多保留 3 个活动 session；提交、status、current、history 和 session 列表都会先刷新本地监视状态。达到上限时会列出任务编号，并提示使用 `/agent cancel 任务ID` 或等待完成。进度输出只保存摘要，结果回传失败仍可通过 status 取回。插件停止会请求中断；重启后将原活动任务标记为 interrupted，不自动重新执行。任务可能已经产生文件或远端副作用，中断后请先检查再续聊。

Codex 请求补充时使用 `/agent reply`；如出现审批，只有本人 QQ 命令 `/agent approve 任务ID approve|deny` 可以回答，模型不能替人审批。默认 Codex approval policy 为 never，因此需要额外授权的动作通常直接拒绝。多个补充问题的回答可以填写 JSON 对象，以问题 ID 为键。

## 开发验证

插件独立分发，代码不导入 MaiBot 的 `src.*`，所有宿主能力经 SDK 调用。QQ 授权、机器路径、配置备份和运行数据均由 Git 忽略。工具采用 SDK 的 Action 兼容入口，以校验原始参数并拒绝覆盖真实用户/群/会话上下文；插件不会自动修改宿主文件。

独立克隆后，在仓库根目录运行：

```text
uv sync
uv run python scripts/check_release.py
uv run ruff check .
uv run python scripts/run_tests.py
```

测试不需要 MaiBot 仓库、真实 Codex 登录或 QQ 账号；GitHub Actions 在 Windows 和 Linux 上执行相同检查。

真实 Codex 联调（会发起模型请求，仅输出到终端）：

```text
python -m plugins.maimai_agent_bridge.tests.smoke_codex
```

协议依据：[Codex App Server](https://developers.openai.com/codex/app-server)、[A2A 规范](https://a2a-protocol.org/v1.0.1/specification/)。
