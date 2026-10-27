"""Голосовые таймеры, будильники и напоминания с озвучкой и мелодией на Яндекс Станции и Mac."""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from typing import Callable

import numpy as np

from .audio import to_wav_bytes

log = logging.getLogger(__name__)

STATION_HOST = os.environ.get("STATION_IP", "192.168.2.83")
STATION_PORT = os.environ.get("STATION_SSH_PORT", "2222")


@dataclasses.dataclass
class ScheduledItem:
    id: int
    kind: str  # "timer" | "reminder" | "alarm"
    label: str
    fire_at: float
    duration_sec: float
    timer_obj: threading.Timer
    cancelled: bool = False


_LOCK = threading.Lock()
_NEXT_ID = 1
_ACTIVE_ITEMS: dict[int, ScheduledItem] = {}

# Числительные для распознавания длительности («на десять минут», «через полчаса», «на две минуты»)
_WORD_TO_NUM: dict[str, int] = {
    "одну": 1,
    "один": 1,
    "одна": 1,
    "полторы": 1,  # обрабатывается отдельно ниже
    "полтора": 1,
    "две": 2,
    "два": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
    "одиннадцать": 11,
    "двенадцать": 12,
    "тринадцать": 13,
    "четырнадцать": 14,
    "пятнадцать": 15,
    "двадцать": 20,
    "двадцать пять": 25,
    "тридцать": 30,
    "сорок": 40,
    "сорок пять": 45,
    "пятьдесят": 50,
    "шестьдесят": 60,
    "девяносто": 90,
}


def _generate_alarm_chime(rate: int) -> np.ndarray:
    """Генерирует приятную переливчатую мелодию колокольчиков (аккорд Cmaj9) длительностью 1.8 с."""
    notes_hz = [523.25, 659.25, 783.99, 987.77, 1174.66, 1567.98]  # C5, E5, G5, B5, D6, G6
    total_sec = 1.85
    total_samples = int(rate * total_sec)
    out = np.zeros(total_samples, dtype=np.float32)

    for idx, freq in enumerate(notes_hz):
        start_s = idx * 0.16
        start_i = int(start_s * rate)
        dur_s = total_sec - start_s
        n = int(dur_s * rate)
        if n <= 0:
            continue
        t = np.linspace(0.0, dur_s, n, endpoint=False, dtype=np.float32)
        env = np.exp(-3.2 * t) * (1.0 - np.exp(-60.0 * t))
        wave = (
            0.70 * np.sin(2.0 * np.pi * freq * t)
            + 0.22 * np.sin(2.0 * np.pi * (freq * 2.0) * t)
            + 0.08 * np.sin(2.0 * np.pi * (freq * 3.0) * t)
        )
        out[start_i : start_i + n] += 0.26 * wave * env

    peak = float(np.max(np.abs(out)))
    if peak > 0:
        out = out / peak * 0.55
    return out.astype(np.float32)


def _format_duration_ru(seconds: int) -> str:
    seconds = max(1, int(round(seconds)))
    hours = seconds // 3600
    mins = (seconds % 3600) // 60
    secs = seconds % 60
    parts: list[str] = []
    if hours > 0:
        h_word = "час" if hours == 1 else ("часа" if 2 <= hours <= 4 else "часов")
        parts.append(f"{hours} {h_word}")
    if mins > 0:
        m_word = "минуту" if mins == 1 else ("минуты" if 2 <= mins <= 4 else "минут")
        parts.append(f"{mins} {m_word}")
    if secs > 0 and hours == 0:
        s_word = "секунду" if secs == 1 else ("секунды" if 2 <= secs <= 4 else "секунд")
        parts.append(f"{secs} {s_word}")
    return " ".join(parts) if parts else f"{seconds} секунд"


