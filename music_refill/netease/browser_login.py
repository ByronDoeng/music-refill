"""浏览器一键登录：弹出真实 Chromium 打开网易官网，用户在官方页面内登录
（扫码/密码均可），检测到登录态后自动抓取 cookie 存盘。

原理：8821 风控针对"第三方接口的扫码轮询"，不针对真实浏览器会话——
在官方页面里登录就是普通用户行为，不触发拦截。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ..config import PROJECT_ROOT

# 浏览器内核放项目目录（.gitignore），不写系统盘
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(PROJECT_ROOT / ".pw-browsers"))


def login_via_browser(cookie_path: Path, timeout_s: int = 300, headless: bool = False) -> dict:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            ctx = browser.new_context(locale="zh-CN")
            page = ctx.new_page()
            page.goto("https://music.163.com/", wait_until="domcontentloaded")
            # 尝试点开登录页；选择器失效也无妨，用户可以自己在页面里点"登录"
            for sel in ("#link-login", "text=登录"):
                try:
                    page.click(sel, timeout=2000)
                    break
                except Exception:
                    continue
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                cookies = ctx.cookies("https://music.163.com")
                if any(c["name"] == "MUSIC_U" and c["value"] for c in cookies):
                    picked = {
                        c["name"]: c["value"]
                        for c in cookies
                        if c["domain"].endswith("163.com")
                    }
                    cookie_path.parent.mkdir(parents=True, exist_ok=True)
                    cookie_path.write_text(
                        json.dumps(picked, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    return {"ok": True}
                time.sleep(2)
            return {
                "ok": False,
                "reason": f"等待登录超时（{timeout_s} 秒）——请在弹出窗口中完成登录后重试",
            }
        finally:
            browser.close()
