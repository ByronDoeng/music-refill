"""动态加载音源适配器目录。

约定：
- 目录内每个 .py（下划线开头的模板除外）在模块顶层调用 register_source(...)；
- 可叠加多个目录；同名源由后加载的目录覆盖，便于替换单个音源或整体回退；
- enabled / disabled 只按注册名过滤，不要求改文件名；
- 单个源崩溃不影响其他源，health_check 失败的源标记为不可用；
- 每次调用 load_sources 都会重建注册表；改源后重载或重启服务即可生效。
"""
from __future__ import annotations

import concurrent.futures
import importlib.util
import logging
import sys
from pathlib import Path

from .base import MusicSource, clear_registry, get_registry

log = logging.getLogger(__name__)

DEFAULT_SOURCES_DIR = Path(__file__).resolve().parent.parent.parent / "sources_local"


def configure_musicdl_workdir(workdir: Path) -> None:
    """musicdl 每次 search 都会写 work_dir；默认写到当前工作目录，越跑越大。

    这里把本进程的默认 work_dir 指到 data/musicdl（gitignored），
    既保留库行为，又不污染仓库根目录。
    """
    try:
        from musicdl.modules.sources import base as _base

        workdir = str(workdir)
        _base.BaseMusicClient.__init__.__defaults__ = _patch_defaults(
            _base.BaseMusicClient.__init__.__defaults__, workdir
        )
    except Exception:
        pass


def _patch_defaults(defaults, workdir: str):
    if not defaults:
        return defaults
    out = []
    for v in defaults:
        if v == "musicdl_outputs":
            out.append(workdir)
        else:
            out.append(v)
    return tuple(out)

