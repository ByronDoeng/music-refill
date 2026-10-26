"""音源协议与注册表。

设计约束：
- 音源实现与主程序解耦；每个目录下的 .py 通过 loader 动态加载。
- 可叠加多个目录；同名源由后加载的目录覆盖，便于单源替换。
- 可用 config.toml 的 enabled/disabled 按注册名启停，不影响其它源。
- 仓库自带酷我/咪咕适配器，也可把私有适配器放进独立目录。
- 搜索匹配以"歌名为主键、歌手仅参考"（歌单里搬运版歌曲的歌手名不可靠）。
- 源失效 = health_check 返回 False，自动跳过并在报告提示；换源 = 新增 .py 文件。
"""
from __future__ import annotations

import hashlib
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
    # 以下字段是可选的扩展，放在末尾以保证旧适配器的按位置传参仍然兼容。
    source_id: str = ""  # 音源内部 ID（用于二次解析下载地址）
    bitrate: int = 0     # 源声明的码率（bps）；未知时留 0，由扩展名估算
    resolved: bool = True
    # 适配器私有元数据（例如咪咕解析下载地址所需的 contentId/copyrightId）。
    # 它会随 /api/match/results 返回并在 /api/download 中原样回传，不能放不可序列化对象。
    source_meta: dict = field(default_factory=dict)
    # 音源已确认不可用（例如列表页显示无版权）。这类候选保留给人工判断，
    # 但不应进入“可下载版本”下拉。
    unavailable: bool = False

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def external_song_id(title: str, artist: str, album: str = "") -> int:
    """给“直接搜音源”的候选生成稳定负数 songId。

    这类候选没有网易曲库 songId，但仍要落 jobs/uploads。负数可避免与真实
    songId 混在一起，同时同一候选重复添加时保持稳定。
    """
    raw = f"{title}\0{artist}\0{album}".encode("utf-8")
    return -int(hashlib.md5(raw).hexdigest()[:12], 16)


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
