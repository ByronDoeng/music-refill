"""音源候选打分（歌名为主键）+ 下载 + mutagen 打标。

打分规则（DECISIONS 2026-10-25：用户歌单大量搬运版歌曲，歌手名不可靠）：
  标题归一化完全相等 +1.0 ｜ 互相包含 +0.7 ｜ 编辑距离相似度×0.6
  歌手互相包含 +0.15（仅参考）
  时长±3s +0.3 ｜ ±8s +0.1 ｜ 超出 -0.2
  flac +0.05；目标非 Live/Cover 而候选是 -0.3
"""
from __future__ import annotations

import difflib
import random
import re
import time
from pathlib import Path

import requests

from ..sources.base import SourceTrack

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
BAD_VARIANTS = ("live", "cover", "remix", "demo", "instrumental", "伴奏", "翻唱", "现场", "cover")

AUTO_CONFIRM_THRESHOLD = 0.8  # 达到此分的候选在确认页默认勾选


def _norm(s: str) -> str:
    return "".join(strip_brackets(s or "").lower().split())


def strip_brackets(title: str) -> str:
    """去括号后缀：男孩(Live) -> 男孩、Love（现场版） -> Love。

    歌单里的 (Live)/(翻唱)/（DJ版）后缀多是"当年找不到正版源"的妥协，
    搜音源时应剥掉找原曲（用户 2026-10-25 反馈）。支持全角括号与方括号。
    """
    return re.sub(r"[（(【\[][^）)】\]]*[）)】\]]", "", title or "").strip()


def score_track(t: SourceTrack, title: str, artist: str, duration_ms: int) -> float:
    """歌名为主键：目标标题先剥括号（男孩(Live)→男孩），候选与之比较。

    若候选也带同类后缀（live/翻唱…）而剥后目标没有 → 降权（原曲优先）。
    """
    base = strip_brackets(title)
    nb, nt = _norm(base), _norm(title)
    tt = _norm(t.title)
    tb = _norm(strip_brackets(t.title))
    if not nb or not tt:
        return 0.0
    # 与"剥括号目标"和"完整目标"分别比，取高者
    def sim(a: str, b: str) -> float:
        if not a or not b:
            return 0.0
        if a == b:
            return 1.0
        if a in b or b in a:
            return 0.7
        return difflib.SequenceMatcher(None, a, b).ratio() * 0.6

    s = max(sim(nb, tt), sim(nt, tt), sim(nb, tb))
    na, ta = _norm(artist), _norm(t.artist)
    if na and ta and (na in ta or ta in na):
        s += 0.15
    if duration_ms and t.duration_s:
        diff = abs(t.duration_s * 1000 - duration_ms)
        if diff <= 3000:
            s += 0.3
        elif diff <= 8000:
            s += 0.1
        else:
            s -= 0.2
    if t.ext.lower() == "flac":
        s += 0.05
    if not any(b in nb for b in BAD_VARIANTS) and any(b in tt for b in BAD_VARIANTS):
        s -= 0.3  # 目标是原曲、候选是 Live/翻唱等变体 → 降权
    return round(s, 3)


def rank_candidates(song: dict, tracks: list[SourceTrack]) -> list[SourceTrack]:
    """就地打分、内容级去重并排序（降序）。

    打分参照系优先用"库内原版"（orig_artist/orig_duration_ms，来自站内搜索）：
    歌单条目常是搬运版（歌手随机、时长可疑），拿它当基准会把正版压到最后。
    去重键为内容级（同源+标题+歌手+音质+时长）——kuwo 等源同一首歌每次 URL 不同。
    """
    artist = song.get("orig_artist") or "、".join(song.get("artists") or [])
    duration = int(song.get("orig_duration_ms") or song.get("duration_ms") or 0)
    seen: set = set()
    uniq = []
    for t in tracks:
        key = (t.source, _norm(t.title), _norm(t.artist), t.ext, t.duration_s)
        if key in seen:
            continue
        seen.add(key)
        t.score = score_track(t, song.get("name") or "", artist, duration)
        uniq.append(t)
    return sorted(uniq, key=lambda t: (-t.score, t.source))


def _sanitize(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", (s or "").strip()) or "unknown"


def download_track(t: SourceTrack, dest_dir: Path, title: str, artist: str, progress_cb=None) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{_sanitize(artist)} - {_sanitize(title)}.{t.ext}"
    tmp = dest.with_suffix(dest.suffix + ".part")
    with requests.get(
        t.download_url,
        headers={**t.headers, "User-Agent": UA},
        cookies=t.cookies,
        stream=True,
        timeout=120,
    ) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or t.size_bytes or 0)
        recv = 0
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                if chunk:
                    f.write(chunk)
                    recv += len(chunk)
                    if progress_cb:
                        try:
                            progress_cb(recv, total)
                        except Exception:
                            pass
    tmp.replace(dest)
    return dest


def tag_file(
    path: Path,
    title: str,
    artist: str,
    album: str,
    cover_url: str = "",
    lyric: str = "",
) -> None:
    """按曲库官方元数据打标 + 嵌封面/歌词。打标质量决定云盘匹配率。"""
    cover_bytes = b""
    if cover_url:
        try:
            r = requests.get(cover_url, headers={"User-Agent": UA}, timeout=20)
            if r.ok:
                cover_bytes = r.content
        except Exception:
            pass

    ext = path.suffix.lower().lstrip(".")
    if ext == "flac":
        from mutagen.flac import FLAC, Picture

        audio = FLAC(path)
        audio["title"], audio["artist"], audio["album"] = title, artist, album
        if lyric:
            audio["lyrics"] = lyric
        if cover_bytes:
            pic = Picture()
            pic.type = 3  # front cover
            pic.mime = "image/jpeg"
            pic.data = cover_bytes
            audio.clear_pictures()
            audio.add_picture(pic)
        audio.save()
    elif ext == "mp3":
        from mutagen.id3 import APIC, ID3, TALB, TIT2, TPE1, USLT
        from mutagen.mp3 import MP3

        try:
            tags = MP3(path).tags or ID3()
        except Exception:
            tags = ID3()
        tags.delall("TIT2"), tags.delall("TPE1"), tags.delall("TALB")
        tags.add(TIT2(encoding=3, text=title))
        tags.add(TPE1(encoding=3, text=artist))
        if album:
            tags.add(TALB(encoding=3, text=album))
        if lyric:
            tags.add(USLT(encoding=3, lang="chi", desc="", text=lyric))
        if cover_bytes:
            tags.delall("APIC")
            tags.add(APIC(encoding=3, mime="image/jpeg", type=3, data=cover_bytes))
        tags.save(path)
    # 其他格式暂不处理（M2 只关心 mp3/flac）


def sleep_between(smin: float, smax: float) -> None:
    time.sleep(random.uniform(smin, smax))
