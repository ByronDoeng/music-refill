"""缺口判定：歌单曲目 × 云盘比对 → 需补歌曲清单。

判定规则：
  fee==1(VIP) / fee==4(需购买专辑) / privilege.st<0(下架或无版权) / privilege.pl==0(无播放权限)
  且 songId 不在云盘 → 记为缺口。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..netease.client import Client, LoginRequired

log = logging.getLogger(__name__)


@dataclass
class GapItem:
    id: int
    name: str
    artists: list[str]
    album: str
    duration_ms: int
    fee: int
    reason: str
    playlists: list[str] = field(default_factory=list)
    in_cloud: bool = False  # True=云盘疑似已有（未匹配文件），默认不选
    cloud_info: dict | None = None  # 疑似命中的云盘条目（供人工判断）

    def as_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "artists": self.artists,
            "album": self.album, "duration_ms": self.duration_ms,
            "fee": self.fee, "reason": self.reason, "playlists": self.playlists,
            "in_cloud": self.in_cloud, "cloud_info": self.cloud_info,
        }


@dataclass
class ScanResult:
    playlist_ids: list[int]
    total_tracks: int
    cloud_exact: int
    cloud_fuzzy: int
    gap: list[GapItem]

    def summary(self) -> dict:
        return {
            "playlists": self.playlist_ids,
            "total": self.total_tracks,
            "cloud_exact": self.cloud_exact,
            "cloud_fuzzy": self.cloud_fuzzy,
            "gap": len(self.gap),
        }


def classify(song: dict) -> str | None:
    """返回"当前账号不可播放"的原因；能正常听则返回 None。

    关键：fee 是歌曲的收费类型标签（静态），privilege 才是账号实时权限——
    fee=1 的歌可能限免可播（pl>0），这类不算缺口（用户可免费播放，无需补）。
    """
    priv = song.get("privilege") or {}
    if priv.get("st", 0) < 0:
        return "已下架/无版权"
    if priv.get("pl", 0) == 0:  # 当前账号无播放权限
        fee = song.get("fee", 0)
        if fee == 1:
            return "VIP"
        if fee == 4:
            return "需购买专辑"
        return "无播放权限"
    return None


def _norm(s: str) -> str:
    """标题归一化：剥括号+小写去空白。云盘模糊判重用（搬运版歌名常带 (Live) 等后缀）。"""
    from .downloader import strip_brackets

    return "".join(strip_brackets(s or "").lower().split())


class _CloudIndex:
    """云盘判重索引：songId 精确 / 标题模糊（剥括号归一化相等）。

    模糊档不要求时长一致：歌单条目常是搬运/Live 版（时长与云盘里的原版天生不同），
    时长硬校验会漏掉大量"其实云盘已有"。命中只按标题，是否同一首由人工在
    清单里对照"云盘疑似文件"列判断（该列显示双方歌手/时长/文件信息）。
    """

    def __init__(self, items: list[dict]):
        self.items = items
        self.ids = {i["songId"]: i for i in items if i["songId"]}
        self.by_name: dict[str, dict] = {}
        for i in items:
            if i["songName"]:
                self.by_name.setdefault(_norm(i["songName"]), i)

    def hit(self, song_id: int, name: str, duration_ms: int) -> tuple[str | None, dict | None]:
        if song_id in self.ids:
            return "exact", self.ids[song_id]
        item = self.by_name.get(_norm(name))
        if item:
            return "fuzzy", item
        return None, None


def scan(client: Client, playlist_ids: list[int], include_liked: bool = True) -> ScanResult:
    uid = client.user_id()
    playlists = client.user_playlists(uid)

    wanted: dict[int, str] = {}
    if include_liked:
        liked = next((p for p in playlists if p["liked"]), None)
        if liked:
            wanted[liked["id"]] = f"❤ {liked['name']}"
        else:
            raise LoginRequired("找不到「我喜欢的音乐」歌单")
    for pid in playlist_ids:
        meta = next((p for p in playlists if p["id"] == pid), None)
        wanted[pid] = meta["name"] if meta else f"歌单 {pid}"

    # 汇总曲目，记录每首歌出现在哪些歌单
    track_playlists: dict[int, list[str]] = {}
    for pid, pname in wanted.items():
        for tid in client.playlist_track_ids(pid):
            track_playlists.setdefault(tid, []).append(pname)
    all_ids = list(track_playlists)

    songs = client.song_details(all_ids)
    cloud = _CloudIndex(client.cloud_items())

    # 播放链接终审对全部歌曲生效（privilege/fee 都只是参考标签，URL 才是金标准）：
    # 有完整可播 URL → 不是缺口；仅试听 → 缺口；无 URL → 按 classify 给原因
    play = client.playable_check(all_ids) if all_ids else {}

    gap: list[GapItem] = []
    cloud_exact = cloud_fuzzy = 0
    for s in songs:
        p = play.get(s["id"])
        if p:
            if p["playable"] and not p["trial"]:
                continue  # 当前账号可完整播放
            reason = classify(s) or "不可播放"
            if p["trial"]:
                reason += "（仅试听）"
        else:
            reason = classify(s)
            if not reason:
                continue
        hit_type, citem = cloud.hit(s["id"], s.get("name") or "", int(s.get("dt") or 0))
        if hit_type == "exact":
            cloud_exact += 1
            continue
        if hit_type == "fuzzy":
            # 云盘已有同内容文件（未匹配曲库）：保留在清单并打标，默认不选
            cloud_fuzzy += 1
            gap.append(
                GapItem(
                    id=s["id"],
                    name=s.get("name") or "?",
                    artists=[a.get("name") or "" for a in s.get("ar") or []],
                    album=(s.get("al") or {}).get("name") or "",
                    duration_ms=int(s.get("dt") or 0),
                    fee=int(s.get("fee") or 0),
                    reason=reason,
                    playlists=track_playlists.get(s["id"], []),
                    in_cloud=True,
                    cloud_info=citem,
                )
            )
            continue
        # 曲库偶发字段为 null（空歌手名等），全部做 None 防御
        gap.append(
            GapItem(
                id=s["id"],
                name=s.get("name") or "?",
                artists=[a.get("name") or "" for a in s.get("ar") or []],
                album=(s.get("al") or {}).get("name") or "",
                duration_ms=int(s.get("dt") or 0),
                fee=int(s.get("fee") or 0),
                reason=reason,
                playlists=track_playlists.get(s["id"], []),
            )
        )
    # 云盘疑似已有的排最前，方便集中人工判断（组内保持曲库顺序）
    gap.sort(key=lambda g: 0 if g.in_cloud else 1)
    return ScanResult(
        playlist_ids=list(wanted.keys()),
        total_tracks=len(all_ids),
        cloud_exact=cloud_exact,
        cloud_fuzzy=cloud_fuzzy,
        gap=gap,
    )


def to_markdown(result: ScanResult) -> str:
    lines = [
        "# music-refill 缺口清单",
        "",
        f"- 歌单：{result.playlist_ids}",
        f"- 曲目总数：{result.total_tracks} ｜ 云盘已有（精确）：{result.cloud_exact} ｜ "
        f"云盘疑似已有（标题+时长匹配，未匹配文件）：{result.cloud_fuzzy} ｜ **需补：{len(result.gap)}**",
        "",
        "| # | 歌曲 | 歌手 | 专辑 | 时长 | 原因 | 云盘疑似文件 | 来源歌单 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    import time as _t

    for i, g in enumerate(result.gap, 1):
        mm, ss = divmod(g.duration_ms // 1000, 60)
        reason = f"{g.reason}（云盘疑似已有）" if g.in_cloud else g.reason
        ci = ""
        if g.cloud_info:
            ci = f"{g.cloud_info.get('songName', '')} / {g.cloud_info.get('artist', '')}（{g.cloud_info.get('file_name', '')}）"
        lines.append(
            f"| {i} | {g.name} | {'、'.join(g.artists)} | {g.album} | {mm}:{ss:02d} | {reason} | {ci} | {'、'.join(g.playlists)} |"
        )
    lines += ["", f"_生成时间：{_t.strftime('%Y-%m-%d %H:%M:%S')}_"]
    return "\n".join(lines)