def _replace_number_words(text: str) -> str:
    out = text.lower()
    # Особые разговорные выражения
    out = re.sub(r"\bполчаса\b", "30 минут", out)
    out = re.sub(r"\bпол\s+часа\b", "30 минут", out)
    out = re.sub(r"\bчетверть\s+часа\b", "15 минут", out)
    out = re.sub(r"\bполтора\s+часа\b", "90 минут", out)
    out = re.sub(r"\bполторы\s+минуты\b", "90 секунд", out)
    out = re.sub(r"\bполминуты\b", "30 секунд", out)

    for word, val in sorted(_WORD_TO_NUM.items(), key=lambda x: len(x[0]), reverse=True):
        out = re.sub(rf"\b{word}\b", str(val), out)
    return out


def _parse_duration_seconds(t_lower: str) -> int | None:
    """Извлекает длительность в секундах из фраз вида «на 10 минут», «через полчаса», «на минуту»."""
    norm = _replace_number_words(t_lower)
    total = 0

    for m in re.finditer(r"(\d+)\s*(час[а-я]*|ч\b|минут[а-я]*|мин\b|секунд[а-я]*|сек\b)", norm):
        val = int(m.group(1))
        unit = m.group(2)
        if unit.startswith("ч"):
            total += val * 3600
        elif unit.startswith("м"):
            total += val * 60
        elif unit.startswith("с"):
            total += val

    if total > 0:
        return total

    # Случай без числительного: «через минуту», «на минуту», «на час», «на секунду»
    if re.search(r"\b(?:на|через)\s+минут[уы]\b", norm):
        return 60
    if re.search(r"\b(?:на|через)\s+час\b", norm):
        return 3600
    if re.search(r"\b(?:на|через)\s+секунд[уы]\b", norm):
        return 1
    return None


def _parse_clock_time_seconds(t_lower: str) -> tuple[int, str] | None:
    """
    Извлекает точное время суток («на 7:30», «в 14 30», «на 8 утра») и возвращает
    (секунд_до_срабатывания, строка_ЧЧ:ММ).
    """
    norm = _replace_number_words(t_lower)
    m = re.search(
        r"\b(?:на|в)\s*(\d{1,2})(?:[:.\s]+(\d{2}))?\s*(утра|вечера|дня|ночи|часов|часа)?\b",
        norm,
    )
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2)) if m.group(2) is not None else 0
    period = (m.group(3) or "").strip()

    if m.group(2) is None and not period and "будильник" not in norm and "разбуди" not in norm:
        return None
    if hour > 23 or minute > 59:
        return None

    if period in ("вечера", "дня") and 1 <= hour <= 11:
        hour += 12
    elif period == "ночи" and hour == 12:
        hour = 0

    now = dt.datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += dt.timedelta(days=1)
    diff_sec = max(5, int((target - now).total_seconds()))
    return diff_sec, f"{hour:02d}:{minute:02d}"


