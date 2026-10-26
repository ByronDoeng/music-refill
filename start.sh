#!/usr/bin/env sh
# 一键启动：首次创建虚拟环境，静默启动后端并打开网页。
set -eu

cd "$(dirname "$0")"

PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      PYTHON="$candidate"
      break
    fi
  done
fi
if [ -z "$PYTHON" ]; then
  echo "[error] 未找到 Python 3.11+，请先安装并加入 PATH。" >&2
  exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
  echo "[init] 创建虚拟环境并安装依赖…"
  "$PYTHON" -m venv .venv
  ".venv/bin/python" -m pip install --upgrade pip -q
  ".venv/bin/python" -m pip install -r requirements.txt -q
fi

exec ".venv/bin/python" -m music_refill launch "$@"
