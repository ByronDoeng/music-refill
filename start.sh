#!/usr/bin/env bash
# music-refill 快速启动（Linux/macOS）：自动建 venv、装依赖、起服务
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "[初始化] 创建虚拟环境并安装依赖..."
  python3 -m venv .venv || exit 1
  ./.venv/bin/pip install --upgrade pip -q
  ./.venv/bin/pip install -r requirements.txt -q
fi
echo "  打开 http://127.0.0.1:18123"
exec ./.venv/bin/python -m music_refill serve
