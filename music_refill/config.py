"""配置加载：config.toml（本地，gitignored）→ 默认值兜底。"""
from __future__ import annotations

import dataclasses
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
    limit: int = 50
    sleep_min: float = 3.0
    sleep_max: float = 10.0


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


@dataclasses.dataclass
class SourcesCfg:
    order: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class Config:
    server: ServerCfg = dataclasses.field(default_factory=ServerCfg)
    scan: ScanCfg = dataclasses.field(default_factory=ScanCfg)
    paths: PathsCfg = dataclasses.field(default_factory=PathsCfg)
    sources: SourcesCfg = dataclasses.field(default_factory=SourcesCfg)


def load() -> Config:
    cfg_file = PROJECT_ROOT / "config.toml"
    cfg = Config()
    if not cfg_file.exists():
        return cfg
    raw = tomllib.loads(cfg_file.read_text(encoding="utf-8"))
    if "server" in raw:
        cfg.server = ServerCfg(**{k: v for k, v in raw["server"].items() if k in ServerCfg.__dataclass_fields__})
    if "scan" in raw:
        s = raw["scan"]
        cfg.scan = ScanCfg(**{k: v for k, v in s.items() if k in ScanCfg.__dataclass_fields__})
    if "paths" in raw and "data_dir" in raw["paths"]:
        cfg.paths.data_dir = Path(raw["paths"]["data_dir"])
        if not cfg.paths.data_dir.is_absolute():
            cfg.paths.data_dir = PROJECT_ROOT / cfg.paths.data_dir
    if "sources" in raw:
        cfg.sources = SourcesCfg(**{k: v for k, v in raw["sources"].items() if k in SourcesCfg.__dataclass_fields__})
    return cfg
