#!/usr/bin/env bash
# 本地开发启动（不使用 Docker 时）
set -euo pipefail
cd "$(dirname "$0")"
export EVAC_DB="${EVAC_DB:-$(pwd)/data/evac.db}"
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --reload
