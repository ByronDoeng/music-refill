"""Web GUI 与本地 JSON API。

界面规范：简洁现代，暗色优先，网易云红点缀；
后端只做薄 API，交互全在前端单页里。

接口清单与错误码见 docs/API.md。所有写操作（下载/上传/清缓存/退出）
都必须由用户在页面上显式触发；服务只监听 127.0.0.1。
"""
from __future__ import annotations

import logging
import random
import threading
import time
import concurrent.futures
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..config import Config
from ..core import downloader, gap_finder
from ..netease.client import Client, LoginRequired, QR_EXPIRED, QR_SUCCESS
from ..netease.cloud_upload import upload_cloud_file
from ..services.retry import RetryPolicy, is_retryable
from ..services.verify import verify_upload
from ..sources import get_registry, load_sources
from ..sources.base import external_song_id
from ..state import State


def _run_match_job(
    job: dict, songs: list[dict], registry: dict, healthy: list[str],
    client, sleep_min: float = 0.5, sleep_max: float = 1.5, quality: str = "flac",
    timeout_s: float = 30.0,
):
    """后台匹配线程：逐首（各源并发搜索）→ 打分 → 按顺序追加结果。

    不查网易站内参照；直接用歌单条目的标题/歌手/时长给音源候选打分。
    """
    import time as _time

    job["total"] = len(songs)
    try:
        for idx, song in enumerate(songs):
            job["current"] = {
                "name": song.get("name") or "",
                "artist": "、".join(song.get("artists") or []),
                "index": idx + 1,
                "started": _time.time(),
                "phase": "搜索音源",
            }
            # 匹配阶段不按音质过滤：前端需要看到每个版本实际可用的音质，
            # 再按“精确→更高→更低”选择；过滤留到确认/入队时。
            # 超时仍保留已返回候选；上限可配置，避免酷我慢查询在结果到达前被取消。
            song, ranked = _match_song(
                song, registry, healthy, client, "", timeout_s=timeout_s,
            )
            if not ranked:
                job.setdefault("errors", []).append(
                    {"name": song.get("name") or "", "artist": "、".join(song.get("artists") or []),
                     "detail": "未找到可用候选（可能音源较慢或该版本无下载地址）"}
                )
            versions = downloader.group_candidates(song, ranked)
            job["results"].append(
                {
                    "song": song,
                    "candidates": [t.as_dict() for t in ranked[:8]],
                    "versions": versions,
                    "auto": bool(versions and versions[0]["score"] >= downloader.AUTO_CONFIRM_THRESHOLD),
                }
            )
            job["done_count"] = idx + 1
            job["current"] = {}
            if idx < len(songs) - 1:
                _time.sleep(random.uniform(sleep_min, sleep_max))
    except Exception:
        log.exception("match job crashed")
    finally:
        # 无论成功/异常都释放“匹配中”状态，否则前端永远拿不到新批次
        job["current"] = {}
        job["running"] = False


def _healthy_source_names(registry: dict, status: dict[str, dict]) -> list[str]:
    """按配置优先级返回健康音源名；status 来自 load_sources()。"""
    return [
        n for n, _ in sorted(
            registry.items(),
            key=lambda kv: (status.get(kv[0], {}).get("priority", 10**6), kv[0]),
        )
        if status.get(n, {}).get("healthy")
    ]


def _split_artist_names(artist: str) -> list[str]:
    """把“A、B / C & D”拆成歌手名，优先用共同主歌手补一次查询。"""
    import re

    names = [x.strip() for x in re.split(r"[、,，/&;；]+", artist or "") if x.strip()]
    return list(dict.fromkeys(names))


def _source_queries(title: str, artist: str = "", raw: str = "") -> list[str]:
    """生成音源搜索词：标题、标题+歌手、标题+主歌手。

    中文歌名里常见全角空格、零宽字符、括号英文副标题；先清理，再分别
    用标题和标题+歌手搜索。去重后最多 3 个，避免把慢源拖成十几组请求。
    """
    import re

    def clean(value: str) -> str:
        value = (value or "").replace("\u200b", "").replace("\ufeff", "")
        return re.sub(r"\s+", " ", value).strip()

    full = clean(raw or title)
    base = clean(downloader.strip_brackets(title) or title)
    if not base:
        base = clean(downloader.strip_brackets(full) or full)
    queries = [base] if base else []
    artists = _split_artist_names(artist)
    if artists:
        joined = "、".join(artists)
        if joined not in base:
            queries.append(f"{base} {joined}")
        if len(artists) > 1 and artists[0] not in base:
            queries.append(f"{base} {artists[0]}")
    elif full != base:
        queries.append(full)

    # “歌名 - 歌手”或“歌名/别名”这类输入：额外取第一段作为标题查询。
    pieces = [clean(p) for p in re.split(r"\s+[-–—/|]\s+", full) if clean(p)]
    if len(pieces) > 1:
        queries.append(pieces[0])

    return list(dict.fromkeys(q for q in queries if q))[:3]


