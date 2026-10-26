"""上传后校验：确认文件真的进了云盘，并可被当前账号播放。

为什么需要：
`pub/v2` 只是“提交发布”，服务端建立索引是异步的；历史上出现过
info/v2 返回成功、但云盘列表里暂时查不到的情况。没有校验就无法回答
“这批歌到底能不能听”，也无法验证计划里的 ≥85% 可播验收。
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)


def verify_upload(
    client,
    cloud_song_id: int,
    md5: str = "",
    file_name: str = "",
    attempts: int = 3,
    interval_s: float = 4.0,
) -> dict:
    """返回 {verified, playable, cloud_song_id, detail}。

    - verified：云盘列表里能按 songId / md5 / 文件名找到；
    - playable：若已匹配曲库（cloud_song_id 是正常曲库 ID），再查播放 URL；
      未匹配的私有文件 songId 通常是长数字/0，播放 URL 接口不可靠，标记为 unknown。
    """
    found = None
    for i in range(max(1, attempts)):
        try:
            found = client.cloud_find(
                song_id=cloud_song_id, md5=md5, file_name=file_name
            )
        except Exception as e:
            log.warning("云盘校验查询失败（第 %s 次）：%s", i + 1, e)
        if found:
            break
        if i < attempts - 1:
            time.sleep(interval_s)

    if not found:
        return {
            "verified": False,
            "playable": None,
            "cloud_song_id": cloud_song_id,
            "detail": "云盘列表未查到（可能索引延迟，可稍后手动刷新）",
        }

    sid = int(found.get("songId") or cloud_song_id or 0)
    playable = None
    # 只有“已匹配曲库”的 songId 才能查播放 URL；私有资源 id（长哈希/0）
    # 调 player/url 不可靠，不能据此宣称不可播。
    # 云盘列表里的 songId 与曲库条目同量级；超长 id 一律视为私有资源。
    matched_library = 0 < sid < 10**12
    if sid > 0 and matched_library:
        try:
            info = client.playable_check([sid]).get(sid) or {}
            if info:
                playable = bool(info.get("playable")) and not bool(info.get("trial"))
        except Exception as e:
            log.warning("播放校验失败 songId=%s：%s", sid, e)

    return {
        "verified": True,
        "playable": playable,
        "cloud_song_id": sid,
        "file_name": found.get("file_name") or file_name,
        "detail": "已入云盘" + ("" if playable is not False else "，但当前不可完整播放"),
    }
