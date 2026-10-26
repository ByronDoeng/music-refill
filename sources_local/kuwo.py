"""酷我音乐源（本地文件，不入库）。

musicdl 的 KuwoMusicClient.search() 会为每条搜索结果串行解析下载地址，实测
8 条候选约 10~20 秒；但酷我“搜索列表”接口本身只需 0.3 秒。这里改为：

1. 用官方 searchMusicBykeyWord 拿元信息（快）；
2. 并发用 convert_url2 解析下载地址（每首约 0.1~0.5 秒）；
3. 用 AudioLinkTester 并发探测文件真实类型/大小。

仍保留 musicdl 作为兜底：接口结构变化或解析失败时回退到 client.search()。
注意 convert_url2 的 flac 失败后才能请求 mp3；320kmp3 在无版权时返回的是
96kbps wma，不能误标为 320。
"""
from __future__ import annotations

import concurrent.futures
import contextlib
import io
import re
import time

import requests
from musicdl.modules.sources.base import AudioLinkTester
from musicdl.modules.sources.kuwo import KuwoMusicClient, KuwoMusicClientUtils

from music_refill.sources.base import SourceTrack, register_source


class KuwoSource:
    name = "kuwo"
    quality_ceiling = "flac"

    def __init__(self):
        self._healthy_until = 0.0

    def _get_client(self) -> KuwoMusicClient:
        """每次搜索用独立 client。

        musicdl 客户端在实例上保存 session/分页参数；复用单例会让并发的两次
        搜索互相等待，甚至把后一次拖到前一次慢源结束。创建工作目录已由 loader
        改为进程级默认值，所以新建 client 不会污染仓库根目录。
        """
        return KuwoMusicClient(search_size_per_source=16)

    def health_check(self) -> bool:
        if time.time() < self._healthy_until:
            return True
        try:
            self.quick_search("晴天", limit=1)
            self._healthy_until = time.time() + 600
            return True
        except Exception:
            return False

    # ---------- 快速元信息列表 ----------

    def quick_search(self, title: str, artist: str = "", limit: int = 5) -> list[SourceTrack]:
        client = self._get_client()
        keyword = " ".join(x for x in (title, artist) if x).strip() or title
        # 一次拿稍多候选，给“标题/歌手”排序留余量；limit 只限制最终返回数量。
        page_size = max(10, min(30, limit * 2))
        client.search_size_per_source = page_size
        client.search_size_per_page = page_size
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                resp = client.get(client._constructsearchurls(keyword)[0])
            rows = resp.json().get("abslist") or []
        except Exception:
            return []
        out: list[SourceTrack] = []
        for row in rows:
            rid = str(row.get("MUSICRID") or row.get("musicrid") or "").removeprefix("MUSIC_")
            if not rid:
                continue
            out.append(
                SourceTrack(
                    source=self.name,
                    source_id=rid,
                    title=str(row.get("SONGNAME") or row.get("name") or ""),
                    artist=str(row.get("ARTIST") or row.get("artist") or ""),
                    album=str(row.get("ALBUM") or row.get("album") or ""),
                    ext=str(row.get("FORMAT") or "mp3").lower(),
                    bitrate=0,
                    size_bytes=0,
                    duration_s=int(float(row.get("DURATION") or row.get("duration") or 0)),
                    download_url="",
                    cover_url=str(row.get("hts_MVPIC") or row.get("web_albumpic_short") or ""),
                    resolved=False,
                )
            )
            if len(out) >= limit:
                break
        return out

    # ---------- 下载地址解析 + 探测 ----------

    @staticmethod
    def _fetch_url(song_id: str) -> str:
        client = KuwoMusicClientUtils
        for fmt in ("flac", "mp3"):
            query = (
                "user=0&corp=kuwo&source=kwplayer_ar_5.1.0.0_B_jiakong_vh.apk"
                f"&p2p=1&type=convert_url2&sig=0&format={fmt}&rid={song_id}"
            )
            try:
                resp = requests.get(
                    f"http://mobi.kuwo.cn/mobi.s?f=kuwo&q={client.encryptquery(query)}",
                    headers={"User-Agent": "okhttp/3.10.0"},
                    timeout=8,
                )
                match = re.search(r'http[^\s$"]+', resp.text)
                if match:
                    return match.group(0)
            except Exception:
                continue
        return ""

    def resolve(self, track: SourceTrack) -> SourceTrack | None:
        """补下载地址与真实文件类型；成功返回可下载副本，失败返回 None。"""
        song_id = str(getattr(track, "source_id", "") or "")
        if not song_id:
            return None
        url = self._fetch_url(song_id)
        if not url:
            return None
        status = AudioLinkTester(timeout=(3, 6)).test(url, renew_session=True)
        if not status.get("ok"):
            return None
        ext = str(status.get("ext") or track.ext or "mp3").lower()
        if ext in {"mflac", "mgg", "qmcflac", "qmc0", "qmc3", "qmcogg"}:
            return None
        return SourceTrack(
            source=track.source,
            source_id=track.source_id,
            title=track.title,
            artist=track.artist,
            album=track.album,
            ext=ext,
            bitrate=0,
            size_bytes=int(status.get("file_size_bytes") or 0),
            duration_s=track.duration_s,
            download_url=status.get("download_url") or url,
            headers=dict(track.headers or {}),
            cookies=dict(track.cookies or {}),
            cover_url=track.cover_url,
            lyric=track.lyric,
            resolved=True,
        )

    def resolve_many(self, tracks: list[SourceTrack], workers: int = 6) -> list[SourceTrack]:
        """并发解析多条候选，按原顺序返回成功结果。"""
        if not tracks:
            return []
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(tracks))) as ex:
            done = list(ex.map(self.resolve, tracks))
        return [item for item in done if item]

    # ---------- 兼容协议：search() = 元信息 + 解析 ----------

    def search(self, title: str, artist: str = "", limit: int = 5) -> list[SourceTrack]:
        tracks = self.quick_search(title, artist=artist, limit=limit)
        resolved = self.resolve_many(tracks)
        if resolved:
            return resolved
        # 官方接口变化时回退 musicdl（较慢，但能保证可用性）
        client = self._get_client()
        with contextlib.redirect_stdout(io.StringIO()):
            results = client.search(title, num_threadings=1)
        out: list[SourceTrack] = []
        for si in results:
            status = getattr(si, "download_url_status", None) or {}
            url = getattr(si, "download_url", None)
            if not url or not status.get("ok"):
                continue
            out.append(
                SourceTrack(
                    source=self.name,
                    title=str(getattr(si, "song_name", "") or ""),
                    artist=str(getattr(si, "singers", "") or ""),
                    album=str(getattr(si, "album", "") or ""),
                    ext=str(getattr(si, "ext", "") or status.get("ext") or "mp3").lower(),
                    size_bytes=int(getattr(si, "file_size_bytes", 0) or 0),
                    duration_s=int(getattr(si, "duration_s", 0) or 0),
                    download_url=url,
                    headers=dict(getattr(si, "default_download_headers", None) or {}),
                    cookies=dict(getattr(si, "default_download_cookies", None) or {}),
                    cover_url=str(getattr(si, "cover_url", "") or ""),
                    lyric=str(getattr(si, "lyric", "") or ""),
                )
            )
            if len(out) >= limit:
                break
        return out


register_source(KuwoSource())
