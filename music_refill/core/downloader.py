"""音源候选打分（歌名为主键）+ 下载 + mutagen 打标。

打分规则（歌单里常有搬运版歌曲，歌手名不可靠，因此以标题为主）：
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

# 音质上限 → 允许的扩展名。顺序即“从高到低”，用于把超上限的候选降级/剔除。
QUALITY_ORDER = {"flac": 3, "320": 2, "128": 1}
EXT_RANK = {"flac": 3, "wav": 3, "ape": 3, "320": 2, "mp3": 2, "128": 1, "m4a": 1, "aac": 1}
QUALITY_LABELS = {"flac": "FLAC", "320": "320kbps", "128": "128kbps"}
QUALITY_BITRATES = {"flac": 999000, "320": 320000, "128": 128000}


def ext_rank(ext: str) -> int:
    return EXT_RANK.get((ext or "").lower(), 1)


def track_quality(track: SourceTrack) -> str:
    """把候选归一成 flac / 320 / 128 三档。

    优先使用音源声明的 bitrate；未知时按扩展名估算（mp3 视为 320）。
    这是一致性过滤，不是音频码率鉴伪。
    """
    ext = (track.ext or "").lower()
    if ext in {"flac", "wav", "ape"}:
        return "flac"
    bitrate = int(track.bitrate or 0)
    if bitrate >= 256000:
        return "320"
    if bitrate and bitrate <= 160000:
        return "128"
    return "128" if ext in {"m4a", "aac"} else "320"


def quality_label(quality: str) -> str:
    return QUALITY_LABELS.get((quality or "").lower(), quality or "未知")


def choose_quality(available: list[str], desired: str) -> str | None:
    """按“精确 → 最近更高 → 最近更低”选实际音质。"""
    desired = (desired or "flac").lower()
    ranks = {q: QUALITY_ORDER.get(q, 0) for q in available}
    if desired in ranks:
        return desired
    want = QUALITY_ORDER.get(desired, 3)
    higher = [q for q, r in ranks.items() if r > want]
    if higher:
        return min(higher, key=lambda q: ranks[q])
    lower = [q for q, r in ranks.items() if r < want]
    if lower:
        return max(lower, key=lambda q: ranks[q])
    return None


def allow_track(track: SourceTrack, quality: str) -> bool:
    """候选是否满足配置的音质上限。

    说明：mp3 无法从扩展名区分 320/128，按 320 处理；若用户设 128，
    仍允许 mp3（源不提供码率时不能误杀），但 flac 会被挡掉。
    """
    ceiling = QUALITY_ORDER.get((quality or "flac").lower(), 3)
    rank = ext_rank(track.ext)
    if ceiling >= 3:
        return True
    if ceiling == 2:
        return rank <= 2
    return rank <= 2 if track.ext.lower() == "mp3" else rank <= 1


def _norm(s: str) -> str:
    return "".join(strip_brackets(s or "").lower().split())


def strip_brackets(title: str) -> str:
    """去括号后缀：男孩(Live) -> 男孩、Love（现场版） -> Love。

    歌单里的 (Live)/(翻唱)/（DJ版）后缀多是"当年找不到正版源"的妥协，
    搜音源时应剥掉找原曲。支持全角括号与方括号。
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


def rank_candidates(
    song: dict, tracks: list[SourceTrack], quality: str = "flac"
) -> list[SourceTrack]:
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
        if quality and not allow_track(t, quality):
            continue
        key = (t.source, _norm(t.title), _norm(t.artist), t.ext, t.duration_s)
        if key in seen:
            continue
        seen.add(key)
        t.score = score_track(t, song.get("name") or "", artist, duration)
        uniq.append(t)
    return sorted(uniq, key=lambda t: (-t.score, t.source))


def group_candidates(song: dict, tracks: list[SourceTrack]) -> list[dict]:
    """按“来源+标题+歌手+专辑+时长”合并版本，音质作为版本内的下拉选项。

    不在这里按音质过滤：前端需要看到每个版本实际可用的音质，再按
    “精确→更高→更低”决定下载哪一个。
    """
    artist = song.get("orig_artist") or "、".join(song.get("artists") or [])
    duration = int(song.get("orig_duration_ms") or song.get("duration_ms") or 0)
    versions: dict[tuple, dict] = {}
    for t in tracks:
        t.score = score_track(t, song.get("name") or "", artist, duration)
        key = (t.source, _norm(t.title), _norm(t.artist), _norm(t.album), int(t.duration_s or 0))
        q = track_quality(t)
        version = versions.get(key)
        if version is None:
            version = {
                "source": t.source, "title": t.title, "artist": t.artist,
                "album": t.album, "duration_s": int(t.duration_s or 0),
                "score": t.score, "qualities": {},
            }
            versions[key] = version
        version["score"] = max(version["score"], t.score)
        old = version["qualities"].get(q)
        # 同版本同音质只留一个；优先大文件，其次带更长下载地址的条目
        if old is None or (t.size_bytes, len(t.download_url)) > (old.size_bytes, len(old.download_url)):
            version["qualities"][q] = t

    out = []
    for version in versions.values():
        by_quality = version.pop("qualities")
        qualities = sorted(by_quality, key=lambda x: -QUALITY_ORDER.get(x, 0))
        out.append({
            **version,
            "available_qualities": qualities,
            "qualities": [
                {
                    "quality": q,
                    "label": quality_label(q),
                    "bitrate": QUALITY_BITRATES.get(q, 0),
                    "track": by_quality[q].as_dict(),
                }
                for q in qualities
            ],
        })
    return sorted(out, key=lambda v: (-v["score"], v["source"], v["title"]))


def _sanitize(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", (s or "").strip()) or "unknown"


def download_track(t: SourceTrack, dest_dir: Path, title: str, artist: str, progress_cb=None) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{_sanitize(artist)} - {_sanitize(title)}.{t.ext}"
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
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
    except Exception:
        # 失败/中断不能留下半截 .part：下次下载会覆盖，缓存清理也应可清干净
        tmp.unlink(missing_ok=True)
        raise
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
    # 其他格式暂不处理（当前只关心 mp3/flac）


def sleep_between(smin: float, smax: float) -> None:
    time.sleep(random.uniform(smin, smax))