def _extract_reminder_topic(t: str) -> str:
    """Очищает текст команды от служебных слов («напомни через полчаса ...»), оставляя саму суть напоминания."""
    cleaned = re.sub(
        r"^(?:пожалуйста[\s,]+)?(?:напомни|напомнить|сделай\s+напоминание)(?:\s+мне)?",
        " ",
        t,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\b(?:через|на)\s+(?:полчаса|пол\s+часа|четверть\s+часа|полтора\s+часа|полторы\s+минуты|полминуты|"
        r"(?:[а-яё0-9]+\s+)*(?:час[а-я]*|минут[а-я]*|секунд[а-я]*))\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\b(?:в|на)\s+\d{1,2}(?:[:.\s]\d{2})?(?:\s*(?:утра|вечера|дня|ночи|часов|часа))?\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"^\s*(?:о\s+том\s+что|что\s+надо|что\s+нужно|чтобы\s+я|что)\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = " ".join(cleaned.split()).strip(" .,!?:;")
    return cleaned or "время вышло"


def _play_alarm_on_station_and_mac(
    spoken_text: str,
    title: str,
    tts_speak_fn: Callable[[str], tuple[bytes, int]],
) -> None:
    """
    Синтезирует голосовое оповещение, склеивает его с мелодией колокольчиков и
    проигрывает на Яндекс Станции (192.168.2.83:2222) + показывает уведомление на Mac.
    """
    # 1. Показываем системное уведомление на Mac
    esc_text = spoken_text.replace('"', '\\"')
    esc_title = title.replace('"', '\\"')
    subprocess.run(
        [
            "osascript",
            "-e",
            f'display notification "{esc_text}" with title "{esc_title}" sound name "Glass"',
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )

    # 2. Генерируем комбинированный WAV: [мелодия + речь + мелодия + речь]
    try:
        speech_wav, rate = tts_speak_fn(spoken_text)
        # Извлекаем float32 сэмплы речи из WAV (пропуская 44-байтный заголовок)
        pcm_int16 = np.frombuffer(speech_wav[44:], dtype="<i2")
        speech_samples = pcm_int16.astype(np.float32) / 32767.0
        chime = _generate_alarm_chime(rate)
        pause = np.zeros(int(rate * 0.25), dtype=np.float32)
        combined = np.concatenate([chime, pause, speech_samples, pause, chime, pause, speech_samples])
        full_wav = to_wav_bytes(combined, rate)
    except Exception as exc:
        log.warning("Ошибка синтеза сигнала таймера: %s", exc)
        return

    # 3. Отправляем и проигрываем на Яндекс Станции по SSH через dmix_spk_plug
    remote_cmd = (
        "cat > /tmp/timer_alert.wav && "
        "ALSA_CONFIG_PATH=/etc/alsa-mictap.conf /usr/bin/aplay -N -q -D dmix_spk_plug /tmp/timer_alert.wav; "
        "rm -f /tmp/timer_alert.wav"
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
            input=full_wav,
            capture_output=True,
            timeout=35,
        )
        if res.returncode == 0:
            log.info("Сигнал таймера успешно проигран на колонке: '%s'", spoken_text)
            return
        log.warning("SSH aplay на колонке вернул %s: %s", res.returncode, res.stderr.decode("utf-8", errors="ignore"))
    except Exception as exc:
        log.warning("Не удалось воспроизвести таймер на колонке по SSH: %s", exc)

    # Запасной вариант: проигрываем звук прямо на динамиках Mac через afplay
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(full_wav)
            tmp_path = f.name
        subprocess.run(["/usr/bin/afplay", tmp_path], check=False, timeout=30)
        os.unlink(tmp_path)
    except Exception:
        pass


def _start_station_timer_display(delay_sec: int) -> None:
    """Запускает на мини-часах Яндекс Станции обратный отсчёт таймера."""
    def _run() -> None:
        try:
            subprocess.run(
                [
                    "ssh",
                    "-p",
                    STATION_PORT,
                    "-o",
                    "ConnectTimeout=3",
                    "-o",
                    "StrictHostKeyChecking=no",
                    f"root@{STATION_HOST}",
                    f"/data/mictap/timer-clock.sh start {int(delay_sec)}",
                ],
                capture_output=True,
                timeout=5,
            )
        except Exception as exc:
            log.warning("Не удалось запустить часы таймера на колонке: %s", exc)

    threading.Thread(target=_run, daemon=True).start()


def cancel_all_timers() -> int:
    """Отменяет все активные таймеры, будильники и напоминания, сбрасывает часы на колонке."""
    with _LOCK:
        count = len(_ACTIVE_ITEMS)
        for item in list(_ACTIVE_ITEMS.values()):
            item.cancelled = True
            item.timer_obj.cancel()
        _ACTIVE_ITEMS.clear()
    _stop_station_timer_display()
    return count


def stop_station_audio_and_timers() -> None:
    """Немедленно глушит любые звуки, сигналы таймера, музыку Apple Music и часы на колонке и Mac."""
    cancel_all_timers()

    def _run() -> None:
        try:
            from .music_tools import pause_apple_music

            pause_apple_music()
        except Exception:
            pass

        try:
            # Останавливаем любые aplay и таймер-часы на колонке
            cmd = (
                "for p in $(ps -o pid,args 2>/dev/null | awk '/aplay/ && !/awk/ {print $1}'); do kill -15 \"$p\" 2>/dev/null; done; "
                "/data/mictap/timer-clock.sh stop >/dev/null 2>&1"
            )
            subprocess.run(
                [
                    "ssh",
                    "-p",
                    STATION_PORT,
                    "-o",
                    "ConnectTimeout=3",
                    "-o",
                    "StrictHostKeyChecking=no",
                    f"root@{STATION_HOST}",
                    cmd,
                ],
                capture_output=True,
                timeout=5,
            )
        except Exception as exc:
            log.warning("Не удалось отправить команду остановки на колонку: %s", exc)

        try:
            subprocess.run(["pkill", "-15", "-f", "afplay.*timer"], capture_output=True, timeout=2)
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()


def _stop_station_timer_display() -> None:
    """Останавливает обратный отсчёт на мини-часах Яндекс Станции."""
    def _run() -> None:
        try:
            subprocess.run(
                [
                    "ssh",
                    "-p",
                    STATION_PORT,
                    "-o",
                    "ConnectTimeout=3",
                    "-o",
                    "StrictHostKeyChecking=no",
                    f"root@{STATION_HOST}",
                    "/data/mictap/timer-clock.sh stop",
                ],
                capture_output=True,
                timeout=5,
            )
        except Exception as exc:
            log.warning("Не удалось остановить часы таймера на колонке: %s", exc)

    threading.Thread(target=_run, daemon=True).start()


def _schedule_item(
    kind: str,
    label: str,
    delay_sec: float,
    spoken_alert: str,
    tts_speak_fn: Callable[[str], tuple[bytes, int]],
) -> ScheduledItem:
    global _NEXT_ID
    with _LOCK:
        item_id = _NEXT_ID
        _NEXT_ID += 1

        def _on_fire() -> None:
            with _LOCK:
                cur = _ACTIVE_ITEMS.pop(item_id, None)
                if cur is None or cur.cancelled:
                    return
            log.info("Сработал %s #%d: '%s'", kind, item_id, label)
            if kind == "timer":
                _stop_station_timer_display()
            title = (
                "Таймер" if kind == "timer" else ("Будильник" if kind == "alarm" else "Напоминание")
            )
            _play_alarm_on_station_and_mac(spoken_alert, title, tts_speak_fn)

        tmr = threading.Timer(delay_sec, _on_fire)
        tmr.daemon = True
        item = ScheduledItem(
            id=item_id,
            kind=kind,
            label=label,
            fire_at=time.time() + delay_sec,
            duration_sec=delay_sec,
            timer_obj=tmr,
        )
        _ACTIVE_ITEMS[item_id] = item
        tmr.start()
        return item


def try_handle_timer_command(
    text: str,
    tts_speak_fn: Callable[[str], tuple[bytes, int]],
) -> str | None:
    """
    Обрабатывает голосовые команды таймеров, будильников и напоминаний:
    - «поставь таймер на 10 минут» / «засеки 30 секунд»
    - «напомни через полчаса выключить духовку»
    - «поставь будильник на 8 утра» / «разбуди меня в 7:30»
    - «сколько осталось на таймере?» / «какие таймеры стоят?»
    - «отмени таймер» / «выключи будильник»
    """
    t = " ".join(text.split()).strip()
    if not t:
        return None
    t_lower = t.lower()

    has_keyword = any(
        w in t_lower
        for w in ("таймер", "будильник", "напомни", "напоминан", "разбуди", "засеки")
    )
    if not has_keyword:
        return None

    # 1. Отмена таймеров / будильников / напоминаний
    if any(
        w in t_lower
        for w in ("отмени", "удали", "выключи", "останови", "сбрось", "убери")
    ) and not any(w in t_lower for w in ("через ", "духовк", "плит", "утюг", "чайник", "свет", "комп")):
        with _LOCK:
            if not _ACTIVE_ITEMS:
                return "Сейчас нет активных таймеров, будильников или напоминаний."
            count = len(_ACTIVE_ITEMS)
            for item in list(_ACTIVE_ITEMS.values()):
                item.cancelled = True
                item.timer_obj.cancel()
            _ACTIVE_ITEMS.clear()
        _stop_station_timer_display()
        return f"Отменила все активные таймеры и напоминания ({count} шт.)."

    # 2. Проверка статуса («сколько осталось на таймере», «какие таймеры стоят»)
    if any(
        p in t_lower
        for p in (
            "сколько осталось",
            "какие таймеры",
            "какие будильники",
            "какие напоминания",
            "проверь таймер",
            "статус таймера",
        )
    ):
        with _LOCK:
            active = list(_ACTIVE_ITEMS.values())
        if not active:
            return "Сейчас нет запущенных таймеров или напоминаний."
        now = time.time()
        parts: list[str] = []
        for it in active:
            rem = max(1, int(round(it.fire_at - now)))
            rem_str = _format_duration_ru(rem)
            if it.kind == "timer":
                parts.append(f"до конца таймера «{it.label}» осталось {rem_str}")
            elif it.kind == "alarm":
                parts.append(f"будильник на {it.label} сработает через {rem_str}")
            else:
                parts.append(f"напоминание «{it.label}» сработает через {rem_str}")
        return "Сейчас активно: " + "; ".join(parts) + "."

    # 3. Напоминание («напомни через полчаса выключить духовку», «напомни в 14:30 про встречу»)
    if any(w in t_lower for w in ("напомни", "напоминан")):
        dur_sec = _parse_duration_seconds(t_lower)
        clock_res = _parse_clock_time_seconds(t_lower) if dur_sec is None else None
        topic = _extract_reminder_topic(t)

        if dur_sec is not None:
            dur_str = _format_duration_ru(dur_sec)
            alert = f"Внимание! Напоминаю: {topic}."
            _schedule_item("reminder", topic, float(dur_sec), alert, tts_speak_fn)
            return f"Хорошо, напомню через {dur_str}: {topic}."
        if clock_res is not None:
            delay_sec, time_str = clock_res
            alert = f"Внимание! Напоминаю: {topic}."
            _schedule_item("reminder", f"{topic} ({time_str})", float(delay_sec), alert, tts_speak_fn)
            return f"Хорошо, напомню в {time_str}: {topic}."

    # 4. Будильник («поставь будильник на 8 утра», «разбуди меня в 7:30», «разбуди через час»)
    if any(w in t_lower for w in ("будильник", "разбуди")):
        clock_res = _parse_clock_time_seconds(t_lower)
        if clock_res is not None:
            delay_sec, time_str = clock_res
            dur_str = _format_duration_ru(delay_sec)
            alert = f"Доброе утро! Сработал будильник на {time_str}. Пора вставать!"
            _schedule_item("alarm", time_str, float(delay_sec), alert, tts_speak_fn)
            return f"Поставила будильник на {time_str}. Он прозвенит через {dur_str}."

        dur_sec = _parse_duration_seconds(t_lower)
        if dur_sec is not None:
            dur_str = _format_duration_ru(dur_sec)
            alert = f"Внимание! Сработал будильник. Прошло {dur_str}, пора вставать!"
            _schedule_item("alarm", dur_str, float(dur_sec), alert, tts_speak_fn)
            return f"Поставила будильник, разбужу вас через {dur_str}."

    # 5. Таймер («поставь таймер на 10 минут», «засеки 30 секунд»)
    if any(w in t_lower for w in ("таймер", "засеки")):
        dur_sec = _parse_duration_seconds(t_lower)
        if dur_sec is not None:
            dur_str = _format_duration_ru(dur_sec)
            alert = f"Внимание! Время вышло — сработал таймер на {dur_str}."
            _schedule_item("timer", dur_str, float(dur_sec), alert, tts_speak_fn)
            _start_station_timer_display(int(dur_sec))
            return f"Поставила таймер на {dur_str}."

    return None
