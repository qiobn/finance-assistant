#!/usr/bin/env bash
# 一键启动脚本：创建虚拟环境、安装依赖、启动服务。
set -e
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "==> 创建虚拟环境 .venv"
  python3 -m venv .venv
fi

source .venv/bin/activate

echo "==> 安装依赖（首次较慢）"
pip install -q --upgrade pip
pip install -q -r requirements.txt

PORT="${PORT:-8777}"
echo "==> 启动服务： http://127.0.0.1:${PORT}"
exec uvicorn backend.app:app --reload --port "${PORT}"
