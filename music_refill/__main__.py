"""入口：`python -m music_refill serve` 启动网页 GUI（默认）；`scan` / `doctor` 为辅助。"""
from __future__ import annotations

import argparse
import logging
import sys

from .config import PROJECT_ROOT, load


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

    p_doctor = sub.add_parser("doctor", help="环境自检：依赖、浏览器、音源、数据目录")

    p_launch = sub.add_parser("launch", help="静默启动服务并打开网页（一键启动脚本使用）")
    p_launch.add_argument("--host", default=None)
    p_launch.add_argument("--port", type=int, default=None)
    p_launch.add_argument("--no-browser", action="store_true", help="只启动服务，不打开浏览器")

    sub.add_parser("stop", help="停止由 launch 启动的服务")

    args = parser.parse_args()
    cmd = args.cmd or "serve"

    if cmd == "serve":
        import uvicorn

        from .services.logging import setup_logging

        cfg = load()
        log_file = setup_logging(cfg.paths.data_dir)
        host = args.host or cfg.server.host
        port = args.port or cfg.server.port
        from .web.app import create_app

        print(f"\n  music-refill  →  http://{host}:{port}")
        print(f"  日志：{log_file}\n")
        uvicorn.run(create_app(cfg), host=host, port=port, log_level="warning")
        return 0

    if cmd == "scan":
        from .services.logging import setup_logging

        cfg = load()
        setup_logging(cfg.paths.data_dir)
        from .core import gap_finder
        from .netease.client import Client, LoginRequired

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

    if cmd == "doctor":
        cfg = load()
        problems: list[str] = []
        print("music-refill doctor")
        print(f"  Python      : {sys.version.split()[0]}")
        print(f"  项目目录    : {PROJECT_ROOT}")
        print(f"  数据目录    : {cfg.paths.data_dir}")
        print(f"  配置        : {'config.toml' if (PROJECT_ROOT / 'config.toml').exists() else '默认值（无 config.toml）'}")
        print(f"  cookie      : {'存在' if cfg.paths.cookies.exists() else '不存在（需登录）'}")
        print(f"  单批上限    : {cfg.scan.limit} 首")
        print(f"  音质上限    : {cfg.scan.quality}")
        print(f"  限速        : {cfg.scan.sleep_min}~{cfg.scan.sleep_max}s/首")
        try:
            from .netease.browser_login import find_browser

            browser = find_browser(cfg.browser.executable)
            print(f"  本机浏览器  : {browser}")
        except Exception as e:
            print(f"  本机浏览器  : 未找到（{e}）")
            problems.append("浏览器")
        try:
            from .sources import load_sources

            status = load_sources(
                dirs=cfg.sources.dirs or None,
                order=cfg.sources.order,
                workdir=cfg.paths.musicdl_workdir,
                enabled=cfg.sources.enabled,
                disabled=cfg.sources.disabled,
            )
            if not status:
                print("  音源        : 未找到可用适配器（仅能扫描，不能补歌）")
            print(f"  音源目录    : {', '.join(cfg.sources.dirs) if cfg.sources.dirs else 'sources_local（默认）'}")
            for name, st in status.items():
                print(f"  音源 {name:<8}: {'可用' if st.get('healthy') else '不可用 ' + st.get('error', '')}")
        except Exception as e:
            print(f"  音源        : 加载失败（{e}）")
            problems.append("音源")
        try:
            import musicdl  # noqa: F401
            import mutagen  # noqa: F401
            import requests  # noqa: F401
            import websocket  # noqa: F401

            print("  依赖        : ok")
        except Exception as e:
            print(f"  依赖        : 缺失（{e}）")
            problems.append("依赖")
        try:
            import fastapi  # noqa: F401
            import uvicorn  # noqa: F401
        except Exception as e:
            print(f"  Web 依赖    : 缺失（{e}）")
            problems.append("Web 依赖")
        print(f"  结论        : {'可启动（音源为空时只能扫描，不能补歌）' if not problems else '需修复：' + '、'.join(problems)}")
        return 0

    if cmd == "launch":
        from .launcher import launch

        return launch(no_browser=args.no_browser, host=args.host, port=args.port)

    if cmd == "stop":
        from .launcher import stop

        return stop()

    return 0


if __name__ == "__main__":
    sys.exit(main())
