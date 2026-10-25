#!/usr/bin/env bash
# board.sh —— bspctl 的 bash 包装，方便在 Linux/WSL 里直接敲 ./board.sh
#
# 用法:
#   ./board.sh log --until "login:" --timeout 90
#   ./board.sh cmd "cat /proc/device-tree/model"
#   ./board.sh up
#   ./board.sh oops logs/boot-xxx.log
#
# 二进制名保持 "board.sh" 是为了让 AI 容易记住：任何碰板子的动作都走这一个入口。
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PY="${BSPCTL_PYTHON:-}"
if [[ -z "$PY" ]]; then
    for cand in python3 python; do
        if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
    done
fi
if [[ -z "$PY" ]]; then
    echo "找不到 python3。装一个，或用 BSPCTL_PYTHON=/path/to/python 指定。" >&2
    exit 1
fi

# 默认用脚本同目录的 bsp.conf
if [[ -z "${BSPCTL_CONF:-}" && -f "$DIR/bsp.conf" ]]; then
    export BSPCTL_CONF="$DIR/bsp.conf"
fi

exec "$PY" "$DIR/bspctl.py" "$@"
