"""Голосовые уведомления с Mac на Яндекс Станцию (POST /say + авто-мониторинг скачиваний и задач)."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np

from .audio import to_wav_bytes
from .discord_tools import (
    _CF_ARRAY_TYPE,
    _HAS_AX,
    _ax,
    _ax_copy,
    _ax_str,
    _cf,
    _cfstr,
    _kCFBooleanTrue,
)

log = logging.getLogger(__name__)

STATION_HOST = os.environ.get("STATION_IP", "192.168.2.83")
STATION_PORT = os.environ.get("STATION_SSH_PORT", "2222")
_PLAY_LOCK = threading.Lock()


def _generate_notify_chime(rate: int) -> np.ndarray:
    """Короткий мягкий двухнотный сигнал уведомления (G5 -> D6, 0.42 с)."""
    notes_hz = [783.99, 1174.66]
    total_sec = 0.45
    total_samples = int(rate * total_sec)
    out = np.zeros(total_samples, dtype=np.float32)

    for idx, freq in enumerate(notes_hz):
        start_s = idx * 0.13
        start_i = int(start_s * rate)
        dur_s = total_sec - start_s
        n = int(dur_s * rate)
        if n <= 0:
            continue
        t = np.linspace(0.0, dur_s, n, endpoint=False, dtype=np.float32)
        env = np.exp(-6.5 * t) * (1.0 - np.exp(-80.0 * t))
        wave = 0.75 * np.sin(2.0 * np.pi * freq * t) + 0.25 * np.sin(2.0 * np.pi * (freq * 2.0) * t)
        out[start_i : start_i + n] += 0.28 * wave * env

    peak = float(np.max(np.abs(out)))
    if peak > 0:
        out = out / peak * 0.45
    return out.astype(np.float32)


def build_notification_wav(
    text: str,
    tts_speak_fn: Callable[[str], tuple[bytes, int]],
    with_chime: bool = True,
) -> tuple[bytes, int]:
    """Синтезирует речь и при необходимости добавляет в начало мягкий сигнал уведомления."""
    speech_wav, rate = tts_speak_fn(text)
    pcm_int16 = np.frombuffer(speech_wav[44:], dtype="<i2")
    speech_samples = pcm_int16.astype(np.float32) / 32767.0
    # Комфортная мягкая громкость речи для уведомлений (коэффициент 0.45, около -7 dB)
    speech_samples = speech_samples * 0.45
    if not with_chime:
        return to_wav_bytes(speech_samples, rate), rate

    chime = _generate_notify_chime(rate)
    pause = np.zeros(int(rate * 0.14), dtype=np.float32)
    combined = np.concatenate([chime, pause, speech_samples])
    return to_wav_bytes(combined, rate), rate


def play_wav_on_station(wav_bytes: bytes, fallback_mac: bool = False) -> bool:
    """
    Отправляет WAV на Яндекс Станцию Мини 2 (192.168.2.83:2222), освобождает ALSA от фонового
    AirPlay/mediad при необходимости и воспроизводит через aplay -N (без блокировки).
    Ограничивает аппаратную громкость Master, чтобы уведомление не орало на полной мощности.
    """
    with _PLAY_LOCK:
        remote_cmd = (
            "cur_spk=$(amixer sget 'SPK Master' 2>/dev/null | awk '/Mono: Playback/ {print $3}'); "
            "if [ \"$cur_spk\" = \"0\" ] || [ -z \"$cur_spk\" ]; then exit 0; fi; "
            "cur_master=$(amixer sget 'Master' 2>/dev/null | awk '/Mono: Playback/ {print $3}'); "
            "if [ -n \"$cur_master\" ] && [ \"$cur_master\" -gt 135 ] 2>/dev/null; then "
            "amixer sset 'Master' 135 >/dev/null 2>&1; "
            "fi; "
            "cat > /tmp/mac_notify.wav && "
            "for p in $(ps -o pid,args 2>/dev/null | awk '/aplay.*mac_notify/ && !/awk/ {print $1}'); do kill -15 \"$p\" 2>/dev/null; done; "
            "/usr/bin/aplay -N -q -D dmix_spk_plug /tmp/mac_notify.wav; "
            "if [ -n \"$cur_master\" ] && [ \"$cur_master\" -gt 135 ] 2>/dev/null; then "
            "amixer sset 'Master' \"$cur_master\" >/dev/null 2>&1; "
            "fi; "
            "rm -f /tmp/mac_notify.wav"
        )
        try:
            res = subprocess.run(
                [
                    "ssh",
                    "-p",
                    STATION_PORT,
                    "-o",
                    "ConnectTimeout=3",
                    "-o",
                    "StrictHostKeyChecking=no",
                    f"root@{STATION_HOST}",
                    remote_cmd,
                ],
                input=wav_bytes,
                capture_output=True,
                timeout=15,
            )
            if res.returncode == 0:
                return True
            log.warning(
                "Не удалось проиграть уведомление на колонке (code=%s): %s",
                res.returncode,
                res.stderr.decode("utf-8", errors="ignore").strip(),
            )
        except Exception as exc:
            log.warning("Ошибка SSH при отправке уведомления на колонку: %s", exc)

        if fallback_mac:
            try:
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                    f.write(wav_bytes)
                    tmp_path = f.name
                subprocess.run(["/usr/bin/afplay", tmp_path], check=False, timeout=30)
                Path(tmp_path).unlink(missing_ok=True)
                return True
            except Exception:
                pass
        return False


def announce_on_station_async(
    text: str,
    tts_speak_fn: Callable[[str], tuple[bytes, int]],
    with_chime: bool = True,
) -> None:
    """Асинхронно озвучивает уведомление на колонке, не блокируя вызывающий процесс."""

    def _worker() -> None:
        try:
            wav, _ = build_notification_wav(text, tts_speak_fn, with_chime=with_chime)
            log.info("Голосовое уведомление на колонку: '%s'", text)
            play_wav_on_station(wav, fallback_mac=False)
        except Exception as exc:
            log.warning("Ошибка озвучки уведомления '%s': %s", text, exc)

    threading.Thread(target=_worker, daemon=True).start()


# --- Парсинг уведомлений Google Calendar / расписания macOS ------------------

_CALENDAR_SERVICE_NAMES = {
    "notification center",
    "script editor",
    "google chrome",
    "safari",
    "chrome",
    "calendar",
    "календарь",
    "google calendar",
    "гугл календарь",
    "гугл",
    "google",
    "calendar.google.com",
    "iphone",
    "iphone mirroring",
    "mirroring",
    "дублирование iphone",
    "reminders",
    "напоминания",
}

_CALENDAR_MARKERS = {
    "calendar",
    "календар",
    "google calendar",
    "гугл календарь",
    "calendar.google.com",
    "расписани",
    "напоминани",
    "meet.google",
    "zoom",
    "встреч",
    "урок",
    "заняти",
    "вебинар",
}

_IGNORE_MARKERS = (
    "software update",
    "обновление",
    "обновлена",
    "обновлено",
    "macos",
    "система",
    "system",
    "finder",
    "app store",
    "terminal",
    "таймер",
    "timer",
    "будильник",
    "alarm",
    "секундомер",
    "stopwatch",
    "скачиван",
    "загрузк",
    "download",
    "аккумулятор",
    "battery",
    "диск",
    "disk",
)


def parse_calendar_notification(texts: list[str]) -> str | None:
    """
    Распознаёт ТОЛЬКО уведомления Google Calendar / календаря событий и формирует
    краткий текст для озвучки на колонке:
    - Предварительное: 'У вас через 30 минут English.'
    - О начале: 'У вас English.'
    """
    combined = " | ".join(texts)
    low = combined.lower()

    # Строго отсекаем системные обновления macOS, таймеры и сторонние служебные баннеры
    if any(w in low for w in _IGNORE_MARKERS):
        return None

    # Обязательно должен быть маркер календаря / Google Calendar / запланированного занятия
    if not any(w in low for w in _CALENDAR_MARKERS):
        return None

    # Ищем время события («через 30 минут», «in 30 minutes», «через 15 мин», «через 1 ч»)
    time_part: str | None = None
    m_time_ru = re.search(r"\bчерез\s+(\d+)\s*(минут[а-я]*|мин\.?|ч(?:ас[а-я]*)?)\b", low)
    if m_time_ru:
        time_part = m_time_ru.group(0).strip()
        time_part = re.sub(r"\bмин\.?\b", "минут", time_part)
    else:
        m_time_en = re.search(r"\bin\s+(\d+)\s*(mins?|minutes?|hours?)\b", low)
        if m_time_en:
            num = m_time_en.group(1)
            unit = m_time_en.group(2)
            if "hour" in unit:
                time_part = f"через {num} ч."
            else:
                time_part = f"через {num} минут"

    # Очищаем название события от сервисных названий приложений и временных меток
    content_lines: list[str] = []
    for t in texts:
        t_strip = t.strip()
        t_low = t_strip.lower()
        if t_low in _CALENDAR_SERVICE_NAMES:
            continue
        parts = [p.strip() for p in re.split(r"[,•|·\n]", t_strip) if p.strip()]
        meaningful_parts = [p for p in parts if p.lower() not in _CALENDAR_SERVICE_NAMES]
        if meaningful_parts:
            content_lines.append(" ".join(meaningful_parts))

    if not content_lines:
        return None

    raw_text = " ".join(content_lines)
    # Удаляем диапазоны времени вида 15:00 - 16:00 или 15:00
    cleaned_event = re.sub(r"\b\d{1,2}:\d{2}(?:\s*[-–—]\s*\d{1,2}:\d{2})?\b", "", raw_text)
    # Удаляем фразы времени
    cleaned_event = re.sub(
        r"\bчерез\s+\d+\s*(?:минут[а-я]*|мин\.?|час[а-я]*)\b", "", cleaned_event, flags=re.IGNORECASE
    )
    cleaned_event = re.sub(
        r"\bin\s+\d+\s*(?:mins?|minutes?|hours?)\b", "", cleaned_event, flags=re.IGNORECASE
    )
    # Удаляем слова Google Календарь
    cleaned_event = re.sub(
        r"\b(?:google|гугл)?\s*календар[ьяе]?\b", "", cleaned_event, flags=re.IGNORECASE
    )
    cleaned_event = re.sub(r"\bcalendar(?:\.google\.com)?\b", "", cleaned_event, flags=re.IGNORECASE)
    cleaned_event = re.sub(
        r"\b(?:напоминание|событие|начинается|сейчас|сегодня|запланировано|starting now|starts now|reminder)\b",
        "",
        cleaned_event,
        flags=re.IGNORECASE,
    )

    words = cleaned_event.split()
    dedup_words: list[str] = []
    for w in words:
        if not dedup_words or w.lower() != dedup_words[-1].lower():
            dedup_words.append(w)
    cleaned_event = " ".join(dedup_words).strip(" .,!?:;-•|")

    if not cleaned_event:
        cleaned_event = "событие по расписанию"

    if time_part:
        return f"У вас {time_part} {cleaned_event}."
    else:
        return f"У вас {cleaned_event}."


class MacEventsWatcher:
    """
    Лёгкий фоновый наблюдатель (~0.5 мс на проверку, нулевой оверхед по памяти):
    Следит за всплывающими напоминаниями Google Calendar через нативный Accessibility API
    NotificationCenter и озвучивает их на Яндекс Станции на комфортной громкости.
    """

    def __init__(self, tts_speak_fn: Callable[[str], tuple[bytes, int]]) -> None:
        self.tts_speak_fn = tts_speak_fn
        self._stop = threading.Event()
        self._nc_pid: int | None = None
        self._nc_pid_ts = 0.0
        self._announced_notifications: dict[str, float] = {}

    def _get_notification_center_pid(self) -> int | None:
        now = time.time()
        if self._nc_pid and (now - self._nc_pid_ts < 300.0):
            return self._nc_pid
        try:
            res = subprocess.run(
                ["pgrep", "-x", "NotificationCenter"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            pids = [int(p) for p in res.stdout.split() if p.strip().isdigit()]
            if pids:
                self._nc_pid = pids[0]
                self._nc_pid_ts = now
                return self._nc_pid
        except Exception:
            pass
        return None

    def _poll_notification_center(self) -> None:
        if not _HAS_AX:
            return
        pid = self._get_notification_center_pid()
        if not pid:
            return
        try:
            app = _ax.AXUIElementCreateApplication(pid)
            if not app:
                return
            _ax.AXUIElementSetAttributeValue(app, _cfstr("AXManualAccessibility"), _kCFBooleanTrue)
            wins = _ax_copy(app, "AXWindows")
            _cf.CFRelease(app)
            if not wins:
                return
            count = _cf.CFArrayGetCount(wins) if (_cf.CFGetTypeID(wins) == _CF_ARRAY_TYPE) else 0
            if count <= 0:
                _cf.CFRelease(wins)
                return

            alerts: list[list[str]] = []

            def find_alerts(el, depth: int = 0) -> None:
                if depth > 10:
                    return
                subrole = _ax_str(el, "AXSubrole")
                role = _ax_str(el, "AXRole")
                if (
                    subrole in ("AXNotificationCenterAlert", "AXNotificationCenterBanner")
                    or role in ("AXNotificationCenterAlert", "AXNotificationCenterBanner")
                ):
                    lines: list[str] = []

                    def get_texts(c, d: int = 0) -> None:
                        if d > 5:
                            return
                        t = _ax_str(c, "AXTitle")
                        v = _ax_str(c, "AXValue")
                        ds = _ax_str(c, "AXDescription")
                        for s in (t, v, ds):
                            s = s.strip()
                            if s and s not in lines:
                                lines.append(s)
                        ch = _ax_copy(c, "AXChildren")
                        if ch:
                            if _cf.CFGetTypeID(ch) == _CF_ARRAY_TYPE:
                                n = _cf.CFArrayGetCount(ch)
                                for k in range(n):
                                    get_texts(_cf.CFArrayGetValueAtIndex(ch, k), d + 1)
                            _cf.CFRelease(ch)

                    get_texts(el)
                    if lines:
                        alerts.append(lines)
                    return

                ch = _ax_copy(el, "AXChildren")
                if ch:
                    if _cf.CFGetTypeID(ch) == _CF_ARRAY_TYPE:
                        n = _cf.CFArrayGetCount(ch)
                        for k in range(n):
                            find_alerts(_cf.CFArrayGetValueAtIndex(ch, k), depth + 1)
                    _cf.CFRelease(ch)

            for i in range(count):
                win = _cf.CFArrayGetValueAtIndex(wins, i)
                find_alerts(win)

            _cf.CFRelease(wins)

            now = time.time()
            for lines in alerts:
                speech = parse_calendar_notification(lines)
                if not speech:
                    continue

                # Дедупликация: не повторять одно и то же уведомление в течение 300 секунд (5 минут)
                key = speech.lower().strip()
                last_time = self._announced_notifications.get(key, 0.0)
                if now - last_time < 300.0:
                    continue

                self._announced_notifications[key] = now
                log.info("Обнаружено напоминание календаря: %s -> озвучиваем: '%s'", lines, speech)
                announce_on_station_async(speech, self.tts_speak_fn, with_chime=True)

            # Очистка устаревших ключей дедупликации (> 15 минут)
            if len(self._announced_notifications) > 50:
                self._announced_notifications = {
                    k: ts for k, ts in self._announced_notifications.items() if now - ts < 900.0
                }
        except Exception as exc:
            log.debug("Ошибка проверки NotificationCenter: %s", exc)

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(1.5):
            try:
                self._poll_notification_center()
            except Exception as exc:
                log.debug("Ошибка в _poll_notification_center: %s", exc)
