#!/bin/sh
# Скачивает русский голос Piper (irina, medium ~63 МБ).
set -eu
cd "$(dirname "$0")/.."
mkdir -p voices

# Имя вида ru_RU-irina-medium: локаль-голос-качество.
NAME="${PIPER_VOICE_NAME:-ru_RU-irina-medium}"
LOCALE="${NAME%%-*}"
REST="${NAME#*-}"
SPEAKER="${REST%%-*}"
QUALITY="${REST#*-}"
LANG="${LOCALE%%_*}"
BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main/$LANG/$LOCALE/$SPEAKER/$QUALITY"

if [ ! -f "voices/$NAME.onnx" ]; then
    echo "==> voices/$NAME.onnx"
    curl -fL --progress-bar -o "voices/$NAME.onnx.part" "$BASE/$NAME.onnx"
    mv "voices/$NAME.onnx.part" "voices/$NAME.onnx"
fi

if [ ! -f "voices/$NAME.onnx.json" ]; then
    echo "==> voices/$NAME.onnx.json"
    curl -fL --progress-bar -o "voices/$NAME.onnx.json" "$BASE/$NAME.onnx.json"
fi

ls -lh "voices/$NAME.onnx"
