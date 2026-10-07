#!/usr/bin/env bash
# StockQAbyLLM 统一检查入口的 shell 转发层。
#
# 本文件只做三件事：定位仓根、原样转发全部参数、原样返回退出码。
# 所有检查步骤的定义都在 scripts/checks.py（唯一 Python 定义点）。
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd)"

if [ -n "${PYTHON:-}" ]; then
    PYTHON_BIN="${PYTHON}"
elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
else
    PYTHON_BIN="python3"
fi

exec "${PYTHON_BIN}" -B "${REPO_ROOT}/scripts/checks.py" "$@"
