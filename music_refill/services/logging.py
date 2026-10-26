"""统一日志：控制台 + data/logs/music_refill.log（自动轮转）。

日志里绝不写入 cookie 原文；异常只保留前 300 字，避免把请求头带进文件。
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(data_dir: Path, level: int = logging.INFO) -> Path:
    """初始化根日志；重复调用是幂等的（便于测试和 uvicorn reload）。"""
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "music_refill.log"

    root = logging.getLogger()
    root.setLevel(level)
    if any(getattr(h, "_music_refill", False) for h in root.handlers):
        return log_file

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(
        log_file, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler._music_refill = True  # type: ignore[attr-defined]
    root.addHandler(file_handler)

    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
               for h in root.handlers):
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        console._music_refill = True  # type: ignore[attr-defined]
        root.addHandler(console)
    return log_file
