#!/bin/sh
# Поднимает оркестратор. Слушает 0.0.0.0:8730 для доступа со станции.
set -eu
cd "$(dirname "$0")/.."

if [ -f .env ]; then
    # Экспортируем переменные из .env
    set -a
    . ./.env
    set +a
fi

if [ ! -x .venv/bin/uvicorn ]; then
    echo "нет окружения: сначала sh scripts/setup.sh" >&2
    exit 1
fi

exec .venv/bin/uvicorn app.server:app \
    --host "${ASSISTANT_HOST:-0.0.0.0}" \
    --port "${ASSISTANT_PORT:-8730}"
