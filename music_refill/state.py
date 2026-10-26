"""SQLite 状态库：扫描历史、缺口歌曲、上传结果与任务队列。

表结构升级用 `_migrate()` 里的 `ALTER TABLE ... ADD COLUMN` 完成，
老版本 `data/music_refill.db` 可直接继续使用，不需要手工删库。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    params_json TEXT NOT NULL,
    total INTEGER NOT NULL,
    gap_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS gap_songs (
    scan_id INTEGER NOT NULL REFERENCES scans(id),
    song_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    artists TEXT NOT NULL,
    album TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    fee INTEGER NOT NULL,
    reason TEXT NOT NULL,
    playlists TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS uploads (
    scan_id INTEGER NOT NULL,
    song_id INTEGER NOT NULL,
    status TEXT NOT NULL,          -- ok | unmatched | not_found | failed | skipped
    route TEXT NOT NULL DEFAULT '',-- instant | download | ''
    quality TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,             -- download | upload | verify
    song_id INTEGER NOT NULL,
    song_name TEXT NOT NULL DEFAULT '',
    artist TEXT NOT NULL DEFAULT '',
    album TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    source_title TEXT NOT NULL DEFAULT '',
    source_artist TEXT NOT NULL DEFAULT '',
    ext TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,           -- queued | running | ok | failed | skipped
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    cloud_song_id INTEGER NOT NULL DEFAULT 0,
    created_ts TEXT NOT NULL,
    updated_ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
"""

# 旧库补列：全部是“可空或带默认值”的追加列，迁移幂等。
MIGRATIONS = {
    "uploads": {
        "song_name": "TEXT NOT NULL DEFAULT ''",
        "artist": "TEXT NOT NULL DEFAULT ''",
        "album": "TEXT NOT NULL DEFAULT ''",
        "source": "TEXT NOT NULL DEFAULT ''",
        "source_title": "TEXT NOT NULL DEFAULT ''",
        "source_artist": "TEXT NOT NULL DEFAULT ''",
        "size_bytes": "INTEGER NOT NULL DEFAULT 0",
        "duration_s": "INTEGER NOT NULL DEFAULT 0",
        "attempt": "INTEGER NOT NULL DEFAULT 1",
    },
    "jobs": {
        "payload_json": "TEXT NOT NULL DEFAULT ''",
    },
}


