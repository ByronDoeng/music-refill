"""网易云音乐客户端：会话（扫码登录/cookie 复用）与 M1 所需只读接口。

只封装本项目用到的最小接口集，全部是"登录后可看"的数据：
登录二维码、账号信息、用户歌单、歌单详情、歌曲详情（fee/privilege）、云盘列表。
上传接口在 M3 由 uploader 模块实现，不放在这里。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import requests

from . import weapi

log = logging.getLogger(__name__)

BASE = "https://music.163.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# 登录二维码轮询状态码
QR_WAITING, QR_SCANNED, QR_SUCCESS, QR_EXPIRED = 801, 802, 803, 800


class LoginRequired(Exception):
    """需要登录（无 cookie 或已失效）。"""


class Client:
    def __init__(self, cookie_path: Path | None = None):
        self.cookie_path = cookie_path
        self.session = requests.Session()
        self.session.headers["User-Agent"] = UA
        self.session.headers["Referer"] = "https://music.163.com/"
        # 首次访问拿匿名 cookie（__csrf_token 等）
        self.session.get(f"{BASE}/", timeout=15)
        if cookie_path and cookie_path.exists():
            self._load_cookies()

    # ---------- 会话 ----------

    def _load_cookies(self) -> None:
        assert self.cookie_path is not None
        cookies = json.loads(self.cookie_path.read_text(encoding="utf-8"))
        for k, v in cookies.items():
            self.session.cookies.set(k, v, domain="music.163.com")
        log.info("loaded %d cookies from %s", len(cookies), self.cookie_path)

    def save_cookies(self) -> None:
        if not self.cookie_path:
            return
        cookies = {c.name: c.value for c in self.session.cookies if c.domain.endswith("163.com")}
        self.cookie_path.parent.mkdir(parents=True, exist_ok=True)
        self.cookie_path.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("saved %d cookies -> %s", len(cookies), self.cookie_path)

    def is_logged_in(self) -> bool:
        try:
            data = self._weapi("/weapi/w/nuser/account/get", {})
        except Exception:
            return False
        return data.get("code") == 200 and bool(data.get("profile"))

    def account(self) -> dict:
        data = self._weapi("/weapi/w/nuser/account/get", {})
        profile = data.get("profile") or {}
        return {"user_id": profile.get("userId"), "nickname": profile.get("nickname")}

    def login_with_cookie_raw(self, raw: str) -> dict:
        """解析浏览器复制的 Cookie 串（k=v; k2=v2，也容忍换行）并作为登录态。

        这是绕开 8821 扫码风控的社区标准做法：浏览器登录网页版 → F12 复制
        Cookie 请求头 → 注入。要求包含 MUSIC_U（真正的登录凭证）。
        """
        pairs: dict[str, str] = {}
        for part in raw.replace("\n", ";").split(";"):
            part = part.strip()
            if "=" in part:
                k, _, v = part.partition("=")
                pairs[k.strip()] = v.strip()
        if not pairs.get("MUSIC_U"):
            raise ValueError("Cookie 串里没有 MUSIC_U——请按页面提示从 F12 Network 复制完整 Cookie")
        self.session.cookies.clear()  # 丢弃匿名 cookie，避免干扰
        for k, v in pairs.items():
            self.session.cookies.set(k, v, domain="music.163.com")
        self.save_cookies()
        return self.account()

    # ---------- 扫码登录 ----------

    def qr_create(self) -> dict:
        """创建登录二维码，返回 {unikey, qr_data_url}（png base64，前端直接 <img>）。"""
        import qrcode

        data = self._weapi("/weapi/login/qrcode/unikey", {"type": 1})
        unikey = data["unikey"]
        img = qrcode.make(f"https://music.163.com/login?codekey={unikey}")
        return {"unikey": unikey, "qr_data_url": qr_png_data_url(img)}

    def qr_check(self, unikey: str) -> dict:
        """轮询扫码状态：/weapi/login/qrcode/client/login（与创建接口 /unikey 是两个路径）。

        返回码：803=成功(存 cookie) 802=已扫码待确认 801=等待 800=过期。
        """
        data = self._weapi(
            "/weapi/login/qrcode/client/login", {"key": unikey, "type": 1}
        )
        code = data.get("code")
        if code == QR_SUCCESS:
            self.save_cookies()
        return {"code": code, "message": data.get("message", "")}

    # ---------- 歌单 / 歌曲 / 云盘 ----------

    def user_id(self) -> int:
        acc = self.account()
        if not acc.get("user_id"):
            raise LoginRequired("未登录或 cookie 失效")
        return acc["user_id"]

    def user_playlists(self, uid: int) -> list[dict]:
        """用户全部歌单。返回 [{id, name, count, liked}]，liked=是否"我喜欢的音乐"。"""
        out: list[dict] = []
        offset = 0
        while True:
            data = self._weapi("/weapi/user/playlist", {"uid": uid, "offset": offset, "limit": 100})
            playlists = data.get("playlist", [])
            out.extend(
                {
                    "id": p["id"],
                    "name": p["name"],
                    "count": p.get("trackCount", 0),
                    "liked": p.get("specialType", 0) == 5 and p.get("userId") == uid,
                }
                for p in playlists
            )
            if len(playlists) < 100:
                return out
            offset += 100

    def playlist_track_ids(self, playlist_id: int) -> list[int]:
        data = self._weapi("/weapi/v6/playlist/detail", {"id": playlist_id, "n": 1000, "s": 8})
        return [t["id"] for t in data.get("playlist", {}).get("trackIds", [])]

    def song_details(self, song_ids: list[int]) -> list[dict]:
        """批量歌曲详情，含 fee/privilege。每批 200 个 id 防止 URL 过长。"""
        songs: list[dict] = []
        for chunk in _chunked(song_ids, 200):
            data = self._weapi(
                "/weapi/v3/song/detail",
                {"c": json.dumps([{"id": sid} for sid in chunk]), "ids": json.dumps(chunk)},
            )
            songs.extend(data.get("songs", []))
            time.sleep(0.3)
        return songs

    def cloud_items(self) -> list[dict]:
        """云盘全部条目（正确接口是 /weapi/cloud/get，/cloud/list/get 系列 404）。

        返回 [{songId, songName, artist, duration_ms, fileName}]；
        songId=0 表示该文件未匹配到曲库（"新歌"）。
        """
        out: list[dict] = []
        offset, limit = 0, 1000
        while True:
            data = self._weapi("/weapi/cloud/get", {"limit": limit, "offset": offset})
            items = data.get("data") or []
            for it in items:
                ss = it.get("simpleSong") or {}
                out.append(
                    {
                        "songId": it.get("songId") or 0,
                        "songName": it.get("songName") or (ss.get("name") or ""),
                        "artist": it.get("artist") or "",
                        "duration_ms": int(ss.get("dt") or 0),
                        "file_name": it.get("fileName") or "",
                        "bitrate": it.get("bitrate") or 0,
                        "file_size": it.get("fileSize") or 0,
                    }
                )
            total = data.get("count", 0)
            offset += limit
            if offset >= total or not items:
                return out

    def playable_check(self, song_ids: list[int]) -> dict[int, dict]:
        """播放链接终审：/{'id': {'playable': bool, 'trial': bool}}。

        用明文 /api/song/enhance/player/url（ids 须为 JSON 数组串）；
        weapi 版本对该请求返回 400 参数错误（实测）。
        url 为空=不可播；带 freeTrialInfo=仅 30s 试听；有 url=完整可播。
        """
        out: dict[int, dict] = {}
        for chunk in _chunked(song_ids, 50):
            r = self.session.get(
                f"{BASE}/api/song/enhance/player/url",
                params={"ids": json.dumps(chunk), "br": 320000},
                timeout=20,
            )
            r.raise_for_status()
            try:
                data, _ = json.JSONDecoder().raw_decode(r.text)
            except ValueError:
                log.warning("player/url 响应非 JSON: %r", r.text[:80])
                continue
            for item in data.get("data") or []:
                out[item.get("id")] = {
                    "playable": bool(item.get("url")),
                    "trial": bool(item.get("freeTrialInfo")),
                }
            time.sleep(0.3)
        return out

    def search_songs(self, keyword: str, limit: int = 10) -> list[dict]:
        """站内搜索（明文 cloudsearch 接口）：[{id, name, artists, album, fee, duration_ms}]。"""
        r = self.session.get(
            f"{BASE}/api/cloudsearch/pc",
            params={"s": keyword, "type": 1, "limit": limit},
            timeout=20,
        )
        r.raise_for_status()
        data, _ = json.JSONDecoder().raw_decode(r.text)
        out = []
        for s in (data.get("result") or {}).get("songs") or []:
            out.append(
                {
                    "id": s.get("id"),
                    "name": s.get("name") or "",
                    "artists": "、".join(a.get("name") or "" for a in s.get("ar") or []),
                    "album": (s.get("al") or {}).get("name") or "",
                    "fee": s.get("fee", 0),
                    "duration_ms": int(s.get("duration") or s.get("dt") or 0),
                }
            )
        return out

    # ---------- 请求内核 ----------

    def _weapi(self, path: str, payload: dict, timeout: int = 30) -> dict:
        csrf = self.session.cookies.get("__csrf_token", default="")
        payload = {**payload, "csrf_token": csrf, "csrfToken": csrf}
        # api-enhanced 约定：模块里写 /api/...，框架按加密方式改写前缀；
        # weapi 加密体必须发给 /weapi/ 路径，否则网关回 400 参数错误
        if path.startswith("/api/"):
            path = "/weapi/" + path[len("/api/"):]
        resp = self.session.post(f"{BASE}{path}", data=weapi.encrypt(payload), timeout=timeout)
        resp.raise_for_status()
        return resp.json()


def qr_png_data_url(img) -> str:
    import base64
    import io

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _chunked(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]
