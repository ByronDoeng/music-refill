"""咪咕音乐源（基于 musicdl 的 MiguMusicClient，本地文件，不入库）。

musicdl 的 MiguMusicClient._search() 会为搜索结果逐条请求 listen-url 并探测
文件，8 条候选串行约 6 秒；但“搜索列表”本身只需 0.3 秒左右。这里拆成：

1. quick_search()：只取搜索列表元信息，立刻返回（resolved=False）；
2. resolve()：为该条候选请求最高可用音质的播放地址并探测真实文件；
3. resolve_many()：候选之间并发解析。

保留 search() 作为协议兜底：未实现 quick_search 的调用方仍可直接使用。
"""
from __future__ import annotations

import concurrent.futures
import re
import time
from urllib.parse import urljoin

import requests
from musicdl.modules.sources.base import AudioLinkTester
from musicdl.modules.sources.migu import MiguMusicClient
from musicdl.modules.utils import legalizestring, safeextractfromdict

from music_refill.sources.base import SourceTrack, register_source


class MiguSource:
    name = "migu"
    quality_ceiling = "flac"

    def __init__(self):
        self._healthy_until = 0.0

    def _get_client(self) -> MiguMusicClient:
        """每次搜索用独立 client，避免并发搜索共享 session/分页参数。"""
        return MiguMusicClient(search_size_per_source=8, search_size_per_page=8)

    def health_check(self) -> bool:
        if time.time() < self._healthy_until:
            return True
        try:
            self.quick_search("晴天", limit=1)
            self._healthy_until = time.time() + 600
            return True
        except Exception:
            return False

    def _new_client(self) -> MiguMusicClient:
        """解析下载地址会在实例上保存 session/进度状态，并发时必须各用各的。"""
        return MiguMusicClient(search_size_per_source=8, search_size_per_page=8)

    # ---------- 快速元信息列表 ----------

    def quick_search(self, title: str, artist: str = "", limit: int = 5) -> list[SourceTrack]:
        client = self._get_client()
        keyword = " ".join(x for x in (title, artist) if x).strip() or title
        page_size = max(10, min(30, limit * 2))
        client.search_size_per_source = page_size
        client.search_size_per_page = page_size
        try:
            resp = client.get(client._constructsearchurls(keyword)[0])
            data = client._decryptresp(resp=resp)
        except Exception:
            return []
        rows = safeextractfromdict(data, ["songResultData", "result"], []) or []
        out: list[SourceTrack] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            content_id = str(row.get("contentId") or "")
            copyright_id = str(row.get("copyrightId") or "")
            if not content_id or not copyright_id:
                continue
            singers = [
                str(s.get("name"))
                for s in (row.get("singers") or row.get("singerList") or [])
                if isinstance(s, dict) and s.get("name")
            ]
            albums = [
                str(a.get("name"))
                for a in (row.get("albums") or [])
                if isinstance(a, dict) and a.get("name")
            ]
            cover = (
                safeextractfromdict(row, ["imgItems", -1, "img"], None)
                or next((row.get(k) for k in ("img3", "img2", "img1") if row.get(k)), "")
            )
            if cover and not str(cover).startswith("http"):
                cover = urljoin("https://d.musicapp.migu.cn", str(cover))
            # 列表接口给出最高可用格式的扩展名；实际 ext 由 resolve() 探测覆盖。
            formats = (
                (row.get("rateFormats") or [])
                + (row.get("newRateFormats") or [])
                + (row.get("audioFormats") or [])
            )
            best = max(
                (f for f in formats if isinstance(f, dict)),
                key=lambda f: _size_mb(f),
                default={},
            )
            ext = str(best.get("androidFileType") or best.get("fileType") or "mp3").lower()
            out.append(
                SourceTrack(
                    source=self.name,
                    title=str(row.get("name") or row.get("songName") or ""),
                    artist="、".join(singers),
                    album=albums[0] if albums else "",
                    ext=ext,
                    duration_s=int(float(safeextractfromdict(row, ["duration"], 0) or 0)),
                    cover_url=str(cover or ""),
                    source_id=content_id,
                    source_meta={
                        "contentId": content_id,
                        "copyrightId": copyright_id,
                        # 解析器靠这张表选择可用音质；只保留必要字段，避免把整条
                        # 搜索结果塞进任务 payload。
                        "formats": [
                            {
                                k: f.get(k)
                                for k in ("formatType", "resourceType", "size", "fileType",
                                          "androidFileType", "iosFileType", "androidSize", "iosSize")
                                if f.get(k) not in (None, "")
                            }
                            for f in formats if isinstance(f, dict)
                        ],
                    },
                    resolved=False,
                )
            )
            if len(out) >= limit:
                break
        return out

    # ---------- 下载地址解析 + 探测 ----------

    def resolve(self, track: SourceTrack) -> SourceTrack | None:
        meta = dict(track.source_meta or {})
        content_id = str(meta.get("contentId") or track.source_id or "")
        copyright_id = str(meta.get("copyrightId") or "")
        if not content_id or not copyright_id:
            return None
        # 复用 musicdl 的解析逻辑（含音质优先级、加密响应解密），但每次用独立
        # client，避免并发线程共用 session。
        client = self._new_client()
        row = {
            "contentId": content_id,
            "copyrightId": copyright_id,
            "name": track.title,
            "singers": [{"name": x} for x in re.split(r"[、,，/&;；]+", track.artist or "") if x],
            "rateFormats": list(meta.get("formats") or []),
        }
        try:
            info = client._parsewithofficialapiv1(
                search_result=row, lossless_quality_is_sufficient=False,
            )
        except Exception:
            return None
        status = getattr(info, "download_url_status", None) or {}
        url = getattr(info, "download_url", "") or ""
        if not url or not status.get("ok"):
            return None
        ext = str(getattr(info, "ext", "") or status.get("ext") or track.ext or "mp3").lower()
        if ext not in AudioLinkTester.VALID_AUDIO_EXTS:
            return None
        return SourceTrack(
            source=track.source,
            title=str(getattr(info, "song_name", "") or track.title),
            artist=str(getattr(info, "singers", "") or track.artist),
            album=str(getattr(info, "album", "") or track.album),
            ext=ext,
            size_bytes=int(getattr(info, "file_size_bytes", 0) or status.get("file_size_bytes") or 0),
            duration_s=int(getattr(info, "duration_s", 0) or track.duration_s),
            download_url=url,
            headers=dict(getattr(info, "default_download_headers", None) or {}),
            cookies=dict(getattr(info, "default_download_cookies", None) or {}),
            cover_url=str(getattr(info, "cover_url", "") or track.cover_url),
            lyric=str(getattr(info, "lyric", "") or ""),
            source_id=content_id,
            source_meta=meta,
            resolved=True,
        )

    def resolve_many(self, tracks: list[SourceTrack], workers: int = 5) -> list[SourceTrack]:
        if not tracks:
            return []
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(tracks))) as ex:
            done = list(ex.map(self.resolve, tracks))
        return [item for item in done if item]

    # ---------- 兼容协议：search() = 元信息 + 解析 ----------

    def search(self, title: str, artist: str = "", limit: int = 5) -> list[SourceTrack]:
        tracks = self.quick_search(title, artist=artist, limit=limit)
        return self.resolve_many(tracks) or []


def _size_mb(meta: dict) -> float:
    raw = str(
        meta.get("size") or meta.get("iosSize") or meta.get("androidSize")
        or meta.get("isize") or meta.get("asize") or "0"
    ).removesuffix("MB").strip()
    try:
        return float(raw)
    except ValueError:
        return 0.0


register_source(MiguSource())
