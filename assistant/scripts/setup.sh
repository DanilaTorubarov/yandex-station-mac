#!/bin/sh
# Ставит окружение и скачивает голос Piper. Нужен uv (https://docs.astral.sh/uv/).
set -eu
cd "$(dirname "$0")/.."

if ! command -v uv >/dev/null 2>&1; then
    echo "нужен uv: brew install uv" >&2
    exit 1
fi

echo "==> Python 3.12 + зависимости"
uv python install 3.12
uv sync

echo "==> Голос Piper"
sh scripts/fetch-voice.sh

echo
echo "Готово. Дальше:"
echo "  cp .env.example .env      # при желании поправить модели/пороги"
echo "  sh scripts/run.sh         # поднять оркестратор на 127.0.0.1:8730"
echo "  sh scripts/smoke.sh       # проверить ASR -> LLM -> TTS без HTTP"
