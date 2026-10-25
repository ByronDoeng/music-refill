@echo off
rem music-refill 快速启动（Windows）：自动建 venv、装依赖、起服务
cd /d "%~dp0"
if not exist .venv (
  echo [初始化] 创建虚拟环境并安装依赖...
  python -m venv .venv || goto :err
  .venv\Scripts\python -m pip install --upgrade pip -q || goto :err
  .venv\Scripts\python -m pip install -r requirements.txt -q || goto :err
)
start "" http://127.0.0.1:18123
.venv\Scripts\python -m music_refill serve
goto :eof
:err
echo [错误] 初始化失败，请确认已安装 Python 3.11+ 并在 PATH 中
pause
