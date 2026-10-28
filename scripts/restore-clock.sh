#!/usr/bin/env bash
# ==============================================================================
# Скрипт мгновенного отката часов/дисплея Яндекс Станции к заводскому виду
# ==============================================================================
set -e

STATION="${1:-${STATION_IP:-192.168.2.83}}"
PORT="${2:-${STATION_SSH_PORT:-2222}}"

echo "==> Сброс часов Яндекс Станции ($STATION:$PORT) к заводскому режиму..."

ssh -p "$PORT" -o ConnectTimeout=3 -o StrictHostKeyChecking=no "root@$STATION" '
    # 1. Останавливаем любые кастомные процессы дисплея
    killall -9 display-tool 2>/dev/null || true
    rm -f /tmp/display_tool.pid
    
    # 2. Устанавливаем фабричный режим
    echo "native" > /data/mictap/display_mode
    
    # 3. Гасим сегменты, чтобы Quasar немедленно перерисовал родные часы
    if [ -x /data/mictap/display-tool ]; then
        /data/mictap/display-tool reset >/dev/null 2>&1 || true
    fi
'

echo "✓ Готово! Дисплей переведён в 100% заводской режим Quasar."
