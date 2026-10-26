"""配置加载：config.toml（本地，gitignored）→ 默认值兜底。

所有运行参数都在这里集中声明，业务代码只读 Config，不直接写死数值。
"""
from __future__ import annotations

import dataclasses
import os
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclasses.dataclass
class ServerCfg:
    host: str = "127.0.0.1"
    port: int = 18123


@dataclasses.dataclass
class ScanCfg:
    include_liked: bool = True
    playlist_ids: list[int] = dataclasses.field(default_factory=list)
    quality: str = "flac"  # flac | 320 | 128
    limit: int = 50        # 单批上传上限（1~50）
    sleep_min: float = 3.0
    sleep_max: float = 10.0
    match_sleep_min: float = 0.5
    match_sleep_max: float = 1.5
    # 单个音源完成一次搜索的时间上限。寒潮/酷我接口偶尔 15 秒才返回，
    # 不能像旧版 12 秒那样在结果到达前就取消。
    match_source_timeout_s: float = 30.0
    source_search_timeout_s: float = 30.0
    # 搜索补歌的首批等待：酷我通常在 1 秒内返回，咪咕要几秒。超时先返回已完成
    # 音源，其余继续在后台跑，页面可先展示候选；仍受 source_search_timeout_s 封顶。
    source_search_first_batch_s: float = 3.0
    upload_attempts: int = 2
    retry_backoff_s: float = 5.0
    retry_backoff_max_s: float = 40.0
    # 按歌名快速补歌：最高分达到此阈值才允许跳过候选确认直接入队
    quick_add_min_score: float = 1.15


@dataclasses.dataclass
class PathsCfg:
    data_dir: Path = PROJECT_ROOT / "data"

    @property
    def cookies(self) -> Path:
        return self.data_dir / "cookies.json"

    @property
    def db(self) -> Path:
        return self.data_dir / "music_refill.db"

    @property
    def reports(self) -> Path:
        return self.data_dir / "reports"

    @property
    def logs(self) -> Path:
        return self.data_dir / "logs"

    @property
    def browser_profile(self) -> Path:
        return self.data_dir / "browser_profile"

    @property
    def musicdl_workdir(self) -> Path:
        # 第三方搜索库的中间产物放数据目录，随缓存一起清理，不进仓库
        return self.data_dir / "musicdl"


@dataclasses.dataclass
class SourcesCfg:
    order: list[str] = dataclasses.field(default_factory=list)
    # 兼容旧配置：单目录。优先级低于 dirs。
    dir: str = ""
    # 多个目录按顺序叠加加载；后面的目录可覆盖同名源。
    # 留空时使用 dirs（或仓库自带 sources_local/）。
    dirs: list[str] = dataclasses.field(default_factory=list)
    # 只加载这些源；留空表示全部。用于临时停用某个源而不删文件。
    enabled: list[str] = dataclasses.field(default_factory=list)
    # 强制排除某些源；优先级高于 enabled。
    disabled: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class BrowserCfg:
    # 留空 = 自动查找 Chrome / Edge / Chromium
    executable: str = ""
    debug_port: int = 9222
    login_timeout_s: int = 300


@dataclasses.dataclass
class Config:
    server: ServerCfg = dataclasses.field(default_factory=ServerCfg)
    scan: ScanCfg = dataclasses.field(default_factory=ScanCfg)
    paths: PathsCfg = dataclasses.field(default_factory=PathsCfg)
    sources: SourcesCfg = dataclasses.field(default_factory=SourcesCfg)
    browser: BrowserCfg = dataclasses.field(default_factory=BrowserCfg)


def load() -> Config:
    cfg_file = PROJECT_ROOT / "config.toml"
    cfg = Config()
    if not cfg_file.exists():
        return _finalize(cfg)
    raw = tomllib.loads(cfg_file.read_text(encoding="utf-8"))
    if "server" in raw:
        cfg.server = ServerCfg(**{k: v for k, v in raw["server"].items() if k in ServerCfg.__dataclass_fields__})
    if "scan" in raw:
        s = raw["scan"]
        cfg.scan = ScanCfg(**{k: v for k, v in s.items() if k in ScanCfg.__dataclass_fields__})
    if "paths" in raw and "data_dir" in raw["paths"]:
        cfg.paths.data_dir = Path(raw["paths"]["data_dir"])
    if "sources" in raw:
        cfg.sources = SourcesCfg(**{k: v for k, v in raw["sources"].items() if k in SourcesCfg.__dataclass_fields__})
    if "browser" in raw:
        cfg.browser = BrowserCfg(**{k: v for k, v in raw["browser"].items() if k in BrowserCfg.__dataclass_fields__})
    return _finalize(cfg)


def _finalize(cfg: Config) -> Config:
    """校验并归一化配置；环境变量优先级最高，便于测试/便携运行。"""
    if not cfg.paths.data_dir.is_absolute():
        cfg.paths.data_dir = PROJECT_ROOT / cfg.paths.data_dir
    if cfg.sources.dir:
        cfg.sources.dir = str(resolve_sources_dir(cfg.sources.dir))
    dirs: list[str] = []
    for value in cfg.sources.dirs or ([cfg.sources.dir] if cfg.sources.dir else []):
        resolved = str(resolve_sources_dir(str(value)))
        if resolved not in dirs:
            dirs.append(resolved)
    cfg.sources.dirs = dirs
    cfg.sources.enabled = [str(x).strip() for x in cfg.sources.enabled if str(x).strip()]
    cfg.sources.disabled = [str(x).strip() for x in cfg.sources.disabled if str(x).strip()]
    # 测试/便携场景覆盖数据目录，必须在读取 config.toml 之后应用
    if os.environ.get("MUSIC_REFILL_DATA_DIR"):
        cfg.paths.data_dir = Path(os.environ["MUSIC_REFILL_DATA_DIR"]).expanduser()
    if not cfg.paths.data_dir.is_absolute():
        cfg.paths.data_dir = PROJECT_ROOT / cfg.paths.data_dir

    cfg.scan.limit = max(1, min(50, int(cfg.scan.limit)))
    if cfg.scan.quality not in {"flac", "320", "128"}:
        raise ValueError("scan.quality 只能是 flac / 320 / 128")
    if cfg.scan.sleep_min < 0 or cfg.scan.sleep_max < cfg.scan.sleep_min:
        raise ValueError("scan.sleep_min/sleep_max 不合法")
    cfg.scan.match_source_timeout_s = max(5.0, float(cfg.scan.match_source_timeout_s))
    cfg.scan.source_search_timeout_s = max(5.0, float(cfg.scan.source_search_timeout_s))
    cfg.scan.source_search_first_batch_s = max(
        0.5, min(cfg.scan.source_search_timeout_s, float(cfg.scan.source_search_first_batch_s))
    )
    cfg.scan.upload_attempts = max(1, int(cfg.scan.upload_attempts))
    cfg.scan.quick_add_min_score = max(0.0, float(cfg.scan.quick_add_min_score))
    cfg.browser.debug_port = max(1024, min(65535, int(cfg.browser.debug_port)))
    cfg.browser.login_timeout_s = max(10, int(cfg.browser.login_timeout_s))
    return cfg


def resolve_sources_dir(value: str) -> Path:
    """把配置中的音源目录解析成绝对路径；相对路径以项目根目录为基准。"""
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path