def _collect_source_tracks(
    registry: dict, source_names: list[str], queries: list[str], limit: int,
    timeout_s: float, stop_title: str = "", first_batch_s: float | None = None,
    pending_out: list | None = None,
) -> tuple[list, list[str]]:
    """音源之间并行；同一音源内按查询顺序串行，命中精确标题即停止。

    原因是 musicdl 客户端会在实例上保存会话/进度状态，同一实例并发 search
    线程安全性未验证；旧实现还把多个查询同时压给慢源，导致 30 秒仍不齐。
    这里每源一个线程，标题命中就跳过“标题+歌手”，其它源不受影响。
    first_batch_s 用于渐进返回：到点先返回已完成音源，慢源任务追加到
    pending_out 由调用方稍后收集；否则等到 timeout_s，并取消未完成任务。
    """
    if not source_names or not queries:
        return [], []

    target = downloader._norm(downloader.strip_brackets(stop_title or "") or stop_title)

    def search_one_source(src_name: str):
        started_at = time.monotonic()
        source_tracks: list = []
        source_errors: list[str] = []
        adapter = registry[src_name]
        for query in queries:
            try:
                # 优先用音源的快速元信息接口；未实现 quick_search 的源退回 search()。
                quick = getattr(adapter, "quick_search", None)
                found = quick(query, limit=limit) if callable(quick) else adapter.search(query, limit=limit)
            except Exception as e:
                log.warning("音源 %s 搜索 %s 失败: %s", src_name, query, e)
                source_errors.append(f"{src_name}（{query}）：{str(e)[:120]}")
                continue
            source_tracks.extend(found)
            # 第一组查询就是标题本身；只要候选里出现同名版本，就没必要
            # 再用“标题+歌手”压慢源。目标标题被剥括号后比较，避免 Live 误判。
            if target and any(
                downloader._norm(downloader.strip_brackets(t.title) or t.title) == target
                for t in found
            ):
                break
        # 快搜只返回元信息；下载链接在入队时按需解析。解析失败就丢弃该候选，
        # 避免前端出现“看起来可选、点入队必失败”的条目。
        if source_tracks and not any(t.resolved for t in source_tracks):
            resolve_many = getattr(adapter, "resolve_many", None)
            if callable(resolve_many):
                source_tracks = resolve_many(source_tracks)
            else:
                resolved = []
                for track in source_tracks:
                    try:
                        done = adapter.resolve(track) if callable(getattr(adapter, "resolve", None)) else track
                    except Exception:
                        done = None
                    if done:
                        resolved.append(done)
                source_tracks = resolved
        return source_tracks, source_errors, started_at

    tracks: list = []
    errors: list[str] = []
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=len(source_names))
    futures = {executor.submit(search_one_source, name): name for name in source_names}
    pending = set(futures)
    deadline = time.monotonic() + max(1.0, float(timeout_s))
    if first_batch_s is not None:
        deadline = min(deadline, time.monotonic() + max(0.1, float(first_batch_s)))
    while pending:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        done, pending = concurrent.futures.wait(
            pending, timeout=remaining, return_when=concurrent.futures.FIRST_COMPLETED,
        )
        if not done:
            break
        for fut in done:
            name = futures[fut]
            try:
                source_tracks, source_errors, _ = fut.result()
                tracks.extend(source_tracks)
                errors.extend(source_errors)
            except Exception as e:
                log.warning("音源 %s 搜索线程失败: %s", name, e)
                errors.append(f"{name}：{str(e)[:120]}")
    if pending_out is not None and first_batch_s is not None and not tracks:
        # 首批没有结果时再给一个短窗口，避免“列表接口稍慢”直接呈现空页。
        # 该窗口远小于 source_search_timeout_s，慢源仍在后台继续。
        extra_deadline = time.monotonic() + 2.0
        while pending and time.monotonic() < extra_deadline:
            done, pending = concurrent.futures.wait(
                pending, timeout=extra_deadline - time.monotonic(),
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for fut in done:
                try:
                    source_tracks, source_errors, _ = fut.result()
                    tracks.extend(source_tracks)
                    errors.extend(source_errors)
                except Exception as e:
                    errors.append(f"{futures[fut]}：{str(e)[:120]}")
            if tracks:
                break
    if pending_out is not None and first_batch_s is not None:
        # 交还未完成的 future：慢源继续跑，调用方稍后合并结果。
        # 这里不能 shutdown(wait=True)：ThreadPoolExecutor 的两参 shutdown 会等
        # 工作线程结束，等于把“先返回”抵消成等最慢源。
        # 先收割已经完成的，避免 wait() 超时时把完成结果误判成 pending。
        for fut in list(pending):
            if not fut.done():
                continue
            try:
                source_tracks, source_errors, _ = fut.result()
                tracks.extend(source_tracks)
                errors.extend(source_errors)
            except Exception as e:
                log.warning("音源 %s 搜索线程失败: %s", futures[fut], e)
                errors.append(f"{futures[fut]}：{str(e)[:120]}")
            pending.discard(fut)
        pending_out.append({"executor": executor, "jobs": [
            {"future": fut, "name": futures[fut]} for fut in pending
        ]})
        return tracks, errors
    for fut in pending:
        fut.cancel()
    executor.shutdown(wait=False, cancel_futures=True)
    if pending:
        names = sorted({futures[f] for f in pending})
        errors.append(f"部分音源超时：{'、'.join(names)}（已返回其余结果）")
        log.warning("音源搜索超时：%s（先返回已完成候选）", "、".join(names))
    return tracks, errors


def _match_song(
    song: dict, registry: dict, healthy: list[str], client, quality: str,
    limit: int = 8, timeout_s: float = 45,
) -> tuple[dict, list]:
    """单首匹配：各源并发搜“标题”和“标题+歌手” → 打分排序。

    不再查网易站内参照：那会为每首歌多一次串行网络请求，是匹配页长时间
    无响应的主要原因。打分直接用歌单条目自身的标题/歌手/时长。
    """
    raw = (song.get("name") or "").strip()
    title = downloader.strip_brackets(raw) or raw
    artist = "、".join(song.get("artists") or [])
    queries = _source_queries(title or raw, artist, raw)
    tracks, _ = _collect_source_tracks(
        registry, healthy, queries, limit, timeout_s, stop_title=title or raw,
    )
    ranked = downloader.rank_candidates(song, tracks, quality=quality)
    return song, ranked


def _run_quick_add_job(job: dict, songs: list[dict], registry: dict, healthy: list[str],
                       client, quality: str, threshold: float,
                       sleep_min: float, sleep_max: float, enqueue_cb):
    """后台快速补歌：逐首打分，高分交给 enqueue_cb 入队，低分留给人工确认。

    放在线程里是因为多个音源搜索对 10~50 首会持续几十秒到数分钟，
    同步 HTTP 会让浏览器/反代超时，也无法显示进度。
    """
    job["total"] = len(songs)
    try:
        for idx, song in enumerate(songs):
            try:
                song, ranked = _match_song(song, registry, healthy, client, quality)
                if ranked and ranked[0].score >= threshold:
                    item = {"song": song, "track": ranked[0].as_dict(), "scan_id": 0}
                    job_ids = enqueue_cb([item])
                    item["job_id"] = job_ids[0]
                    job["queued"].append(item)
                elif ranked:
                    job["needs_review"].append({
                        "song": song,
                        "candidates": [t.as_dict() for t in ranked[:8]],
                        "reason": f"最高分 {ranked[0].score} < 阈值 {threshold}",
                    })
                else:
                    job["skipped"].append({"song": song, "reason": "未找到候选"})
            except Exception as e:
                log.exception("quick-add item failed: %s", song.get("name"))
                job["errors"].append({"song": song, "reason": str(e)[:160]})
            job["done_count"] = idx + 1
            if idx < len(songs) - 1:
                time.sleep(random.uniform(sleep_min, sleep_max))
    finally:
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
    origin: str = "gap"  # gap | search：决定任务是否关联最近一次扫描
    quality: str | None = None  # 仅作为前端默认提示；候选不在此阶段过滤


class DownloadReq(BaseModel):
    items: list[dict]  # [{song: gap item, track: SourceTrack.as_dict()}]
    origin: str = "gap"  # gap | search


class SearchReq(BaseModel):
    keyword: str
    limit: int = 20


class SourceSearchReq(BaseModel):
    keyword: str
    limit: int = 20  # 每个音源最多取多少条原始候选


class SourceSearchResultReq(BaseModel):
    """拉取某个搜索会话尚未返回的音源结果。"""

    token: str


class QuickAddReq(BaseModel):
    """按歌名快速补歌：站内搜索选中后，自动取最高分音源候选入队。"""

    songs: list[dict]
    min_score: float | None = None  # 不传则用 config 的 quick_add_min_score


def _choose_canon(song: dict, rows: list[dict]) -> dict | None:
    """从站内搜索结果挑“原版参照”：精确同名优先，否则取第一条。"""
    target = downloader._norm(song.get("name") or "")
    for row in rows:
        if downloader._norm(row.get("name") or "") == target:
            return row
    return rows[0] if rows else None


def _versions_payload(keyword: str, tracks: list) -> list[dict]:
    """把音源候选整理成前端确认单位；匹配与搜索补歌共用同一形状。"""
    parts = keyword.split(maxsplit=1)
    head, tail = parts[0], (parts[1] if len(parts) > 1 else "")
    song = {"id": 0, "name": head, "artists": [tail] if tail else [], "album": "", "duration_ms": 0}
    versions = downloader.group_candidates(song, tracks)
    for v in versions:
        v["canonical"] = None
        v["suggested_song"] = {
            "id": external_song_id(v["title"], v["artist"], v["album"]),
            "name": v["title"],
            "artists": [x for x in str(v["artist"] or "").split("、") if x],
            "album": v["album"] or "",
            "duration_ms": int(v["duration_s"] or 0) * 1000,
        }
    return versions


def create_app(cfg: Config) -> FastAPI:
    app = FastAPI(title="music-refill", docs_url=None, redoc_url=None)
    cfg.paths.data_dir.mkdir(parents=True, exist_ok=True)
    state = State(cfg.paths.db)
    client_holder: dict[str, Client] = {}
    browser_login_running = {"v": False}
    # 最近一次扫描的缺口（download 的 scan_id 关联用）；重启后需重新扫描
    last_scan: dict = {"scan_id": None, "gap": []}
    sources_loaded = {"done": False}
    match_job: dict = {
        "running": False, "total": 0, "done_count": 0, "results": [], "origin": "gap",
        "errors": [],
        "current": {},
    }
    quick_job: dict = {
        "running": False, "total": 0, "done_count": 0, "threshold": None,
        "queued": [], "needs_review": [], "skipped": [], "errors": [],
    }
    dl_state: dict = {
        "lock": threading.Lock(),
        "queue": [],
        "running": False,
        "current": None,
        "results": [],
    }
    # 搜索补歌的渐进结果：token -> {keyword, tracks, versions, pending, errors, done}
    # 只保留最近 8 个会话，避免长期占内存；服务重启即清空（搜索本身是只读操作）。
    search_sessions: dict[str, dict] = {}
    # 服务重启后续跑未完成任务：running → queued
    try:
        recovered = state.reset_running_jobs()
        if recovered:
            log.info("recovered %s interrupted jobs", recovered)
    except Exception:
        log.exception("reset_running_jobs failed")

    def ensure_sources() -> dict[str, dict]:
        if not sources_loaded["done"]:
            logging.getLogger("musicdl").setLevel(logging.WARNING)  # musicdl 话痨日志降噪
            sources_loaded["status"] = load_sources(
                dirs=cfg.sources.dirs or None,
                order=cfg.sources.order, workdir=cfg.paths.musicdl_workdir,
                enabled=cfg.sources.enabled, disabled=cfg.sources.disabled,
            )
            sources_loaded["done"] = True
        return sources_loaded["status"]

    def ensure_source_adapters() -> dict[str, dict]:
        """快速加载音源但不探活，供匹配/搜索立即启动。

        探活本身可能各花 10~20 秒；用户点“匹配”不应先等两轮探活。
        单个源失败由搜索调用自己的异常处理捕获，不会拖垮整批。
        """
        if not sources_loaded["done"]:
            logging.getLogger("musicdl").setLevel(logging.WARNING)
            sources_loaded["status"] = load_sources(
                dirs=cfg.sources.dirs or None,
                order=cfg.sources.order, workdir=cfg.paths.musicdl_workdir, probe=False,
                enabled=cfg.sources.enabled, disabled=cfg.sources.disabled,
            )
            sources_loaded["done"] = True
        return sources_loaded["status"]

    def get_client() -> Client:
        if "c" not in client_holder:
            client_holder["c"] = Client(cfg.paths.cookies)
        return client_holder["c"]

    def _items_from_jobs(jobs: list[dict]) -> list[dict]:
        """把持久化任务还原成 worker 入参；坏 payload 直接标失败，不阻塞其他任务。"""
        import json as _json

        out: list[dict] = []
        for job in jobs:
            try:
                item = _json.loads(job.get("payload_json") or "{}")
            except Exception:
                state.mark_job(job["id"], "failed", "payload 损坏")
                continue
            if not isinstance(item, dict) or not item.get("song") or not item.get("track"):
                state.mark_job(job["id"], "failed", "payload 缺少 song/track")
                continue
            item["job_id"] = job["id"]
            item.setdefault("scan_id", 0)
            out.append(item)
        return out

    def _append_queue(items: list[dict]) -> None:
        """把已持久化的任务放进内存队列，并按需启动 worker。"""
        if not items:
            return
        with dl_state["lock"]:
            dl_state["queue"].extend(items)
            need_start = not dl_state["running"]
        if need_start:
            dl_state["running"] = True
            try:
                threading.Thread(target=_dl_worker, daemon=True).start()
            except Exception:
                # 线程起不来时必须回滚 running，否则后续入队永远不会再启动
                dl_state["running"] = False
                log.exception("failed to start download worker")
                raise

    def resume_persisted_queue() -> None:
        """把数据库里 queued 的任务恢复到内存队列（服务重启后续跑）。"""
        jobs = state.queued_jobs()
        if not jobs:
            return
        # 恢复的任务按最近一次扫描记账（仅影响统计，不影响下载/上传）
        try:
            state.conn.execute("PRAGMA journal_mode=WAL")
        except Exception:
            log.exception("enable WAL failed")
        items = _items_from_jobs(jobs)
        _append_queue(items)
        if items:
            log.info("resumed %s queued jobs", len(items))

    resume_lock = threading.Lock()
    resume_state = {"attempted": False}

    def maybe_resume_queue() -> None:
        """首次确认登录后恢复持久队列；只尝试一次，避免每个请求都探测登录态。"""
        with resume_lock:
            if resume_state["attempted"] or not state.queued_jobs():
                return
            resume_state["attempted"] = True
        try:
            if get_client().is_logged_in():
                resume_persisted_queue()
        except Exception:
            log.exception("resume persisted queue failed")

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
        if logged:
            maybe_resume_queue()
        try:
            queued = len(state.queued_jobs())
        except Exception:
            queued = 0
        return {
            "logged_in": logged,
            "user_id": acc.get("user_id"),
            "nickname": acc.get("nickname"),
            "config": {
                "quality": cfg.scan.quality,
                "limit": cfg.scan.limit,
                "sleep_min": cfg.scan.sleep_min,
                "sleep_max": cfg.scan.sleep_max,
                "upload_attempts": cfg.scan.upload_attempts,
            },
            "queue_len": len(dl_state["queue"]),
            "persisted_queued": queued,
            "running": dl_state["running"],
        }

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
        """一键登录：启动本机 Chrome/Edge，用户在官方页面登录后通过 CDP 读取 cookie。

        不依赖 Playwright，也不会下载浏览器内核；浏览器 profile 在 data/browser_profile/。
        """
        if browser_login_running["v"]:
            raise HTTPException(409, "已有一个浏览器登录窗口在进行中")
        browser_login_running["v"] = True
        try:
            from ..netease.browser_login import login_via_browser

            result = login_via_browser(
                cfg.paths.cookies,
                profile_dir=cfg.paths.data_dir / "browser_profile",
                executable=cfg.browser.executable,
                debug_port=cfg.browser.debug_port,
                timeout_s=cfg.browser.login_timeout_s,
            )
        except Exception as e:
            from ..netease.browser_login import BrowserLoginError

            log.exception("browser login failed")
            code = 408 if isinstance(e, BrowserLoginError) and "超时" in str(e) else 500
            raise HTTPException(code, f"浏览器登录失败：{e}") from e
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
        # 历史已成功上传过的歌不重复推荐（但仍显示，标为 processed，前端默认不勾选）
        try:
            latest = state.latest_upload_status()
        except Exception:
            log.exception("latest_upload_status failed")
            latest = {}
        gap_payload = []
        for g in result.gap:
            row = g.as_dict()
            row["processed"] = latest.get(g.id) == "ok"
            gap_payload.append(row)
        return {
            "scan_id": scan_id,
            "summary": result.summary(),
            "gap": gap_payload,
            "report_file": report.name,
        }

    @app.get("/api/sources")
    def sources():
        status = ensure_sources()
        return {
            "sources": status,
            "dirs": cfg.sources.dirs,
            "enabled": cfg.sources.enabled,
            "disabled": cfg.sources.disabled,
            "order": cfg.sources.order,
        }

    @app.post("/api/sources/reload")
    def reload_sources():
        """重新扫描音源目录；改适配器或配置后无需重启进程。

        只重载适配器注册表，不影响已在跑的任务；新任务使用新集合。
        """
        sources_loaded["done"] = False
        sources_loaded.pop("status", None)
        status = ensure_source_adapters()
        return {
            "reloaded": True,
            "sources": status,
            "dirs": cfg.sources.dirs,
            "enabled": cfg.sources.enabled,
            "disabled": cfg.sources.disabled,
        }

    @app.post("/api/sources/probe")
    def probe_sources():
        """主动探活当前音源集合；不重载文件，只刷新健康状态。"""
        sources_loaded["done"] = False
        sources_loaded.pop("status", None)
        status = ensure_sources()
        return {
            "sources": status,
            "dirs": cfg.sources.dirs,
            "enabled": cfg.sources.enabled,
            "disabled": cfg.sources.disabled,
            "order": cfg.sources.order,
        }

    @app.post("/api/search")
    def search(req: SearchReq):
        """按歌名搜索网易曲库（新增功能的入口，不触发任何写操作）。

        返回与缺口清单同构的字段，前端可直接复用现有确认/下载流水线。
        """
        keyword = (req.keyword or "").strip()
        if not keyword:
            raise HTTPException(400, "请输入歌名")
        c = get_client()
        try:
            rows = c.search_songs(keyword, limit=max(1, min(req.limit, 30)))
        except Exception as e:
            log.exception("search failed")
            raise HTTPException(502, f"搜索失败：{e}") from e
        # 标注是否已在云盘（精确/模糊），方便用户避免重复补歌
        try:
            cloud = gap_finder._CloudIndex(c.cloud_items())
        except Exception as e:
            log.warning("cloud index unavailable: %s", e)
            cloud = None
        out = []
        for s in rows:
            row = dict(s)
            row["in_cloud"] = False
            if cloud:
                hit, info = cloud.hit(s["id"], s.get("name") or "", s.get("duration_ms") or 0)
                row["in_cloud"] = bool(hit)
                row["cloud_hit"] = hit
                row["cloud_info"] = info
            out.append(row)
        return {"keyword": keyword, "songs": out}

    @app.post("/api/source-search")
    def source_search(req: SourceSearchReq):
        """搜索补歌：关键词直接搜音源，先返回已完成音源，慢源稍后合并。

        响应带 token；若仍有音源在跑，前端轮询 /api/source-search/results 追加。
        """
        keyword = (req.keyword or "").strip()
        if not keyword:
            raise HTTPException(400, "请输入歌名")
        ensure_source_adapters()
        registry = get_registry()
        healthy = list(registry)  # 不探活，直接搜；单源失败在下方捕获
        if not healthy:
            raise HTTPException(400, "没有可用音源（请检查 [sources].dirs / enabled / disabled）")

        limit = max(1, min(req.limit, 30))
        # 用户可能只输入“歌名”，也可能输入“歌名 歌手”；统一生成查询变体，
        # 交给音源自己匹配。超时也保留已返回结果。
        parts = keyword.split(maxsplit=1)
        head = parts[0]
        tail = parts[1] if len(parts) > 1 else ""
        queries = _source_queries(head, tail, keyword)
        pending: list[dict] = []
        tracks, errors = _collect_source_tracks(
            registry, healthy, queries, limit, cfg.scan.source_search_timeout_s,
            first_batch_s=cfg.scan.source_search_first_batch_s,
            pending_out=pending,
        )
        token = uuid.uuid4().hex
        if len(search_sessions) >= 8:
            oldest = next(iter(search_sessions))
            stale = search_sessions.pop(oldest, None)
            # 被淘汰会话若还有慢源在跑，先取消任务并关掉线程池，避免残留连接。
            for group in (stale or {}).get("pending", []):
                group["executor"].shutdown(wait=False, cancel_futures=True)
        search_sessions[token] = {
            "keyword": keyword, "tracks": tracks, "pending": pending,
            "errors": errors, "done": not pending,
        }
        return {
            "keyword": keyword, "sources": healthy,
            "versions": _versions_payload(keyword, tracks),
            "errors": errors, "token": token,
            "pending": sum(len(g["jobs"]) for g in pending),
        }

    @app.post("/api/source-search/results")
    def source_search_results(req: SourceSearchResultReq):
        """合并搜索会话中已完成的慢音源结果；前端在 pending>0 时轮询。"""
        session = search_sessions.get(req.token)
        if session is None:
            raise HTTPException(404, "搜索会话不存在或已过期，请重新搜索")
        groups = session["pending"]
        changed = False
        keep_groups: list[dict] = []
        for group in groups:
            executor = group["executor"]
            keep_jobs: list[dict] = []
            for job in group["jobs"]:
                fut = job["future"]
                if not fut.done():
                    keep_jobs.append(job)
                    continue
                try:
                    source_tracks, source_errors, _ = fut.result()
                    # 慢源结果去重：同一来源/标题/歌手/专辑/时长只合并一次。
                    existing = {
                        (t.source, t.title, t.artist, t.album, t.duration_s)
                        for t in session["tracks"]
                    }
                    for track in source_tracks:
                        key = (track.source, track.title, track.artist, track.album, track.duration_s)
                        if key not in existing:
                            session["tracks"].append(track)
                            existing.add(key)
                    session["errors"].extend(source_errors)
                    changed = True
                except Exception as e:
                    session["errors"].append(f"{job['name']}：{str(e)[:120]}")
            if keep_jobs:
                keep_groups.append({"executor": executor, "jobs": keep_jobs})
            else:
                executor.shutdown(wait=False, cancel_futures=True)
        session["pending"] = keep_groups
        session["done"] = not keep_groups
        return {
            "keyword": session["keyword"],
            "versions": _versions_payload(session["keyword"], session["tracks"]),
            "errors": session["errors"],
            "pending": sum(len(g["jobs"]) for g in keep_groups),
            "done": session["done"],
            "changed": changed,
        }

    @app.post("/api/match/start")
    def match_start(req: MatchStartReq):
        """启动后台匹配任务（匹配结果必须经人工确认）。"""
        if req.origin not in {"gap", "search"}:
            raise HTTPException(400, "origin 只能是 gap 或 search")
        ensure_source_adapters()
        registry = get_registry()  # 适配器实例表；ensure_source_adapters() 只加载不探活
        healthy = list(registry)
        if not healthy:
            raise HTTPException(400, "没有可用音源（请检查 [sources].dirs / enabled / disabled）")
        if not (1 <= len(req.songs) <= max(1, cfg.scan.limit * 10)):
            raise HTTPException(400, f"请选择 1~{max(1, cfg.scan.limit * 10)} 首歌")
        if match_job["running"]:
            raise HTTPException(409, "已有匹配任务在进行中")
        match_job["running"] = True
        match_job["total"] = len(req.songs)
        match_job["done_count"] = 0
        match_job["results"] = []
        match_job["errors"] = []
        match_job["origin"] = req.origin
        threading.Thread(
            target=_run_match_job,
            args=(
                match_job, req.songs, registry, healthy, get_client(),
                cfg.scan.match_sleep_min, cfg.scan.match_sleep_max, cfg.scan.quality,
                cfg.scan.match_source_timeout_s,
            ),
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
            "origin": match_job.get("origin", "gap"),
            "current": dict(match_job.get("current") or {}),
            "errors": list(match_job.get("errors") or []),
        }

    @app.post("/api/download")
    def download(req: DownloadReq):
        """把人工确认的批次加入后台队列：下载 → 打标 → 上传云盘，立即返回。"""
        if not (1 <= len(req.items) <= cfg.scan.limit):
            raise HTTPException(400, f"单批 1~{cfg.scan.limit} 首")
        if req.origin not in {"gap", "search"}:
            raise HTTPException(400, "origin 只能是 gap 或 search")
        scan_id = 0
        if req.origin == "gap" and last_scan["scan_id"]:
            scan_id = int(last_scan["scan_id"])
        items = []
        for raw in req.items:
            song, track = raw.get("song"), raw.get("track")
            if not isinstance(song, dict) or not isinstance(track, dict):
                raise HTTPException(400, "items 必须包含 song/track 对象")
            if not song.get("id") or not song.get("name") or not track.get("download_url"):
                raise HTTPException(400, "song.id/name 与 track.download_url 不能为空")
            if not str(track.get("download_url")).startswith(("http://", "https://")):
                raise HTTPException(400, "track.download_url 只允许 http/https 地址")
            quality = str(raw.get("quality") or "").lower()
            if quality and quality not in {"flac", "320", "128"}:
                raise HTTPException(400, "quality 只能是 flac / 320 / 128")
            items.append({**raw, "scan_id": scan_id, "origin": req.origin, "quality": quality})
        with dl_state["lock"]:
            if len(dl_state["queue"]) + len(items) > cfg.scan.limit * 6:
                raise HTTPException(400, "后台队列积压过多，请先等当前批次完成")
        # 先持久化再入内存队列：重启时 jobs 与内存队列保持一致
        job_ids = state.enqueue(items)
        for item, job_id in zip(items, job_ids):
            item["job_id"] = job_id
        try:
            _append_queue(items)
        except Exception:
            # 已落库但 worker 没起来：标失败，避免界面显示“已入队”却永不执行
            for job_id in job_ids:
                state.mark_job(job_id, "failed", "worker 启动失败")
            raise HTTPException(500, "后台任务启动失败，详见 data/logs/music_refill.log")
        return {"queued": len(items), "queue_len": len(dl_state["queue"]), "job_ids": job_ids}

    @app.post("/api/quick-add")
    def quick_add(req: QuickAddReq):
        """搜索选中 → 后台自动匹配 → 只在高置信度时直接入队。

        立即返回任务已启动；前端轮询 `/api/quick-add/status`。
        低置信度不猜，结果留在 needs_review 供人工确认。
        """
        if not (1 <= len(req.songs) <= cfg.scan.limit):
            raise HTTPException(400, f"单次 1~{cfg.scan.limit} 首")
        if quick_job["running"]:
            raise HTTPException(409, "已有快速补歌任务在进行中，请等待完成")
        with dl_state["lock"]:
            backlog = len(dl_state["queue"]) + len(req.songs)
        if backlog > cfg.scan.limit * 6:
            raise HTTPException(400, "后台队列积压过多，请先等当前批次完成")
        ensure_source_adapters()
        registry = get_registry()
        healthy = list(registry)
        if not healthy:
            raise HTTPException(400, "没有可用音源（请检查 [sources].dirs / enabled / disabled）")
        # 阈值下限锁在自动推荐分：不允许调用方把明显低分的候选强制直传。
        if req.min_score is not None and req.min_score < 0:
            raise HTTPException(400, "min_score 不能为负数")
        requested = cfg.scan.quick_add_min_score if req.min_score is None else float(req.min_score)
        threshold = max(downloader.AUTO_CONFIRM_THRESHOLD, min(3.0, requested))

        def enqueue_cb(items: list[dict]) -> list[int]:
            with dl_state["lock"]:
                if len(dl_state["queue"]) + len(items) > cfg.scan.limit * 6:
                    raise RuntimeError("后台队列积压过多，已留作人工确认")
            ids = state.enqueue(items)
            for i, job_id in zip(items, ids):
                i["job_id"] = job_id
            try:
                _append_queue(items)
            except Exception:
                for job_id in ids:
                    state.mark_job(job_id, "failed", "worker 启动失败")
                raise
            return ids

        quick_job.update({
            "running": True, "total": len(req.songs), "done_count": 0,
            "threshold": threshold, "queued": [], "needs_review": [],
            "skipped": [], "errors": [],
        })
        try:
            threading.Thread(
                target=_run_quick_add_job,
                args=(
                    quick_job, req.songs, registry, healthy, Client(cfg.paths.cookies),
                    cfg.scan.quality, threshold,
                    cfg.scan.match_sleep_min, cfg.scan.match_sleep_max, enqueue_cb,
                ),
                daemon=True,
            ).start()
        except Exception:
            quick_job["running"] = False
            log.exception("failed to start quick-add worker")
            raise HTTPException(500, "快速补歌任务启动失败，详见 data/logs/music_refill.log")
        return {"started": True, "total": len(req.songs), "threshold": threshold}

    @app.get("/api/quick-add/status")
    def quick_add_status():
        """轮询快速补歌进度；完成后返回入队数量、待确认候选与跳过原因。"""
        return {
            "running": quick_job["running"],
            "total": quick_job["total"],
            "done": quick_job["done_count"],
            "threshold": quick_job["threshold"],
            "queued": len(quick_job["queued"]),
            "job_ids": [item.get("job_id") for item in quick_job["queued"]],
            "needs_review": quick_job["needs_review"],
            "skipped": quick_job["skipped"],
            "errors": quick_job["errors"],
        }

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
                "verified": sum(1 for r in dl_state["results"] if r.get("verified")),
                "playable": sum(1 for r in dl_state["results"] if r.get("playable") is True),
            }
        return snap

    @app.get("/api/tasks")
    def tasks():
        """全局任务栏：匹配/下载/队列的轻量快照（前端每秒轮询）。"""
        with dl_state["lock"]:
            ok = sum(1 for r in dl_state["results"] if r["status"] == "ok")
            recent = [
                {
                    "name": r.get("name"), "status": r.get("status"),
                    "origin": r.get("origin", "gap"), "detail": r.get("detail", ""),
                    "instant": bool(r.get("instant")), "verified": bool(r.get("verified")),
                    "quality": r.get("quality", ""),
                    "quality_label": r.get("quality_label", ""),
                }
                for r in dl_state["results"][-5:]
            ]
            snapshot = {
                "running": dl_state["running"],
                "current": dict(dl_state["current"]) if dl_state["current"] else None,
                "queue_len": len(dl_state["queue"]),
                "done": len(dl_state["results"]),
                "ok": ok,
                "failed": len(dl_state["results"]) - ok,
                "recent": recent,
            }
        snapshot["match"] = {
            "running": match_job["running"],
            "done": match_job["done_count"],
            "total": match_job["total"],
            "origin": match_job.get("origin", "gap"),
        }
        snapshot["persisted_queued"] = len(state.queued_jobs())
        return snapshot

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

    @app.post("/api/jobs/retry")
    def jobs_retry(limit: int = 20):
        """把失败任务重新入队（用户显式触发，不会无限自动重跑）。"""
        jobs = state.requeue_failed(limit=limit)
        items = _items_from_jobs(jobs)
        try:
            _append_queue(items)
        except Exception:
            for job in jobs:
                state.mark_job(job["id"], "failed", "worker 启动失败")
            raise
        return {"requeued": len(items)}

    def _dl_worker():
        """后台流水线：下载 → 打标 → 上传，逐首限速；队列为空即退出。"""
        try:
            _dl_worker_loop()
        except Exception:
            log.exception("download worker crashed")
            with dl_state["lock"]:
                dl_state["running"] = False

    def _dl_worker_loop():
        worker_client = Client(cfg.paths.cookies)  # 独立会话，避免与请求线程共享
        dest_dir = cfg.paths.data_dir / "downloads"
        policy = RetryPolicy(
            attempts=max(1, cfg.scan.upload_attempts),
            base_s=cfg.scan.retry_backoff_s,
            max_s=cfg.scan.retry_backoff_max_s,
        )
        while True:
            with dl_state["lock"]:
                if not dl_state["queue"]:
                    dl_state["running"] = False
                    dl_state["current"] = None
                    return
                item = dl_state["queue"].pop(0)
            song, t = item["song"], item["track"]
            job_id = item.get("job_id")
            if job_id:
                state.update_job(int(job_id), status="running")
            stage = "下载"
            attempt = 1
            verified = None
            try:
                while True:
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
                        quality = str(item.get("quality") or "").lower()
                        bitrate = downloader.QUALITY_BITRATES.get(quality) or (track.bitrate or 999000)
                        up = upload_cloud_file(
                            worker_client, path, song["name"], "、".join(song["artists"]),
                            song.get("album") or track.album, bitrate=bitrate,
                        )
                        stage = "校验云盘"
                        with dl_state["lock"]:
                            dl_state["current"] = {"name": song["name"], "stage": "校验云盘", "recv": None, "total": None}
                        verified = verify_upload(
                            worker_client, up["songId"], md5=up.get("md5", ""),
                            file_name=up.get("file_name", ""),
                        )
                        break
                    except Exception as e:
                        if attempt >= policy.attempts or not is_retryable(e):
                            raise
                        delay = policy.delay(attempt)
                        log.warning(
                            "job %s 第 %s 次失败（%s），%.1fs 后重试：%s",
                            item.get("job_id"), attempt, stage, delay, str(e)[:160],
                        )
                        with dl_state["lock"]:
                            dl_state["current"] = {
                                "name": song["name"], "stage": f"{stage}失败，等待重试",
                                "recv": None, "total": None,
                            }
                        time.sleep(delay)
                        attempt += 1
                quality = str(item.get("quality") or "").lower()
                bitrate = downloader.QUALITY_BITRATES.get(quality) or (track.bitrate or 999000)
                res = {
                    "song_id": song["id"], "name": song["name"], "status": "ok", "file": path.name,
                    "quality": quality or downloader.track_quality(track),
                    "quality_label": downloader.quality_label(quality or downloader.track_quality(track)),
                    "origin": item.get("origin", "gap"),
                    "file_ext": track.ext, "size_kb": track.size_bytes // 1024,
                    "cloud_song_id": verified["cloud_song_id"] if verified else up["songId"],
                    "matched_library": up["matched_library"],
                    "instant": not up["need_upload"],
                    "verified": bool(verified and verified["verified"]),
                    "playable": None if not verified else verified.get("playable"),
                    "attempt": attempt,
                }
            except Exception as e:
                log.exception("download/upload failed at [%s]: %s", stage, song["name"])
                res = {"song_id": song["id"], "name": song["name"], "status": "failed",
                       "origin": item.get("origin", "gap"), "quality": item.get("quality", ""),
                       "detail": f"[{stage}] {str(e)[:130]}", "attempt": attempt}
            with dl_state["lock"]:
                dl_state["results"].append(res)
                dl_state["current"] = None
            scan_id = int(item.get("scan_id") or 0)
            # 按歌名补歌没有 scan_id，但成功记录必须落库，否则 processed/历史统计会漏
            if scan_id or res["status"] == "ok":
                state.save_upload(
                    scan_id, song["id"],
                    "ok" if res["status"] == "ok" else "failed",
                    route="download", quality=res.get("quality") or "",
                    detail=res.get("detail") or (
                        f"cloud:{res['cloud_song_id']} matched={res['matched_library']}"
                        f" verified={res.get('verified')} playable={res.get('playable')}"
                    ),
                    song=song, track=t, attempt=attempt,
                )
            if job_id:
                state.update_job(
                    int(job_id),
                    status="ok" if res["status"] == "ok" else "failed",
                    attempts=attempt,
                    last_error=res.get("detail") or "",
                    cloud_song_id=int(res.get("cloud_song_id") or 0),
                )
            downloader.sleep_between(cfg.scan.sleep_min, cfg.scan.sleep_max)

    @app.get("/api/report/{filename}")
    def report(filename: str):
        # 只允许取 reports/ 目录下的 .md 文件，防路径穿越
        if "/" in filename or "\\" in filename or not filename.endswith(".md"):
            raise HTTPException(400, "非法文件名")
        path = cfg.paths.reports / filename
        if not path.exists():
            raise HTTPException(404, "报告不存在")
        return FileResponse(path, media_type="text/markdown", filename=filename)

    @app.get("/api/history")
    def history(limit: int = 20):
        """交接排障用：最近上传记录、任务记录与去重后的成功/失败统计。"""
        return {
            "summary": state.upload_summary(),
            "uploads": state.recent_uploads(limit=limit),
            "jobs": state.recent_jobs(limit=limit),
            "configured": {
                "quality": cfg.scan.quality,
                "limit": cfg.scan.limit,
                "sleep": [cfg.scan.sleep_min, cfg.scan.sleep_max],
                "sources_order": cfg.sources.order,
            },
        }

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
