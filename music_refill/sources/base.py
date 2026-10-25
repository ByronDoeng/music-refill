"""音源协议与注册表。

设计约束（docs/plan.md FR-5 / §4.5）：
- 主包不含任何音源实现；sources_local/*.py 通过 loader 动态加载，永不进 git。
- 搜索匹配以"歌名为主键、歌手仅参考"（用户歌单大量搬运版歌曲，DECISIONS 2026-10-25）。
- 源失效 = health_check 返回 False，自动跳过并在报告提示；换源 = 新增 .py 文件。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

# 注册表：name -> 实例（由 loader 填充）
_registry: dict[str, "MusicSource"] = {}


@dataclass
class SourceTrack:
    """一个可下载候选。"""

    source: str          # 来源标识，如 "migu"
    title: str
    artist: str
    album: str = ""
    ext: str = "mp3"     # mp3 / flac / ...
    size_bytes: int = 0
    duration_s: int = 0
    download_url: str = ""
    headers: dict = field(default_factory=dict)
    cookies: dict = field(default_factory=dict)
    cover_url: str = ""
    lyric: str = ""
    score: float = 0.0   # 由 core.downloader.score_track 计算

    def as_dict(self) -> dict:
        return dict(self.__dict__)


class MusicSource(Protocol):
    """音源适配器协议。sources_local 里的实现按此协议编写。"""

    name: str
    quality_ceiling: str  # "flac" | "320" | "128"

    def health_check(self) -> bool:
        """探活：源接口可达。失败自动跳过。"""
        ...

    def search(self, title: str, artist: str = "", limit: int = 5) -> list[SourceTrack]:
        """按歌名搜索候选（歌名为主键，artist 仅辅助）。"""
        ...


def register_source(instance: MusicSource) -> MusicSource:
    """sources_local 模块在顶部调用：register_source(MiguSource())。"""
    _registry[instance.name] = instance
    return instance


def get_registry() -> dict[str, "MusicSource"]:
    return _registry


def clear_registry() -> None:
    _registry.clear()
