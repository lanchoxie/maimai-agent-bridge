"""从独立插件仓库运行离线测试，不需要安装或启动 MaiBot。"""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType

import sys
import unittest


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    # 保留与部署在 MaiBot/plugins 下时相同的导入名；不依赖仓库目录名。
    namespace = ModuleType("plugins")
    namespace.__path__ = [str(root.parent)]
    sys.modules["plugins"] = namespace
    spec = spec_from_file_location("plugins.maimai_agent_bridge", root / "__init__.py",
                                   submodule_search_locations=[str(root)])
    assert spec is not None and spec.loader is not None
    package = module_from_spec(spec)
    sys.modules[spec.name] = package
    spec.loader.exec_module(package)
    suite = unittest.defaultTestLoader.discover(str(root / "tests"), pattern="test_*.py")
    outcome = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if outcome.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
