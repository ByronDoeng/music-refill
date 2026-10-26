"""音源适配器模板（复制到 sources_local/ 下并去掉下划线前缀生效）。

步骤：
1. mkdir -p sources_local  （仓库已自带该目录与酷我/咪咕实现）
2. cp music_refill/sources/_template.py sources_local/my_source.py
3. 实现三个成员：name / quality_ceiling / health_check / search
4. 调用 POST /api/sources/reload 或重启服务，即可看到新源

重要：musicdl 客户端请在 search() 里懒创建（不要放模块顶层/__init__），
这样主程序才能把它的中间产物目录改到 data/musicdl，避免仓库根目录膨胀。

接口约定见 music_refill/sources/base.py。也可以把 [sources].dirs 配成多个目录，
叠加加载；后加载的同名源覆盖前者，方便只替换单个音源。
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

    def search(self, title: str, artist: str = "", limit: int = 5):
        raise NotImplementedError("实现 search：返回 list[SourceTrack]")

    def search(self, title: str, artist: str = "", limit: int = 5) -> list[SourceTrack]:
        # TODO: 请求你的音源接口，把结果映射为 SourceTrack
        # 必填：source/title/artist/ext/size_bytes/duration_s/download_url
        # 可选：album/headers/cookies/cover_url/lyric
        return []


register_source(MySource())
