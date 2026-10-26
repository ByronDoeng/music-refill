"""测试夹具：把数据目录指向临时目录，绝不碰真实账号数据。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def tmp_cfg(tmp_path, monkeypatch):
    from music_refill.config import load

    monkeypatch.setenv("MUSIC_REFILL_DATA_DIR", str(tmp_path / "data"))
    cfg = load()
    cfg.paths.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg
