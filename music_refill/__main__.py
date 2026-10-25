"""入口：`python -m music_refill serve` 启动网页 GUI（默认）；`scan` 为命令行辅助。"""
from __future__ import annotations

import argparse
import logging
import sys

from .config import load


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="music-refill", description="网易云云盘自动补歌")
    sub = parser.add_subparsers(dest="cmd")

    p_serve = sub.add_parser("serve", help="启动网页 GUI（默认命令）")
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=int, default=None)

    p_scan = sub.add_parser("scan", help="命令行扫描缺口（辅助调试，输出 markdown）")
    p_scan.add_argument("--playlists", type=int, nargs="*", default=None)
    p_scan.add_argument("--no-liked", action="store_true", help="不含我喜欢的音乐")

    args = parser.parse_args()
    cmd = args.cmd or "serve"

    if cmd == "serve":
        import uvicorn

        cfg = load()
        host = args.host or cfg.server.host
        port = args.port or cfg.server.port
        from .web.app import create_app

        print(f"\n  music-refill  →  http://{host}:{port}\n")
        uvicorn.run(create_app(cfg), host=host, port=port, log_level="warning")
        return 0

    if cmd == "scan":
        from .core import gap_finder
        from .netease.client import Client, LoginRequired

        cfg = load()
        client = Client(cfg.paths.cookies)
        try:
            result = gap_finder.scan(
                client,
                args.playlists if args.playlists is not None else cfg.scan.playlist_ids,
                include_liked=not args.no_liked and cfg.scan.include_liked,
            )
        except LoginRequired as e:
            print(f"[未登录] {e} —— 请先 `python -m music_refill serve` 扫码登录")
            return 1
        print(gap_finder.to_markdown(result))
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
