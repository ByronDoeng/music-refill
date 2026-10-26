from __future__ import annotations

from music_refill.state import State


def test_migration_and_idempotent_helpers(tmp_path):
    st = State(tmp_path / "x.db")
    st.save_scan({"a": 1}, 3, [])
    st.save_upload(
        1, 100, "ok", route="download", quality="flac",
        detail="cloud:1 matched=False verified=True playable=None",
        song={"name": "晴天", "artists": ["周杰伦"], "album": "叶惠美"},
        track={"source": "kuwo", "title": "晴天", "artist": "周杰伦", "size_bytes": 123, "duration_s": 269},
        attempt=1,
    )
    st.save_upload(1, 100, "failed", detail="timeout", song={"name": "晴天"})
    latest = st.latest_upload_status()
    assert latest[100] == "failed"  # 后写覆盖前写
    summary = st.upload_summary()
    assert summary["attempts"] == 2
    assert summary["ok_songs"] == 1
    recents = st.recent_uploads(10)
    assert recents[0]["song_name"] == "晴天"


def test_jobs_queue_survives_reset(tmp_path):
    st = State(tmp_path / "x.db")
    item = {
        "song": {"id": 1, "name": "A", "artists": ["B"], "album": "C"},
        "track": {"source": "kuwo", "title": "A", "artist": "B", "ext": "mp3", "size_bytes": 1},
    }
    ids = st.enqueue([item])
    assert len(ids) == 1
    jobs = st.queued_jobs()
    assert jobs and jobs[0]["payload_json"]
    st.update_job(ids[0], status="running")
    assert st.reset_running_jobs() == 1
    assert st.queued_jobs()[0]["id"] == ids[0]
