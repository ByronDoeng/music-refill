"""一键启动/停止本地 Web 服务。

设计目标：
- `launch` 把 uvicorn 作为独立进程启动，不占用调用方终端；
- 等服务真正可访问后再打开浏览器，避免“页面打不开”；
- 重复执行只重新打开页面，不会重复起服务；
- 记录 PID 到 `data/serve.pid`，供 `stop` 使用。

注意：这里只负责进程编排，业务逻辑仍在 `web/app.py`。
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

from .config import Config, PROJECT_ROOT, load

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def service_url(cfg: Config, host: str | None = None, port: int | None = None) -> str:
    """生成浏览器访问地址；监听 0.0.0.0/:: 时本机访问仍用 127.0.0.1。"""
    host = (host or cfg.server.host or "127.0.0.1").strip()
    if host in {"0.0.0.0", "::", "[::]"}:
        host = "127.0.0.1"
    return f"http://{host}:{int(port or cfg.server.port)}"


def _pid_path(cfg: Config) -> Path:
    return cfg.paths.data_dir / "serve.pid"


def _pid_info(cfg: Config) -> dict:
    try:
        raw = json.loads(_pid_path(cfg).read_text(encoding="utf-8"))
    except Exception:
        return {}
    try:
        return {"pid": int(raw["pid"]), "url": str(raw.get("url") or "")}
    except (KeyError, TypeError, ValueError):
        return {}


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            return f'"{pid}"' in out
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _health(url: str) -> dict | None:
    """只把本项目的 /api/status 视为健康，避免误判端口上的其他服务。"""
    try:
        with _OPENER.open(f"{url}/api/status", timeout=1.5) as resp:
            if resp.status != 200:
                return None
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    if isinstance(data, dict) and "logged_in" in data and "config" in data:
        return data
    return None


def _pythonw() -> Path:
    """优先用 pythonw.exe，避免后台服务弹出控制台窗口。"""
    exe = Path(sys.executable)
    if os.name == "nt":
        candidate = exe.with_name("pythonw.exe")
        if candidate.exists():
            return candidate
    return exe


def _spawn_args(host: str | None, port: int | None) -> list[str]:
    # -u：后台重定向到日志文件时禁用缓冲，失败时日志能立刻看到
    args = [str(_pythonw()), "-u", "-m", "music_refill", "serve"]
    if host:
        args += ["--host", host]
    if port:
        args += ["--port", str(port)]
    return args


def _write_pid(cfg: Config, pid: int, url: str) -> None:
    cfg.paths.data_dir.mkdir(parents=True, exist_ok=True)
    _pid_path(cfg).write_text(
        json.dumps({
            "pid": pid,
            "url": url,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _remove_pid(cfg: Config) -> None:
    _pid_path(cfg).unlink(missing_ok=True)


def _tail(path: Path, lines: int = 30) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def _terminate(pid: int, timeout_s: float = 6.0) -> bool:
    """先请求退出，超时后强制结束；只对记录的 PID 操作。"""
    if not _pid_alive(pid):
        return True
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.25)
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True, timeout=10,
        )
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    time.sleep(0.5)
    return not _pid_alive(pid)


def launch(no_browser: bool = False, host: str | None = None, port: int | None = None) -> int:
    """静默启动服务并打开网页。已在运行时只打开网页。"""
    cfg = load()
    url = service_url(cfg, host, port)
    existing = _health(url)
    if existing is not None:
        print(f"服务已在运行：{url}")
        if not no_browser:
            webbrowser.open(url)
        return 0

    info = _pid_info(cfg)
    if info and _pid_alive(info["pid"]):
        # 进程已存在但可能仍在启动，给它一个短暂窗口，避免重复起第二个实例。
        print(f"检测到启动中的服务（PID {info['pid']}），等待就绪…")
        deadline = time.time() + 20
        while time.time() < deadline:
            if _health(url) is not None:
                print(f"服务已就绪：{url}")
                if not no_browser:
                    webbrowser.open(url)
                return 0
            time.sleep(0.4)
        print("已有进程未通过健康检查；请先运行 stop.bat 或检查日志。", file=sys.stderr)
        return 1

    log_dir = cfg.paths.data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    serve_log = log_dir / "serve.log"
    cmd = _spawn_args(host, port)
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")

    log_fh = serve_log.open("ab")
    kwargs: dict = {
        "cwd": str(PROJECT_ROOT),
        "stdin": subprocess.DEVNULL,
        # 启动参数写进日志；服务自身日志仍在 music_refill.log
        "stdout": log_fh,
        "stderr": subprocess.STDOUT,
        "env": env,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        kwargs["start_new_session"] = True

    try:
        log_fh.write(
            f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] launch: {' '.join(cmd)}\n".encode("utf-8")
        )
        log_fh.flush()
        proc = subprocess.Popen(cmd, **kwargs)
    finally:
        log_fh.close()

    _write_pid(cfg, proc.pid, url)
    print(f"服务启动中（PID {proc.pid}）：{url}")

    deadline = time.time() + 30
    last_error = ""
    while time.time() < deadline:
        if proc.poll() is not None:
            last_error = f"服务进程提前退出（code {proc.returncode}）"
            break
        if _health(url) is not None:
            print(f"已启动：{url}")
            if not no_browser:
                opened = webbrowser.open(url)
                if not opened:
                    print(f"浏览器未自动打开，请手动访问：{url}", file=sys.stderr)
            return 0
        time.sleep(0.4)

    _remove_pid(cfg)
    if proc.poll() is None:
        _terminate(proc.pid)
    print(f"启动失败：{last_error or '等待服务就绪超时'}", file=sys.stderr)
    tail = _tail(serve_log)
    if tail:
        print("最近日志：", file=sys.stderr)
        print(tail, file=sys.stderr)
    return 1


def stop() -> int:
    """停止由 launch 启动并记录 PID 的服务。"""
    cfg = load()
    info = _pid_info(cfg)
    if not info:
        print("服务未运行（没有 PID 记录）。")
        return 0
    pid = int(info["pid"])
    if not _pid_alive(pid):
        _remove_pid(cfg)
        print(f"服务已不在运行（PID {pid}），已清理记录。")
        return 0
    print(f"正在停止服务（PID {pid}）…")
    if _terminate(pid):
        _remove_pid(cfg)
        print("已停止。")
        return 0
    print("停止失败，请用任务管理器结束该 PID。", file=sys.stderr)
    return 1
