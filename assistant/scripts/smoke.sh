#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
    echo "нет окружения: сначала sh scripts/setup.sh" >&2
    exit 1
fi
exec .venv/bin/python scripts/smoke.py "$@"
