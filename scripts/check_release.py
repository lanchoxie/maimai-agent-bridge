"""检查公开源码清单、版本及误纳入的本机数据；不输出敏感匹配内容。"""

from pathlib import Path
from urllib.parse import urlsplit

import json
import re
import subprocess
import sys
import tomllib


FORBIDDEN_PARTS = {".runtime", "config_back", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "exports"}
SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"-----BEGIN (?:OPENSSH |RSA |EC )?PRIVATE KEY-----"),
)


def validate(root: Path, files: list[str]) -> list[str]:
    errors: list[str] = []
    required = {"_manifest.json", "plugin.py", "LICENSE", "README.md", "requirements.txt", "config.example.toml"}
    for missing in sorted(required - set(files)):
        errors.append(f"缺少发布文件：{missing}")
    for name in files:
        path = Path(name)
        if (FORBIDDEN_PARTS.intersection(path.parts) or path.name == "config.toml"
                or path.name == ".env" or path.name.startswith(".env.")
                or path.suffix in {".pyc", ".log"}):
            errors.append(f"不应公开的文件：{name}")
            continue
        source = root / path
        if source.is_symlink():
            errors.append(f"发布源码不能包含符号链接：{name}")
            continue
        text = source.read_text(encoding="utf-8")
        if any(pattern.search(text) for pattern in SECRET_PATTERNS):
            errors.append(f"疑似凭据：{name}（内容已隐藏）")
        if re.search(r"(?i)C:[/\\]Users[/\\]xiety\b", text):
            errors.append(f"含本机用户路径：{name}")
    manifest = json.loads((root / "_manifest.json").read_text(encoding="utf-8"))
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    example = tomllib.loads((root / "config.example.toml").read_text(encoding="utf-8"))
    version = manifest["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        errors.append("manifest 版本须使用三段式版本号")
    if project["project"]["version"] != version or example["plugin"]["config_version"] != version:
        errors.append("项目、示例配置与 manifest 版本不一致")
    url = urlsplit(manifest["urls"]["repository"])
    if url.scheme != "https" or url.hostname != "github.com" or len(url.path.strip("/").split("/")) != 2:
        errors.append("仓库地址必须是公开 GitHub HTTPS 仓库地址")
    if manifest["capabilities"] != ["send.text", "send.custom"]:
        errors.append("声明能力与当前实现不一致")
    if example["access"]["allowed_users"] or example["access"]["allowed_groups"]:
        errors.append("示例配置不得预设真实 QQ 用户或群")
    if example["codex"]["sandbox"] != "read-only":
        errors.append("示例配置应默认只读")
    declared = {entry["name"] + entry["version_spec"] for entry in manifest["dependencies"]}
    requirements = {line.strip() for line in (root / "requirements.txt").read_text().splitlines() if line.strip()}
    if declared != requirements:
        errors.append("requirements.txt 与 manifest 依赖不一致")
    return errors


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                            cwd=root, capture_output=True, check=True)
    files = sorted(set(value for value in result.stdout.decode("utf-8").split("\0") if value))
    errors = validate(root, files)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        raise SystemExit(1)
    print(f"发布检查通过：{len(files)} 个源码文件；配置、备份和运行数据未纳入。")


if __name__ == "__main__":
    main()
