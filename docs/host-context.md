# 群聊自然语言调用的宿主要求

`/agent` 命令从宿主的命令上下文读取发起人 QQ 号。自然语言工具还依赖 Planner 向插件传入当前触发消息的真实 `user_id`、`group_id`、`platform` 和 `stream_id`。

部分 MaiBot 1.3.9 代码仍从群聊天流的 `user_id` 取值；群聊天流该字段为空，因此插件会返回“未传入发起人的 QQ 号”，或提示未授权。把用户号放进模型参数、放宽白名单或改成按昵称鉴权都不能修复来源身份。

先确认插件配置中的 `allowed_users` 包含实际操作者 QQ 号，再用 `/agent status` 验证。如果命令正常而自然语言报错，检查宿主是否已包含该修复：在本轮 Planner 开始时，从 `trigger_message.message_info.user_info.user_id` 保存触发者；工具暴露与执行上下文都使用这个快照，避免异步等待时被另一位用户新到达的消息覆盖。

仓库附带基于 MaiBot 1.3.9 的最小参考补丁：[maibot-1.3.9-tool-user-context.patch](patches/maibot-1.3.9-tool-user-context.patch)。先在 MaiBot 仓库执行 `git apply --check 补丁路径` 检查，确认版本匹配后再由管理员决定是否应用；若上游已有等效修复，则无需重复应用。插件不会自动修改宿主文件。

此补丁只修正身份传递，不授予任何用户新的调用权限。发起人的 QQ 号仍必须由宿主注入，插件仍校验用户、群及任务归属。宿主不提供真实身份时自然语言工具保持拒绝调用，请使用 `/agent` 命令。
