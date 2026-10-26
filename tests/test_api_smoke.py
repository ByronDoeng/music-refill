"""接口冒烟：不登录、不联网，只验证路由与错误码契约。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from music_refill.web.app import create_app
from music_refill.state import State
import time


def test_status_and_static(tmp_cfg):
    app = create_app(tmp_cfg)
    with TestClient(app) as c:
        r = c.get("/api/status")
        assert r.status_code == 200
        body = r.json()
        assert "logged_in" in body
        assert body["config"]["limit"] == tmp_cfg.scan.limit
        assert c.get("/").status_code == 200
        assert c.get("/static/app.js").status_code == 200


def test_search_requires_keyword(tmp_cfg):
    with TestClient(create_app(tmp_cfg)) as c:
        r = c.post("/api/search", json={"keyword": "  "})
        assert r.status_code == 400


def test_download_batch_limit(tmp_cfg):
    with TestClient(create_app(tmp_cfg)) as c:
        r = c.post("/api/download", json={"items": []})
        assert r.status_code == 400


def test_download_rejects_non_http_url(tmp_cfg):
    """队列只接受 http/https，避免把本地服务变成任意协议请求入口。"""
    with TestClient(create_app(tmp_cfg)) as c:
        item = {
            "song": {"id": 8, "name": "测试", "artists": ["A"], "album": ""},
            "track": {"source": "t", "title": "测试", "artist": "A", "ext": "mp3",
                      "size_bytes": 1, "download_url": "file:///etc/passwd"},
            "quality": "320",
        }
        r = c.post("/api/download", json={"items": [item], "origin": "search"})
        assert r.status_code == 400


def test_report_path_traversal_rejected(tmp_cfg):
    with TestClient(create_app(tmp_cfg)) as c:
        assert c.get("/api/report/..%2F..%2Fconfig.toml").status_code in (400, 404)


def test_history_shape(tmp_cfg):
    with TestClient(create_app(tmp_cfg)) as c:
        r = c.get("/api/history")
        assert r.status_code == 200
        assert "uploads" in r.json() and "jobs" in r.json()


def test_sources_payload_lists_dirs_and_enabled_flags(tmp_cfg, monkeypatch):
    """状态接口要能看出加载了哪些目录、哪些被停用，便于交接排查。"""
    from music_refill.web import app as web_app

    monkeypatch.setattr(web_app, "load_sources", lambda **k: {"fake": {"healthy": True, "error": "", "priority": 0}})
    tmp_cfg.sources.dirs = ["a", "b"]
    tmp_cfg.sources.enabled = []
    tmp_cfg.sources.disabled = ["old"]
    with TestClient(create_app(tmp_cfg)) as c:
        body = c.get("/api/sources").json()
    assert body["dirs"] == ["a", "b"]
    assert body["disabled"] == ["old"]
    assert body["sources"]["fake"]["healthy"] is True


def test_download_persists_job_and_links_status(tmp_cfg, monkeypatch):
    """回归：/api/download 必须把 job_id 关联到内存 item，worker 才能写回状态。"""
    from music_refill.core import downloader

    monkeypatch.setattr(downloader, "sleep_between", lambda *a, **k: None)

    def fail_download(*a, **k):
        raise RuntimeError("test network disabled")

    monkeypatch.setattr(downloader.requests, "get", fail_download)
    with TestClient(create_app(tmp_cfg)) as c:
        item = {
            "song": {"id": 7, "name": "测试歌", "artists": ["A"], "album": "B"},
            "track": {
                "source": "test", "title": "测试歌", "artist": "A",
                "ext": "mp3", "size_bytes": 1, "download_url": "https://example.invalid/x",
            },
        }
        r = c.post("/api/download", json={"items": [item]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["queued"] == 1
        assert body["job_ids"]

    st = State(tmp_cfg.paths.db)
    jobs = st.recent_jobs(10)
    assert jobs and jobs[0]["id"] == body["job_ids"][0]
    # 等 worker 写回失败状态；不能一直停在 queued（旧 bug 的表现）
    for _ in range(30):
        jobs = st.recent_jobs(10)
        if jobs and jobs[0]["status"] in {"ok", "failed"}:
            break
        time.sleep(0.05)
    assert jobs[0]["status"] in {"ok", "failed"}


def test_quick_add_requires_songs(tmp_cfg):
    with TestClient(create_app(tmp_cfg)) as c:
        r = c.post("/api/quick-add", json={"songs": []})
        assert r.status_code == 400


def test_search_falls_back_when_first_endpoint_fails():
    """第一个 cloudsearch 路径异常时必须降级到 pc 路径，而不是整首匹配失败。"""
    from music_refill.netease.client import Client

    c = Client()
    calls = []

    def fake_call_ws(path, payload, timeout=30):
        calls.append(path)
        if path.endswith("/get/web"):
            raise RuntimeError("460 接口频繁")
        return {"result": {"songs": [{
            "id": 1, "name": "晴天", "ar": [{"name": "周杰伦"}],
            "al": {"name": "叶惠美"}, "fee": 1, "duration": 269000,
        }]}}

    c.call_ws = fake_call_ws
    rows = c.search_songs("晴天", limit=5)
    assert calls[-1].endswith("/cloudsearch/pc")
    assert rows[0]["name"] == "晴天" and rows[0]["duration_ms"] == 269000


def test_quick_add_runs_in_background_and_polls(tmp_cfg, monkeypatch):
    """高分候选在后台入队；接口先返回 started，再通过状态接口暴露结果。"""
    from music_refill.sources import get_registry
    from music_refill.sources.base import clear_registry
    from music_refill.web import app as web_app
    from music_refill.sources.base import SourceTrack
    from music_refill.core import downloader

    class FakeSource:
        name = "fake"

        def search(self, title, artist="", limit=5):
            return [SourceTrack(
                source="fake", title=title, artist="A", ext="mp3",
                size_bytes=1, duration_s=269,
                download_url="https://example.invalid/x",
            )]

    clear_registry()
    get_registry()["fake"] = FakeSource()
    monkeypatch.setattr(web_app, "load_sources", lambda **k: {
        "fake": {"healthy": True, "error": "", "priority": 0},
    })
    monkeypatch.setattr(web_app, "get_registry", lambda: get_registry())
    monkeypatch.setattr(web_app.Client, "search_songs", lambda self, kw, limit=5: [])
    monkeypatch.setattr(downloader, "sleep_between", lambda *a, **k: None)
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)
    monkeypatch.setattr(downloader.requests, "get", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))

    with TestClient(create_app(tmp_cfg)) as c:
        song = {"id": 2, "name": "晴天", "artists": ["周杰伦"], "album": "叶惠美", "duration_ms": 269000}
        r = c.post("/api/quick-add", json={"songs": [song], "min_score": 0.1})
        assert r.status_code == 200, r.text
        assert r.json()["started"] is True
        for _ in range(60):
            st = c.get("/api/quick-add/status").json()
            if not st["running"]:
                break
            time.sleep(0.05)
    assert st["done"] == 1
    assert st["threshold"] >= downloader.AUTO_CONFIRM_THRESHOLD
    assert st["queued"] == 1 and st["job_ids"]
    assert st["needs_review"] == []


def test_quick_add_low_score_falls_back_to_review(tmp_cfg, monkeypatch):
    from music_refill.sources import get_registry
    from music_refill.sources.base import clear_registry
    from music_refill.web import app as web_app
    from music_refill.sources.base import SourceTrack

    class WeakSource:
        name = "weak"

        def search(self, title, artist="", limit=5):
            return [SourceTrack(
                source="weak", title="完全无关的歌名", artist="X", ext="mp3",
                size_bytes=1, duration_s=10,
                download_url="https://example.invalid/x",
            )]

    clear_registry()
    get_registry()["weak"] = WeakSource()
    monkeypatch.setattr(web_app, "load_sources", lambda **k: {
        "weak": {"healthy": True, "error": "", "priority": 0},
    })
    monkeypatch.setattr(web_app, "get_registry", lambda: get_registry())
    monkeypatch.setattr(web_app.Client, "search_songs", lambda self, kw, limit=5: [])
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)

    with TestClient(create_app(tmp_cfg)) as c:
        song = {"id": 3, "name": "晴天", "artists": ["周杰伦"], "album": "", "duration_ms": 269000}
        r = c.post("/api/quick-add", json={"songs": [song], "min_score": 1.15})
        assert r.status_code == 200, r.text
        for _ in range(60):
            st = c.get("/api/quick-add/status").json()
            if not st["running"]:
                break
            time.sleep(0.05)
    assert st["queued"] == 0
    assert len(st["needs_review"]) == 1
    assert st["needs_review"][0]["candidates"]


def test_group_candidates_merges_quality_and_prefers_desired():
    from music_refill.core import downloader
    from music_refill.sources.base import SourceTrack

    song = {"name": "晴天", "artists": ["周杰伦"], "duration_ms": 269000}
    tracks = [
        SourceTrack(source="a", title="晴天", artist="周杰伦", ext="flac", duration_s=269,
                    size_bytes=100, download_url="https://x/1"),
        SourceTrack(source="a", title="晴天", artist="周杰伦", ext="mp3", bitrate=320000,
                    duration_s=269, size_bytes=90, download_url="https://x/2"),
        SourceTrack(source="a", title="晴天", artist="周杰伦", ext="mp3", bitrate=128000,
                    duration_s=269, size_bytes=80, download_url="https://x/3"),
    ]
    versions = downloader.group_candidates(song, tracks)
    assert len(versions) == 1
    assert versions[0]["available_qualities"] == ["flac", "320", "128"]
    assert downloader.choose_quality(versions[0]["available_qualities"], "320") == "320"
    assert downloader.choose_quality(["flac", "128"], "320") == "flac"
    assert downloader.choose_quality(["128"], "320") == "128"


def test_source_search_rejects_empty_keyword(tmp_cfg):
    with TestClient(create_app(tmp_cfg)) as c:
        r = c.post("/api/source-search", json={"keyword": "  "})
        assert r.status_code == 400


def test_match_song_queries_title_and_artist_without_netease(monkeypatch):
    """匹配不得再走站内参照；应并发查询标题与标题+歌手。"""
    from music_refill.web import app as web_app
    from music_refill.sources.base import SourceTrack
    from music_refill.sources import get_registry
    from music_refill.sources.base import clear_registry

    calls = []

    class FakeSource:
        name = "fake"

        def search(self, title, artist="", limit=5):
            calls.append(title)
            # 首轮“晴天”故意不回精确同名，确保仍会追加“晴天 周杰伦”；
            # 若首轮精确命中，匹配器会提前停止以避开慢源。
            candidate_title = title if " " in title else "别的歌"
            return [SourceTrack(source="fake", title=candidate_title, artist="A", ext="flac",
                                duration_s=200, size_bytes=1, download_url="https://x/1")]

    clear_registry()
    get_registry()["fake"] = FakeSource()
    song = {"id": 1, "name": "晴天", "artists": ["周杰伦"], "album": "", "duration_ms": 269000}
    _, ranked = web_app._match_song(song, get_registry(), ["fake"], object(), "")
    assert "晴天" in calls and "晴天 周杰伦" in calls
    assert ranked


def test_source_queries_clean_space_and_artist_variants():
    """全角/零宽空格、多人歌手和“歌名 - 歌手”都应生成可搜索变体。"""
    from music_refill.web import app as web_app

    queries = web_app._source_queries("晴天\u3000", "周杰伦、方文山", "晴天\u3000- 周杰伦")
    assert queries[0] == "晴天"
    assert "晴天 周杰伦、方文山" in queries
    assert "晴天 周杰伦" in queries


def test_frontend_has_dashboard_and_unchecked_search(tmp_cfg):
    """纯静态契约：登录后是 Dashboard 入口；搜索候选默认不勾选。"""
    app = create_app(tmp_cfg)
    with TestClient(app) as c:
        html = c.get("/").text
        js = c.get("/static/app.js").text
    assert 'id="dashboard-card"' in html and "歌单补歌" in html and "搜索补歌" in html
    assert "①" not in html and "②" not in html
    assert 'class="search-check" data-idx="${idx}" ${selected ? "checked" : ""}' in js


def test_frontend_scan_stops_at_gap_list(tmp_cfg):
    """歌单补歌必须先在缺口清单确认，再启动匹配。"""
    with TestClient(create_app(tmp_cfg)) as c:
        html = c.get("/").text
        js = c.get("/static/app.js").text
    assert 'id="gap-card"' in html and "为选中歌曲匹配音源" in html
    assert "renderGaps(r);" in js
    assert "await startMatch();" not in js


def test_download_rejects_invalid_quality(tmp_cfg):
    with TestClient(create_app(tmp_cfg)) as c:
        item = {
            "song": {"id": 8, "name": "测试", "artists": ["A"], "album": ""},
            "track": {"source": "t", "title": "测试", "artist": "A", "ext": "mp3",
                      "size_bytes": 1, "download_url": "https://example.invalid/x"},
            "quality": "lossless",
        }
        r = c.post("/api/download", json={"items": [item], "origin": "search"})
        assert r.status_code == 400


def test_source_search_returns_first_batch_then_merges_slow_source(tmp_cfg, monkeypatch):
    """首批只等已完成音源；慢源由 /results 合并，不能把结果丢掉。"""
    import time as _time
    from music_refill.web import app as web_app
    from music_refill.sources.base import SourceTrack, clear_registry
    from music_refill.sources import get_registry

    class FastSource:
        name = "fast"

        def quick_search(self, title, artist="", limit=5):
            return [SourceTrack(source="fast", title=title, artist="A", ext="flac",
                                duration_s=200, size_bytes=10,
                                download_url="https://x/fast")]

    class SlowSource:
        name = "slow"

        def quick_search(self, title, artist="", limit=5):
            _time.sleep(1.5)
            return [SourceTrack(source="slow", title=title, artist="B", ext="mp3",
                                duration_s=201, size_bytes=11,
                                download_url="https://x/slow")]

    clear_registry()
    get_registry()["fast"] = FastSource()
    get_registry()["slow"] = SlowSource()
    monkeypatch.setattr(web_app, "load_sources", lambda **k: {})
    monkeypatch.setattr(web_app, "get_registry", lambda: get_registry())

    cfg = tmp_cfg
    cfg.scan.source_search_first_batch_s = 0.2
    with TestClient(web_app.create_app(cfg)) as c:
        started = _time.monotonic()
        first = c.post("/api/source-search", json={"keyword": "晴天", "limit": 5}).json()
        elapsed = _time.monotonic() - started
        assert elapsed < 1.2, elapsed
        assert [v["source"] for v in first["versions"]] == ["fast"]
        assert first["pending"] == 1 and first["token"]

        deadline = _time.monotonic() + 3
        while _time.monotonic() < deadline:
            merged = c.post("/api/source-search/results", json={"token": first["token"]}).json()
            if merged["done"]:
                break
            _time.sleep(0.1)
        assert merged["done"] is True
        assert {v["source"] for v in merged["versions"]} == {"fast", "slow"}


def test_source_search_results_unknown_token(tmp_cfg):
    with TestClient(create_app(tmp_cfg)) as c:
        r = c.post("/api/source-search/results", json={"token": "missing"})
        assert r.status_code == 404


def test_search_versions_keep_suggested_song_and_quality(tmp_cfg):
    """搜索补歌的版本必须可直接入队：suggested_song 有 id，qualities 带 track。"""
    from music_refill.web.app import _versions_payload
    from music_refill.sources.base import SourceTrack

    tracks = [SourceTrack(source="s", title="晴天", artist="周杰伦", album="",
                          ext="flac", duration_s=269, size_bytes=1,
                          download_url="https://x/1")]
    versions = _versions_payload("晴天 周杰伦", tracks)
    assert versions and versions[0]["suggested_song"]["id"] < 0
    assert versions[0]["qualities"][0]["track"]["download_url"] == "https://x/1"
