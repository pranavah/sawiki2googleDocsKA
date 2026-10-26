#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "Python 3 was not found. Install Python 3 and its venv support." >&2
    exit 1
fi

if [[ ! -x ".venv/bin/python" ]]; then
    "$PYTHON" -m venv .venv
fi

".venv/bin/python" -m pip install -r requirements.txt
exec ".venv/bin/python" wikisource_crawler_daemon.py
