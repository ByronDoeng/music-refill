#!/usr/bin/env sh
# 停止由 start.sh / launch 启动的后台服务。
set -eu

cd "$(dirname "$0")"
if [ ! -x ".venv/bin/python" ]; then
  echo "[error] 未找到 .venv，无法判断服务状态。" >&2
  exit 1
fi
exec ".venv/bin/python" -m music_refill stop
