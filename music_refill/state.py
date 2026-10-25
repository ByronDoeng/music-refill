"""SQLite 状态库：扫描历史与缺口歌曲（M1），后续扩展上传结果/命中源。"""
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
"""


class State:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # FastAPI 同步端点跑在线程池：允许跨线程使用，写操作用锁串行化
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

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
    ) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO uploads(scan_id, song_id, status, route, quality, detail, ts)"
                " VALUES(?,?,?,?,?,?,?)",
                (scan_id, song_id, status, route, quality, detail, time.strftime("%Y-%m-%d %H:%M:%S")),
            )
            self.conn.commit()
