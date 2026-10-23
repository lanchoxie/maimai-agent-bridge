# GitHub 与插件中心发布

仓库：https://github.com/lanchoxie/maimai-agent-bridge

插件 ID 沿用 `local.maimai-agent-bridge`，保持已部署实例的数据目录、绑定和任务状态兼容；公开发布后保持该 ID 稳定。

## 验证与打包

在仓库根目录运行：

```text
uv sync
uv run python scripts/check_release.py
uv run ruff check .
uv run python scripts/run_tests.py
```

测试只使用本地临时数据、HTTP mock 和回环 WebSocket，不读取真实 Codex 登录，不向 QQ 发消息，也不消耗模型额度。`tests/smoke_*.py` 是单独手动运行的真实 Codex 联调，不由上述测试自动执行。

确认 `_manifest.json`、`pyproject.toml` 和 `config.example.toml` 的版本一致，更新 `CHANGELOG.md` 后提交。先创建 `dist/` 目录，再从 Git 提交生成源码包，不直接压缩正在使用的插件目录：

```text
git archive --format=zip --output=dist/maimai-agent-bridge-v0.5.0.zip HEAD
```

本机 `config.toml`、`config_back/`、`.runtime/`、导出文件、环境变量文件、聊天记录和 CLI 登录信息均不应进入提交。

## GitHub Release

为通过验证的提交创建同版本 Tag，例如 `v0.5.0`，再发布 GitHub Release。首个版本建议作为预发布版，Release 说明应明确群聊自然语言对宿主身份上下文的依赖及远端文件限制。GitHub 自动生成的源码包即可安装；草稿或单独 Tag 不满足插件中心的发布要求。已收录的 Tag 与提交不可改写，修复发布新的版本号。

## 社区登记

按[插件中心贡献指南](https://github.com/Mai-with-u/plugin-repo/blob/main/CONTRIBUTING.md)，在该仓库使用“Add Plugin / 添加插件” Issue 模板，填写：

- 插件 ID：`local.maimai-agent-bridge`
- 公开仓库：`https://github.com/lanchoxie/maimai-agent-bridge`

发布 GitHub 仓库和 Release 不等于已进入社区插件中心，仍须提交登记并等待验证、维护者审核。登记 Issue 由维护者按准备情况提交，不由本插件自动创建。
