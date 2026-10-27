#!/bin/sh
# Установка голосового перехватчика mictap.so, демона touch-assistant,
# часов таймера LED и ALSA dmix на Яндекс Станцию Мини 2 (по SSH или ADB).
set -e

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
STATION="${1:-${STATION:-192.168.2.83}}"
SSH_PORT="${2:-${STATION_SSH_PORT:-2222}}"
ASSISTANT_PORT="${ASSISTANT_PORT:-8730}"

detect_mac_ip() {
    for iface in en0 en1 en2 bridge0; do
        ip=$(ipconfig getifaddr "$iface" 2>/dev/null || true)
        if [ -n "$ip" ]; then
            printf '%s\n' "$ip"
            return 0
        fi
    done
    ifconfig 2>/dev/null | awk '/inet / && $2 != "127.0.0.1" {print $2; exit}'
}

MAC_IP="${3:-${MAC_IP:-$(detect_mac_ip)}}"
[ -n "$MAC_IP" ] || { echo "[x] Не удалось определить IP Mac. Укажите MAC_IP=192.168.x.x"; exit 1; }

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

TRANSPORT=""
if ssh -p "$SSH_PORT" -o ConnectTimeout=3 -o StrictHostKeyChecking=no "root@$STATION" "true" >/dev/null 2>&1; then
    TRANSPORT="ssh"
elif ssh -p 22 -o ConnectTimeout=3 -o StrictHostKeyChecking=no "root@$STATION" "true" >/dev/null 2>&1; then
    TRANSPORT="ssh"
    SSH_PORT=22
elif command -v adb >/dev/null 2>&1 && adb connect "$STATION:5555" >/dev/null 2>&1 && adb -s "$STATION:5555" shell true >/dev/null 2>&1; then
    TRANSPORT="adb"
else
    die "Нет подключения к станции $STATION ни по SSH (порт $SSH_PORT), ни по ADB (:5555)."
fi

say "Станция: $STATION (транспорт: $TRANSPORT) | Оркестратор Mac: http://$MAC_IP:$ASSISTANT_PORT"

remote_exec() {
    if [ "$TRANSPORT" = "ssh" ]; then
        ssh -p "$SSH_PORT" -o ConnectTimeout=5 -o StrictHostKeyChecking=no "root@$STATION" "mount -o remount,rw / >/dev/null 2>&1 || true; $*"
    else
        adb -s "$STATION:5555" shell "mount -o remount,rw / >/dev/null 2>&1 || true; $*"
    fi
}

remote_put() {
    src="$1"
    dst="$2"
    mode="${3:-644}"
    [ -f "$src" ] || die "Отсутствует локальный файл: $src"
    if [ "$TRANSPORT" = "ssh" ]; then
        cat "$src" | ssh -p "$SSH_PORT" -o ConnectTimeout=5 -o StrictHostKeyChecking=no "root@$STATION" \
            "mount -o remount,rw / >/dev/null 2>&1 || true; cat > '${dst}.tmp' && chmod $mode '${dst}.tmp' && mv -f '${dst}.tmp' '$dst'"
    else
        adb -s "$STATION:5555" push "$src" /tmp/.mictap_push >/dev/null 2>&1
        remote_exec "chmod $mode /tmp/.mictap_push && mv -f /tmp/.mictap_push '$dst'"
    fi
}

say "1/5 Подготовка директории /data/mictap на станции..."
remote_exec "mkdir -p /data/mictap /var/run"

say "2/5 Копирование бинарников (mictap.so, touch-assistant, display-tool, evdump)..."
remote_put "$ROOT/bin/mictap.so" "/data/mictap/mictap.so" 755
remote_put "$ROOT/bin/touch-assistant" "/data/mictap/touch-assistant" 755
if [ -f "$ROOT/bin/display-tool" ]; then
    remote_put "$ROOT/bin/display-tool" "/data/mictap/display-tool" 755
fi
if [ -f "$ROOT/bin/evdump" ]; then
    remote_put "$ROOT/bin/evdump" "/data/mictap/evdump" 755
fi

say "3/5 Копирование скриптов часов таймера, громкости AirPlay и звуковых сигналов..."
remote_put "$ROOT/files/mictap/timer-clock.sh" "/data/mictap/timer-clock.sh" 755
remote_put "$ROOT/files/mictap/airplay-vol.sh" "/data/mictap/airplay-vol.sh" 755
for wav in chime.wav cancel.wav error.wav vol_click.wav; do
    if [ -f "$ROOT/files/mictap/$wav" ]; then
        remote_put "$ROOT/files/mictap/$wav" "/data/mictap/$wav" 644
    fi
done

say "4/5 Установка ALSA dmix, shairport-sync.conf и автозапуска S99touch-assistant..."
remote_put "$ROOT/files/etc/alsa-mictap.conf" "/etc/alsa-mictap.conf" 644
if [ -f "$ROOT/files/etc/init.d/S99shairport" ]; then
    remote_put "$ROOT/files/etc/init.d/S99shairport" "/etc/init.d/S99shairport" 755
fi
if [ -f "$ROOT/files/etc/shairport-sync.conf" ]; then
    remote_put "$ROOT/files/etc/shairport-sync.conf" "/etc/shairport-sync.conf" 644
    remote_exec "/etc/init.d/S99shairport restart >/dev/null 2>&1 || true"
fi

TMP_INIT=$(mktemp)
cat > "$TMP_INIT" <<EOF
#!/bin/sh
DAEMON=/data/mictap/touch-assistant
PID=/var/run/touch-assistant.pid
LOG=/tmp/touch-assistant.log

case "\$1" in
  start)
    chmod +x "\$DAEMON" 2>/dev/null || true
    [ -x "\$DAEMON" ] || exit 0
    if [ -f "\$PID" ] && kill -0 "\$(cat "\$PID")" 2>/dev/null; then exit 0; fi
    (
      sleep 2
      ALSA_CONFIG_PATH=/etc/alsa-mictap.conf ASSISTANT_URL="http://$MAC_IP:$ASSISTANT_PORT" "\$DAEMON" >> "\$LOG" 2>&1 &
      echo \$! > "\$PID"
    ) &
    ;;
  stop)
    [ -f "\$PID" ] && kill "\$(cat "\$PID")" 2>/dev/null
    rm -f "\$PID"
    ;;
  restart)
    "\$0" stop
    sleep 1
    "\$0" start
    ;;
  *)
    echo "Usage: \$0 {start|stop|restart}"
    exit 1
    ;;
esac
EOF
remote_put "$TMP_INIT" "/etc/init.d/S99touch-assistant" 755
rm -f "$TMP_INIT"

say "5/5 Проверка инжекта LD_PRELOAD=/data/mictap/mictap.so в launch.json и перезапуск..."
remote_exec '
LAUNCH="/system/vendor/quasar/launch.json"
if [ -f "$LAUNCH" ] && ! grep -q "mictap.so" "$LAUNCH"; then
    cp "$LAUNCH" "${LAUNCH}.bak" 2>/dev/null || true
    sed -i "/\"--platformd\"/,/\"envs\"/ s|\"envs\": \[|\"envs\": [\n                        \"LD_PRELOAD=/data/mictap/mictap.so\",\n                        \"MICTAP_SOCKET=/tmp/vqe.sock\",|" "$LAUNCH"
    echo "launch.json пропатчен (добавлен mictap.so)"
fi
/etc/init.d/S99touch-assistant restart
'

say "Готово! Голосовой перехватчик и демон touch-assistant активны на $STATION."
