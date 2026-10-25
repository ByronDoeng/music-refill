"""音源适配器模板（复制到项目根目录 sources_local/ 下并去掉下划线前缀生效）。

步骤：
1. mkdir sources_local  （该目录被 .gitignore 排除，实现永远不进仓库）
2. cp music_refill/sources/_template.py sources_local/my_source.py
3. 实现三个成员：name / quality_ceiling / health_check / search
4. 重启服务，GUI 与 /api/sources 即可看到新源

接口约定见同目录 base.py。社区可参考的现成实现：
lx-music 自定义源（JS）、musicdl（pip 包，本项目的 requirements 已含）等。
"""
from __future__ import annotations

from music_refill.sources.base import SourceTrack, register_source


class MySource:
    name = "my_source"        # 唯一标识，展示在候选与报告中
    quality_ceiling = "320"   # "flac" | "320" | "128"

    def health_check(self) -> bool:
        # 轻量探活：能搜到结果即健康；失败自动跳过该源
        try:
            return bool(self.search("晴天", limit=1))
        except Exception:
            return False

    def search(self, title: str, artist: str = "", limit: int = 5) -> list[SourceTrack]:
        # TODO: 请求你的音源接口，把结果映射为 SourceTrack
        # 必填：source/title/artist/ext/size_bytes/duration_s/download_url
        # 可选：album/headers/cookies/cover_url/lyric
        return []


register_source(MySource())
