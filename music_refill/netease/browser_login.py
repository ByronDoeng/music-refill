"""用本机已安装的 Chrome / Edge 完成一键登录（不依赖 Playwright）。

做法：以独立用户目录 + 远程调试端口启动本机浏览器，打开网易云官网；
用户在窗口里登录后，通过 Chrome DevTools Protocol 读取 Cookie。

为什么不用 Playwright：
- Playwright 需要额外下载数百 MB 的浏览器内核；
- 本项目只是“打开官网、等登录、读 cookie”，本机浏览器 + CDP 足够。

安全边界：
- 独立 profile 放在 data/browser_profile/，不读写用户日常浏览器数据；
- 只读取 music.163.com 域名下的 cookie，且只把登录所需字段写入 data/cookies.json；
- 调试端口只监听 127.0.0.1。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

BROWSER_CANDIDATES = [
    # Windows
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
    # macOS
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    # Linux
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/microsoft-edge",
    "/usr/bin/microsoft-edge-stable",
]


class BrowserLoginError(RuntimeError):
    pass


def find_browser(configured: str = "") -> Path:
    """返回可执行文件路径；显式配置优先，其次 PATH，最后常见安装位置。"""
    if configured:
        p = Path(configured).expanduser()
        if p.is_file():
            return p
        raise BrowserLoginError(f"config.toml [browser].executable 指向的文件不存在：{p}")

    for name in ("chrome", "google-chrome", "google-chrome-stable", "chromium",
                 "chromium-browser", "msedge", "microsoft-edge", "microsoft-edge-stable"):
        found = shutil.which(name)
        if found:
            return Path(found)

    for raw in BROWSER_CANDIDATES:
        p = Path(raw)
        if p.is_file():
            return p
    raise BrowserLoginError(
        "未找到本机 Chrome / Edge / Chromium。请安装其一，"
        "或修改 config.toml 的 [browser].executable 指向浏览器可执行文件。"
    )


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _is_cdp_endpoint(port: int) -> bool:
    """端口开着不代表是浏览器：必须能返回 CDP /json/version。"""
    try:
        data = _json(f"http://127.0.0.1:{port}/json/version", timeout=1.5)
    except Exception:
        return False
    return bool(data.get("webSocketDebuggerUrl") and data.get("Browser"))


def _wait_debug_port(port: int, timeout_s: float = 20) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if _is_cdp_endpoint(port):
            return True
        time.sleep(0.3)
    return False


def _json(url: str, timeout: float = 5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def _pick_cookies(raw: list[dict]) -> dict[str, str]:
    return {
        c["name"]: c["value"]
        for c in raw
        if c.get("name") and c.get("domain", "").endswith("163.com") and c.get("value")
    }


def login_via_local_browser(
    cookie_path: Path,
    profile_dir: Path,
    executable: str = "",
    debug_port: int = 9222,
    timeout_s: int = 300,
    start_url: str = "https://music.163.com/",
) -> dict:
    """启动本机浏览器并等待登录，成功后写入 cookies.json。返回 {ok, browser}。"""
    browser = find_browser(executable)
    profile_dir.mkdir(parents=True, exist_ok=True)

    # 复用已经打开的调试实例；端口被别的程序占用时换一个端口。
    # 注意：只探测端口会被任意本地服务误判，必须以 /json/version 为准。
    port = debug_port
    if not _is_cdp_endpoint(port):
        for candidate in range(debug_port, debug_port + 20):
            # 先认领可用的 CDP 实例（例如上次登录窗口还开着），再找空闲端口。
            if _is_cdp_endpoint(candidate) or not _port_open(candidate):
                port = candidate
                break
        else:
            raise BrowserLoginError(
                f"{debug_port}~{debug_port + 19} 端口均被占用，请修改 [browser].debug_port"
            )

    proc: subprocess.Popen | None = None
    if not _is_cdp_endpoint(port):
        args = [
            str(browser),
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            start_url,
        ]
        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "DETACHED_PROCESS", 0)
        log.info("launching local browser: %s (debug port %s)", browser, port)
        proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        if not _wait_debug_port(port):
            raise BrowserLoginError(f"浏览器调试端口 {port} 未就绪，请确认浏览器可正常启动")

    try:
        deadline = time.time() + timeout_s
        last_reason = ""
        while time.time() < deadline:
            try:
                targets = _json(f"http://127.0.0.1:{port}/json/list")
            except Exception as e:  # 浏览器刚启动时偶发
                last_reason = str(e)
                time.sleep(1)
                continue
            for t in targets:
                ws = t.get("webSocketDebuggerUrl")
                if not ws or "music.163.com" not in (t.get("url") or ""):
                    continue
                cookies = _cookies_from_cdp(ws)
                picked = _pick_cookies(cookies)
                if picked.get("MUSIC_U"):
                    cookie_path.parent.mkdir(parents=True, exist_ok=True)
                    cookie_path.write_text(
                        json.dumps(picked, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    return {"ok": True, "browser": browser.name, "port": port}
            time.sleep(2)
        raise BrowserLoginError(
            f"等待登录超时（{timeout_s} 秒）"
            + (f"：{last_reason[:120]}" if last_reason else "")
        )
    finally:
        # 只关闭我们自己启动的实例；用户手动打开的调试浏览器交给用户处理。
        # Windows 下 Chrome 会拉起子进程，terminate 主进程通常足够；
        # 若没退出，日志提示用户手动关闭，避免误杀日常浏览器。
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                log.warning("browser login window did not exit; close it manually")


def _cookies_from_cdp(ws_url: str) -> list[dict]:
    """最小 CDP 客户端：仅发送 Network.getAllCookies 并读回结果。"""
    try:
        from websocket import create_connection  # websocket-client，musicdl 已间接依赖
    except ImportError as e:  # pragma: no cover - 依赖缺失时的清晰报错
        raise BrowserLoginError("缺少 websocket-client，请执行 pip install -r requirements.txt") from e

    ws = create_connection(ws_url, timeout=10)
    try:
        ws.send(json.dumps({"id": 1, "method": "Network.getAllCookies"}))
        deadline = time.time() + 10
        while time.time() < deadline:
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                if "error" in msg:
                    raise BrowserLoginError(f"读取 Cookie 失败：{msg['error']}")
                return msg.get("result", {}).get("cookies", [])
        raise BrowserLoginError("读取 Cookie 超时")
    finally:
        ws.close()


# 兼容旧调用名（web/app.py 与文档里的 login_via_browser）
login_via_browser = login_via_local_browser