def load_sources(
    sources_dir: Path | str | None = None,
    order: list[str] | None = None,
    workdir: Path | None = None,
    probe: bool = True,
    dirs: list[Path | str] | None = None,
    enabled: list[str] | None = None,
    disabled: list[str] | None = None,
) -> dict[str, dict]:
    """扫描目录、加载适配器并探活。返回 {name: {healthy, error, priority, ...}}。

    order 来自 config.toml 的 [sources].order：列表里的名字优先，
    未列出的源排在后面（保持文件名字典序），因此“换源/调优先级”不用改代码。
    dirs 可叠加多个目录；sources_dir 保留为单目录兼容参数。
    enabled 非空时只保留其中的注册名；disabled 永远排除，二者都支持热改配置。
    probe=False 时只加载不探活，供匹配/搜索快速启动；真正的搜索失败由各源
    自己的 try/except 处理，不会拖住整批任务。
    """
    search_dirs: list[Path] = []
    for value in (dirs or ([sources_dir] if sources_dir else [DEFAULT_SOURCES_DIR])):
        path = Path(value)
        if path not in search_dirs:
            search_dirs.append(path)
    order = order or []
    enabled = {str(x) for x in (enabled or []) if str(x).strip()}
    disabled = {str(x) for x in (disabled or []) if str(x).strip()}
    clear_registry()
    status: dict[str, dict] = {}
    if not search_dirs:
        log.warning("sources dir missing: no directory configured")
        return status

    loaded_from: dict[str, str] = {}
    broken: dict[str, dict] = {}
    for directory in search_dirs:
        if not directory.exists():
            log.warning("sources dir missing: %s", directory)
            continue
        for py in sorted(directory.glob("*.py")):
            if py.name.startswith("_"):
                continue  # 模板文件
            # 按绝对路径生成模块名：同名文件来自不同目录时不会复用旧模块。
            mod_name = f"music_refill_adapter_{py.stem}_{abs(hash(str(py.resolve())))}"
            before = dict(get_registry())
            try:
                spec = importlib.util.spec_from_file_location(mod_name, py)
                if spec is None or spec.loader is None:
                    raise ImportError("无法创建模块 spec")
                module = importlib.util.module_from_spec(spec)
                sys.modules[mod_name] = module
                spec.loader.exec_module(module)
            except Exception as e:
                log.exception("加载音源失败: %s", py)
                # 模块可能先注册再抛错；回滚到加载前的快照，避免半成品源进入搜索。
                registry = get_registry()
                registry.clear()
                registry.update(before)
                broken[py.stem] = {
                    "healthy": False,
                    "error": f"加载失败: {e}",
                    "path": str(py),
                }
                continue
            # 一个文件可能注册多个源，也可能覆盖已有同名源；用实例身份判断，
            # 不能只看新增的 key，否则覆盖时来源路径会停留在旧文件。
            for name, src in sorted(get_registry().items()):
                if before.get(name) is src:
                    continue
                overridden = name in loaded_from
                if overridden:
                    log.warning(
                        "音源重名，后加载的覆盖前者: %s (%s -> %s)",
                        name, loaded_from[name], py,
                    )
                loaded_from[name] = str(py)
                status[name] = {
                    "healthy": True,
                    "error": "",
                    "path": str(py),
                    "overridden": overridden,
                }

    # 按配置过滤注册表本身，而不仅是状态，避免搜索仍用被停用的源。
    registry = get_registry()
    for name in list(registry):
        if disabled and name in disabled:
            registry.pop(name, None)
        elif enabled and name not in enabled:
            registry.pop(name, None)

    for name, path in loaded_from.items():
        if name in registry and name in status:
            status[name].update({"path": path})
        elif name in registry:
            status.setdefault(
                name, {"healthy": True, "error": "", "path": path}
            )

    # 被禁用/未启用的源只从注册表过滤还不够，状态也必须是同一视图。
    status = {name: st for name, st in status.items() if name in registry}
    status.update(broken)

    def priority(name: str) -> tuple[int, str]:
        return (order.index(name) if name in order else len(order), name)

    def dedupe(items: list[tuple[str, MusicSource]]) -> list[tuple[str, MusicSource]]:
        """同一注册名在合并目录后只保留最后一个实例。

        register_source 已按 name 覆盖旧值，正常不会重复；这里再按 name 去重，
        防止适配器绕过注册表直接写入内部 dict 的历史用法污染探活列表。
        """
        seen: dict[str, tuple[str, MusicSource]] = {}
        for name, src in items:
            seen[name] = (name, src)
        return sorted(seen.values(), key=lambda kv: priority(kv[0]))

    adapters = dedupe(list(get_registry().items()))
    if not adapters:
        return status
    if workdir:
        configure_musicdl_workdir(workdir)
    if not probe:
        for name, _src in adapters:
            status.setdefault(name, {}).update(
                {"healthy": True, "error": "", "priority": priority(name)[0]}
            )
        return status

    def check(name: str, src) -> tuple[str, dict]:
        try:
            healthy = bool(src.health_check())
            return name, {
                "healthy": healthy,
                "error": "" if healthy else "health_check 未通过",
                "priority": priority(name)[0],
                **{k: v for k, v in status.get(name, {}).items() if k in {"path", "overridden"}},
            }
        except Exception as e:
            log.exception("音源探活失败: %s", name)
            return name, {
                "healthy": False, "error": f"探活失败: {e}", "priority": priority(name)[0],
                **{k: v for k, v in status.get(name, {}).items() if k in {"path", "overridden"}},
            }

    # 探活本身也是网络请求；并发执行避免两个源串行等 20 秒。
    keep = {name: status.get(name) for name in adapters}
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(adapters)))
    futures = {ex.submit(check, name, src): name for name, src in adapters}
    try:
        for fut in concurrent.futures.as_completed(futures, timeout=25):
            name, st = fut.result()
            status[name] = st
    except concurrent.futures.TimeoutError:
        log.warning("音源探活超时，未完成的源先标记为不可用")
    finally:
        for fut, name in futures.items():
            if not fut.done():
                fut.cancel()
                status[name] = {
                    "healthy": False, "error": "探活超时",
                    "priority": priority(name)[0],
                    **{k: v for k, v in (keep.get(name) or {}).items() if k in {"path", "overridden"}},
                }
        ex.shutdown(wait=False, cancel_futures=True)
    return status