class State:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # FastAPI 同步端点跑在线程池：允许跨线程使用，写操作用锁串行化
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self.conn.executescript(SCHEMA)
            self._migrate()
            self.conn.commit()

    def _migrate(self) -> None:
        for table, columns in MIGRATIONS.items():
            existing = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, ddl in columns.items():
                if name not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    def save_scan(self, params: dict, total: int, gap: list[dict]) -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO scans(ts, params_json, total, gap_count) VALUES(?,?,?,?)",
                (time.strftime("%Y-%m-%d %H:%M:%S"), json.dumps(params, ensure_ascii=False), total, len(gap)),
            )
            scan_id = cur.lastrowid
            self.conn.executemany(
                "INSERT INTO gap_songs(scan_id, song_id, name, artists, album, duration_ms, fee, reason, playlists)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    (
                        scan_id, g["id"], g["name"] or "?",
                        "、".join(str(x) for x in g["artists"] or []),
                        g["album"] or "", g["duration_ms"] or 0,
                        g["fee"] or 0, g["reason"] or "",
                        "、".join(str(x) for x in g.get("playlists") or []),
                    )
                    for g in gap
                ],
            )
            self.conn.commit()
            return scan_id

    def save_upload(
        self, scan_id: int, song_id: int, status: str,
        route: str = "", quality: str = "", detail: str = "",
        song: dict | None = None, track: dict | None = None, attempt: int = 1,
    ) -> None:
        song = song or {}
        track = track or {}
        with self._lock:
            self.conn.execute(
                "INSERT INTO uploads(scan_id, song_id, status, route, quality, detail, ts,"
                " song_name, artist, album, source, source_title, source_artist, size_bytes, duration_s, attempt)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    scan_id, song_id, status, route, quality, detail,
                    time.strftime("%Y-%m-%d %H:%M:%S"),
                    song.get("name") or "", "、".join(song.get("artists") or []),
                    song.get("album") or "", track.get("source") or "",
                    track.get("title") or "", track.get("artist") or "",
                    int(track.get("size_bytes") or 0), int(track.get("duration_s") or 0), attempt,
                ),
            )
            self.conn.commit()

    def recent_uploads(self, limit: int = 20) -> list[dict]:
        """最近上传记录，供“已处理/可跳过”与交接排障使用。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT scan_id, song_id, song_name, artist, status, route, quality,"
                " detail, ts, source, source_title, attempt FROM uploads"
                " ORDER BY rowid DESC LIMIT ?",
                (max(1, min(limit, 200)),),
            ).fetchall()
        return [dict(r) for r in rows]

    def upload_summary(self) -> dict:
        """按“歌曲去重”统计成功/失败，避免重复重试把成功率算高。"""
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS attempts,"
                " COUNT(DISTINCT CASE WHEN status='ok' THEN song_id END) AS ok_songs,"
                " COUNT(DISTINCT CASE WHEN status='failed' THEN song_id END) AS failed_songs,"
                " SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok_attempts"
                " FROM uploads"
            ).fetchone()
        return dict(row) if row else {}

    def recent_jobs(self, limit: int = 20) -> list[dict]:
        """最近任务（含失败原因），交接排障先看这里。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, kind, song_id, song_name, artist, source, source_title,"
                " status, attempts, last_error, cloud_song_id, updated_ts"
                " FROM jobs ORDER BY id DESC LIMIT ?",
                (max(1, min(limit, 200)),),
            ).fetchall()
        return [dict(r) for r in rows]

    def job_by_id(self, job_id: int) -> dict | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM jobs WHERE id=?", (int(job_id),)).fetchone()
        return dict(row) if row else None

    def latest_upload_status(self) -> dict[int, str]:
        """每首歌最近一次上传状态（后写覆盖前写）。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT song_id, status FROM uploads ORDER BY rowid ASC"
            ).fetchall()
        return {int(r["song_id"]): r["status"] for r in rows}

    # ---------- 队列（跨重启保留） ----------

    def enqueue(self, items: list[dict]) -> list[int]:
        """写入任务队列，返回 job id 列表（顺序与 items 一致）。"""
        import json as _json

        now = time.strftime("%Y-%m-%d %H:%M:%S")
        ids: list[int] = []
        with self._lock:
            for i in items:
                cur = self.conn.execute(
                    "INSERT INTO jobs(kind, song_id, song_name, artist, album, source, source_title,"
                    " source_artist, ext, size_bytes, payload_json, status, created_ts, updated_ts)"
                    " VALUES('download',?,?,?,?,?,?,?,?,?,?, 'queued', ?, ?)",
                    (
                        int(i["song"]["id"]), i["song"].get("name") or "",
                        "、".join(i["song"].get("artists") or []), i["song"].get("album") or "",
                        i["track"].get("source") or "", i["track"].get("title") or "",
                        i["track"].get("artist") or "", i["track"].get("ext") or "",
                        int(i["track"].get("size_bytes") or 0),
                        _json.dumps(i, ensure_ascii=False), now, now,
                    ),
                )
                ids.append(int(cur.lastrowid))
            self.conn.commit()
        return ids

    def requeue_failed(self, limit: int = 50) -> list[dict]:
        """把失败任务重新置为 queued，返回可恢复的 payload。

        只在用户显式点击“重试失败任务”时调用；不会自动无限重跑。
        """
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM jobs WHERE status='failed' ORDER BY id DESC LIMIT ?",
                (max(1, min(limit, 200)),),
            ).fetchall()
            ids = [int(r["id"]) for r in rows]
            if ids:
                marks = ",".join("?" for _ in ids)
                self.conn.execute(
                    f"UPDATE jobs SET status='queued', last_error='', updated_ts=? WHERE id IN ({marks})",
                    (time.strftime("%Y-%m-%d %H:%M:%S"), *ids),
                )
                self.conn.commit()
        return [dict(r) for r in rows]

    def queued_jobs(self) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM jobs WHERE status='queued' ORDER BY id ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def update_job(self, job_id: int, **fields) -> None:
        if not fields:
            return
        fields["updated_ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self.conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*fields.values(), job_id))
            self.conn.commit()

    def reset_running_jobs(self) -> int:
        """服务重启后把遗留 running 重置为 queued，实现断点续跑。"""
        with self._lock:
            cur = self.conn.execute(
                "UPDATE jobs SET status='queued', updated_ts=? WHERE status='running'",
                (time.strftime("%Y-%m-%d %H:%M:%S"),),
            )
            self.conn.commit()
            return cur.rowcount

    def mark_job(self, job_id: int, status: str, last_error: str = "", cloud_song_id: int = 0) -> None:
        self.update_job(job_id, status=status, last_error=last_error, cloud_song_id=cloud_song_id)
