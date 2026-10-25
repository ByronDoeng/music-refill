"""Web GUI（M1）：扫码登录 → 歌单勾选 → 缺口清单。

界面规范见 docs/plan.md FR-14：简洁现代，暗色优先，网易云红点缀；
后端只做薄 API，交互全在前端单页里。
"""
from __future__ import annotations

import logging
import random
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..config import Config
from ..core import downloader, gap_finder
from ..netease.client import Client, LoginRequired, QR_EXPIRED, QR_SUCCESS
from ..netease.cloud_upload import upload_cloud_file
from ..sources import get_registry, load_sources
from ..state import State


def _run_match_job(job: dict, songs: list[dict], registry: dict, healthy: set[str], client):
    """后台匹配线程：逐首（各源并发搜索）→ 打分 → 按顺序追加结果。

    打分参照系 = 库内原版：先用站内搜索拿原版条目（原版歌手/时长），
    避免搬运版条目把正版压到最后（用户实测反馈）。
    """
    import concurrent.futures
    import time as _time

    job["total"] = len(songs)
    for idx, song in enumerate(songs):
        # 原版参照：站内搜索同名条目，精确同名优先
        canon = None
        try:
            kw = downloader.strip_brackets(song["name"]) or song["name"]
            res = client.search_songs(kw, limit=5)
            tgt = downloader._norm(song["name"])
            for x in res:
                if downloader._norm(x["name"]) == tgt:
                    canon = x
                    break
            canon = canon or (res[0] if res else None)
        except Exception as e:
            log.warning("原版参照搜索失败 %s: %s", song["name"], e)
        if canon:
            song = {**song, "orig_artist": canon["artists"], "orig_duration_ms": canon["duration_ms"]}

        tracks: list = []

        def do_search(src_name: str):
            keyword = downloader.strip_brackets(song["name"]) or song["name"]
            return registry[src_name].search(keyword, limit=8)

        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(healthy))) as ex:
            futures = {ex.submit(do_search, n): n for n in healthy}
            for fut in concurrent.futures.as_completed(futures):
                try:
                    tracks.extend(fut.result())
                except Exception as e:  # 单源失败不拖垮整首
                    log.warning("音源 %s 匹配 %s 失败: %s", futures[fut], song["name"], e)
        ranked = downloader.rank_candidates(song, tracks)
        job["results"].append(
            {
                "song": song,
                "candidates": [t.as_dict() for t in ranked[:8]],
                "auto": bool(ranked and ranked[0].score >= downloader.AUTO_CONFIRM_THRESHOLD),
            }
        )
        job["done_count"] = idx + 1
        if idx < len(songs) - 1:
            _time.sleep(random.uniform(0.5, 1.5))
    job["running"] = False

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


class ScanReq(BaseModel):
    """注意：必须定义在模块层。__future__ annotations 下函数内定义的类无法被 FastAPI 解析。"""

    playlist_ids: list[int] = []
    include_liked: bool = True


class CookieLoginReq(BaseModel):
    raw: str


class MatchStartReq(BaseModel):
    songs: list[dict]  # 缺口条目（gap item as_dict）


class DownloadReq(BaseModel):
    items: list[dict]  # [{song: gap item, track: SourceTrack.as_dict()}]


