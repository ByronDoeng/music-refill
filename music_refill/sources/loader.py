"""动态加载 sources_local/*.py 音源适配器。

约定：
- 目录内每个 .py（下划线开头的模板除外）在模块顶层调用 register_source(...)；
- 单个源崩溃不影响其他源，health_check 失败的源标记为不可用；
- 每次调用 load_sources 都会重建注册表（方便热改源后重启生效，M4 再做热重载）。
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

from .base import MusicSource, clear_registry, get_registry

log = logging.getLogger(__name__)

DEFAULT_SOURCES_DIR = Path(__file__).resolve().parent.parent.parent / "sources_local"


def load_sources(sources_dir: Path | None = None) -> dict[str, dict]:
    """扫描目录、加载全部适配器并探活。返回 {name: {healthy, error}}。"""
    sources_dir = sources_dir or DEFAULT_SOURCES_DIR
    clear_registry()
    status: dict[str, dict] = {}
    if not sources_dir.exists():
        log.warning("sources dir missing: %s", sources_dir)
        return status

    for py in sorted(sources_dir.glob("*.py")):
        if py.name.startswith("_"):
            continue  # 模板文件
        mod_name = f"sources_local_{py.stem}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, py)
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)
        except Exception as e:
            log.exception("加载音源失败: %s", py.name)
            status[py.stem] = {"healthy": False, "error": f"加载失败: {e}"}
            continue

    for name, src in get_registry().items():
        try:
            healthy = bool(src.health_check())
            status[name] = {"healthy": healthy, "error": "" if healthy else "health_check 未通过"}
        except Exception as e:
            log.exception("音源探活失败: %s", name)
            status[name] = {"healthy": False, "error": f"探活失败: {e}"}
    return status