def create_app(cfg: Config) -> FastAPI:
    app = FastAPI(title="music-refill", docs_url=None, redoc_url=None)
    cfg.paths.data_dir.mkdir(parents=True, exist_ok=True)
    state = State(cfg.paths.db)
    client_holder: dict[str, Client] = {}
    browser_login_running = {"v": False}
    # 最近一次扫描的缺口（download 的 scan_id 关联用）；重启后需重新扫描
    last_scan: dict = {"scan_id": None, "gap": [], "processed_ids": set()}
    sources_loaded = {"done": False}
    match_job: dict = {"running": False, "total": 0, "done_count": 0, "results": []}
    dl_state: dict = {
        "lock": threading.Lock(),
        "queue": [],
        "running": False,
        "current": None,
        "results": [],
    }

    def ensure_sources() -> dict[str, dict]:
        if not sources_loaded["done"]:
            logging.getLogger("musicdl").setLevel(logging.WARNING)  # musicdl 话痨日志降噪
            sources_loaded["status"] = load_sources()
            sources_loaded["done"] = True
        return sources_loaded["status"]

    def get_client() -> Client:
        if "c" not in client_holder:
            client_holder["c"] = Client(cfg.paths.cookies)
        return client_holder["c"]

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/status")
    def status():
        c = get_client()
        try:
            acc = c.account()
        except Exception:
            acc = {}
        logged = bool(acc.get("user_id"))
        return {"logged_in": logged, "user_id": acc.get("user_id"), "nickname": acc.get("nickname")}

    @app.post("/api/login/qr/create")
    def login_qr_create():
        c = get_client()
        try:
            return c.qr_create()
        except Exception as e:
            log.exception("qr create failed")
            raise HTTPException(502, f"创建二维码失败：{e}") from e

    @app.get("/api/login/qr/check")
    def login_qr_check(unikey: str):
        c = get_client()
        try:
            result = c.qr_check(unikey)
        except Exception as e:
            log.exception("qr check failed")
            raise HTTPException(502, f"登录状态查询失败：{e}") from e
        if result["code"] != 801:  # 等待态不刷日志
            log.info("qr poll code=%s", result["code"])
        if result["code"] == QR_SUCCESS:
            result["account"] = c.account()
        return result

    @app.post("/api/login/cookie")
    def login_cookie(req: CookieLoginReq):
        """手动 Cookie 登录：绕开 8821 扫码风控的社区标准路径。"""
        c = get_client()
        try:
            acc = c.login_with_cookie_raw(req.raw)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except Exception as e:
            log.exception("cookie login failed")
            raise HTTPException(502, f"登录失败：{e}") from e
        if not acc.get("user_id"):
            raise HTTPException(400, "Cookie 无效（网易未认可）——请重新复制完整 Cookie 串")
        return {"ok": True, **acc}

    @app.post("/api/logout")
    def logout():
        """退出登录：清空会话与本地 cookie。"""
        c = get_client()
        c.session.cookies.clear()
        if cfg.paths.cookies.exists():
            cfg.paths.cookies.unlink()
        client_holder.pop("c", None)  # 下次请求重建匿名会话
        return {"ok": True}

    @app.post("/api/login/browser")
    def login_browser():
        """一键登录：弹出官方 Chromium 窗口，用户在官方页面内登录后自动抓 cookie。"""
        if browser_login_running["v"]:
            raise HTTPException(409, "已有一个浏览器登录窗口在进行中")
        browser_login_running["v"] = True
        try:
            from ..netease.browser_login import login_via_browser

            result = login_via_browser(cfg.paths.cookies)
        except Exception as e:
            log.exception("browser login failed")
            raise HTTPException(500, f"浏览器登录失败：{e}") from e
        finally:
            browser_login_running["v"] = False
        if not result.get("ok"):
            raise HTTPException(408, result.get("reason", "登录未完成"))
        client_holder.pop("c", None)  # 丢弃旧会话，重新加载新 cookie
        c = get_client()
        return {"ok": True, **c.account()}

    @app.get("/api/playlists")
    def playlists():
        c = get_client()
        try:
            uid = c.user_id()
        except Exception:
            raise HTTPException(401, "未登录或 cookie 失效，请重新扫码")
        pls = c.user_playlists(uid)
        return {
            "default_include_liked": cfg.scan.include_liked,
            "fixed_playlist_ids": cfg.scan.playlist_ids,
            "playlists": pls,
        }

    @app.post("/api/scan")
    def scan(req: ScanReq):
        c = get_client()
        try:
            result = gap_finder.scan(c, req.playlist_ids, req.include_liked)
        except LoginRequired as e:
            raise HTTPException(401, str(e)) from e
        except Exception as e:
            log.exception("scan failed")
            raise HTTPException(500, f"扫描失败：{e}") from e
        scan_id = state.save_scan(
            {"playlist_ids": req.playlist_ids, "include_liked": req.include_liked},
            result.total_tracks, [g.as_dict() for g in result.gap],
        )
        report = cfg.paths.reports / f"gap-{time.strftime('%Y%m%d-%H%M%S')}.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(gap_finder.to_markdown(result), encoding="utf-8")
        last_scan["scan_id"] = scan_id
        last_scan["gap"] = [g.as_dict() for g in result.gap]
        last_scan["processed_ids"] = set()
        return {
            "scan_id": scan_id,
            "summary": result.summary(),
            "gap": [g.as_dict() for g in result.gap],
            "report_file": report.name,
        }

    @app.get("/api/sources")
    def sources():
        return ensure_sources()

    @app.post("/api/match/start")
    def match_start(req: MatchStartReq):
        """启动后台匹配任务（FR-15：匹配结果必须经人工确认）。"""
        ensure_sources()
        registry = get_registry()  # 适配器实例表；ensure_sources() 只负责加载并返回健康状态
        healthy = {n for n, s in registry.items() if sources_loaded["status"][n]["healthy"]}
        if not healthy:
            raise HTTPException(400, "没有可用音源（sources_local/ 为空或全部探活失败）")
        if not (1 <= len(req.songs) <= 500):
            raise HTTPException(400, "请选择 1~500 首歌")
        if match_job["running"]:
            raise HTTPException(409, "已有匹配任务在进行中")
        match_job["running"] = True
        match_job["total"] = len(req.songs)
        match_job["done_count"] = 0
        match_job["results"] = []
        import threading

        threading.Thread(
            target=_run_match_job,
            args=(match_job, req.songs, registry, healthy, get_client()),
            daemon=True,
        ).start()
        return {"ok": True, "total": len(req.songs)}

    @app.get("/api/match/results")
    def match_results(offset: int = 0, limit: int = 10):
        """前端分批拉取已完成的结果（默认每次 10 条）。"""
        limit = max(1, min(limit, 50))
        results = match_job["results"][offset : offset + limit]
        return {
            "results": results,
            "offset": offset + len(results),
            "running": match_job["running"],
            "done": match_job["done_count"],
            "total": match_job["total"],
        }

    @app.post("/api/download")
    def download(req: DownloadReq):
        """把人工确认的批次加入后台队列：下载 → 打标 → 上传云盘，立即返回。"""
        if not (1 <= len(req.items) <= 50):
            raise HTTPException(400, "单批 1~50 首")
        with dl_state["lock"]:
            if len(dl_state["queue"]) + len(req.items) > 300:
                raise HTTPException(400, "后台队列积压过多（>300），请先等当前批次完成")
            dl_state["queue"].extend(req.items)
            queue_len = len(dl_state["queue"])
            need_start = not dl_state["running"]
            if need_start:
                dl_state["running"] = True
        if need_start:
            import threading

            threading.Thread(target=_dl_worker, daemon=True).start()
        return {"queued": len(req.items), "queue_len": queue_len}

    @app.get("/api/download/progress")
    def download_progress():
        with dl_state["lock"]:
            results = dl_state["results"][-8:]
            ok = sum(1 for r in dl_state["results"] if r["status"] == "ok")
            snap = {
                "running": dl_state["running"],
                "current": dict(dl_state["current"]) if dl_state["current"] else None,
                "queue_len": len(dl_state["queue"]),
                "done": len(dl_state["results"]),
                "ok": ok,
                "failed": len(dl_state["results"]) - ok,
                "results": results,
            }
        return snap

    @app.post("/api/downloads/clear")
    def downloads_clear():
        """清除本地下载缓存（data/downloads 下的全部文件）。"""
        d = cfg.paths.data_dir / "downloads"
        removed, freed = 0, 0
        if d.exists():
            for f in d.iterdir():
                if f.is_file():
                    freed += f.stat().st_size
                    f.unlink()
                    removed += 1
        return {"removed": removed, "freed_mb": round(freed / 1048576, 1)}

    def _dl_worker():
        """后台流水线：下载 → 打标 → 上传，逐首限速；队列为空即退出。"""
        worker_client = Client(cfg.paths.cookies)  # 独立会话，避免与请求线程共享
        dest_dir = cfg.paths.data_dir / "downloads"
        while True:
            with dl_state["lock"]:
                if not dl_state["queue"]:
                    dl_state["running"] = False
                    dl_state["current"] = None
                    return
                item = dl_state["queue"].pop(0)
            song, t = item["song"], item["track"]
            stage = "下载"
            try:
                track = downloader.SourceTrack(**t)

                def pcb(recv, total, _song=song):
                    with dl_state["lock"]:
                        dl_state["current"] = {
                            "name": _song["name"], "stage": "下载", "recv": recv, "total": total,
                        }

                path = downloader.download_track(
                    track, dest_dir, song["name"], "、".join(song["artists"]), progress_cb=pcb,
                )
                if track.size_bytes and abs(path.stat().st_size - track.size_bytes) / track.size_bytes > 0.02:
                    # 完整性校验：下载被截断的文件会导致 info/v2 uploadStatus=6
                    raise RuntimeError(f"下载文件大小异常（{path.stat().st_size} vs 预期 {track.size_bytes}），已放弃")
                stage = "打标"
                with dl_state["lock"]:
                    dl_state["current"] = {"name": song["name"], "stage": "打标", "recv": None, "total": None}
                downloader.tag_file(
                    path, song["name"], "、".join(song["artists"]),
                    song.get("album") or track.album, track.cover_url, track.lyric,
                )
                stage = "上传云盘"
                with dl_state["lock"]:
                    dl_state["current"] = {
                        "name": song["name"], "stage": "上传云盘", "recv": None, "total": track.size_bytes,
                    }
                up = upload_cloud_file(
                    worker_client, path, song["name"], "、".join(song["artists"]),
                    song.get("album") or track.album,
                )
                res = {
                    "song_id": song["id"], "name": song["name"], "status": "ok", "file": path.name,
                    "quality": f"{track.ext}/{track.size_bytes // 1024}KB",
                    "cloud_song_id": up["songId"], "matched_library": up["matched_library"],
                    "instant": not up["need_upload"],
                }
                if last_scan["scan_id"]:
                    state.save_upload(last_scan["scan_id"], song["id"], "ok", route="download",
                                      quality=track.ext,
                                      detail=f"cloud:{up['songId']} matched={up['matched_library']}")
            except Exception as e:
                log.exception("download/upload failed at [%s]: %s", stage, song["name"])
                res = {"song_id": song["id"], "name": song["name"], "status": "failed",
                       "detail": f"[{stage}] {str(e)[:130]}"}
                if last_scan["scan_id"]:
                    state.save_upload(last_scan["scan_id"], song["id"], "failed",
                                      route="download", detail=res["detail"])
            with dl_state["lock"]:
                dl_state["results"].append(res)
                dl_state["current"] = None
            downloader.sleep_between(2, 5)

    @app.get("/api/report/{filename}")
    def report(filename: str):
        # 只允许取 reports/ 目录下的 .md 文件，防路径穿越
        if "/" in filename or "\\" in filename or not filename.endswith(".md"):
            raise HTTPException(400, "非法文件名")
        path = cfg.paths.reports / filename
        if not path.exists():
            raise HTTPException(404, "报告不存在")
        return FileResponse(path, media_type="text/markdown", filename=filename)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
