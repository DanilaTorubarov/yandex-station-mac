"""Управление 4-разрядным LED-дисплеем и индикаторами Яндекс Станции Мини 2."""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
from typing import Any

log = logging.getLogger(__name__)

STATION_HOST = os.environ.get("STATION_IP", "192.168.2.83")
STATION_PORT = os.environ.get("STATION_SSH_PORT", "2222")


def _run_display_cmd(args: list[str], async_mode: bool = True) -> bool:
    """Запуск команды display-tool на станции через SSH."""
    cmd_str = " ".join(shlex.quote(a) for a in args)
    full_cmd = f"/data/mictap/display-tool {cmd_str}"
    if async_mode:
        full_cmd = f"nohup {full_cmd} >/dev/null 2>&1 &"

    ssh_cmd = [
        "ssh",
        "-p",
        STATION_PORT,
        "-o",
        "ConnectTimeout=3",
        "-o",
        "StrictHostKeyChecking=no",
        f"root@{STATION_HOST}",
        full_cmd,
    ]
    try:
        if async_mode:
            subprocess.Popen(ssh_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        res = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=5)
        return res.returncode == 0
    except Exception as exc:
        log.warning("Ошибка выполнения команды дисплея на станции: %s", exc)
        return False


def show_display_text(text: str, scroll: bool = False, speed_ms: int = 180, loops: int = 1) -> dict[str, Any]:
    """Показывает текст (статично или бегущей строкой) на дисплее."""
    text = text.strip()
    if not text:
        return clear_display()

    if scroll or len(text) > 4:
        ok = _run_display_cmd(["scroll", text, str(speed_ms), str(loops)], async_mode=True)
        return {"ok": ok, "mode": "scroll", "text": text}

    ok = _run_display_cmd(["show", text], async_mode=False)
    return {"ok": ok, "mode": "show", "text": text}


def show_display_temperature(val: int | str, duration_sec: int = 6) -> dict[str, Any]:
    """Показывает температуру на дисплее со значком градуса (напр. +21° или -5°)."""
    val_str = str(val).replace("+", "").strip()
    ok = _run_display_cmd(["temp", val_str, str(duration_sec)], async_mode=True)
    return {"ok": ok, "mode": "temp", "value": val_str, "duration": duration_sec}


def show_display_face(face: str, duration_sec: int = 5) -> dict[str, Any]:
    """Показывает эмоцию / рожицу (happy, sleep, cool, wink, love, talk, think, dizzy)."""
    valid_faces = {"happy", "sleep", "cool", "wide", "wink", "love", "talk", "speaking", "think", "spinner", "dizzy"}
    f = face.lower().strip()
    if f not in valid_faces:
        f = "happy"
    ok = _run_display_cmd(["face", f, str(duration_sec)], async_mode=(duration_sec > 0))
    return {"ok": ok, "mode": "face", "face": f, "duration": duration_sec}


def show_display_equalizer(duration_sec: int = 10) -> dict[str, Any]:
    """Включает динамический эквалайзер / аудио-волну на дисплее."""
    ok = _run_display_cmd(["eq", str(duration_sec)], async_mode=True)
    return {"ok": ok, "mode": "eq", "duration": duration_sec}


def show_display_timer(seconds: int) -> dict[str, Any]:
    """Запускает отображение обратного отсчёта таймера на дисплее."""
    ok = _run_display_cmd(["timer", str(int(seconds))], async_mode=True)
    return {"ok": ok, "mode": "timer", "seconds": seconds}


def show_display_clock(seconds: int = 0) -> dict[str, Any]:
    """Возвращает дисплей в режим отображения текущего времени."""
    ok = _run_display_cmd(["clock", str(seconds)], async_mode=(seconds > 0))
    return {"ok": ok, "mode": "clock"}


def set_display_brightness(level: int) -> dict[str, Any]:
    """Устанавливает яркость сегментов дисплея (от 1 до 255)."""
    lvl = max(1, min(255, int(level)))
    ok = _run_display_cmd(["brightness", str(lvl)], async_mode=False)
    return {"ok": ok, "brightness": lvl}


def clear_display() -> dict[str, Any]:
    """Гасит все сегменты и индикаторы дисплея."""
    ok = _run_display_cmd(["clear"], async_mode=False)
    return {"ok": ok, "mode": "clear"}


def try_handle_display_command(text: str) -> str | None:
    """Голосовые команды управления экраном/дисплеем колонки."""
    t_lower = text.strip().lower()

    # 1. Смайлики / Эмоции
    if any(p in t_lower for p in ("улыбнись", "покажи улыбку", "сделай улыбку", "радостное лицо")):
        show_display_face("happy", 5)
        return "Улыбаюсь!"
    if any(p in t_lower for p in ("подмигни", "подмигивание", "сделай подмигивание")):
        show_display_face("wink", 4)
        return "Подмигнула!"
    if any(p in t_lower for p in ("покажи удивление", "удивись", "удивлённое лицо")):
        show_display_face("wide", 4)
        return "Ого!"
    if any(p in t_lower for p in ("покажи любовь", "покажи сердечко", "любовь на экране")):
        show_display_face("love", 5)
        return "С любовью!"
    if any(p in t_lower for p in ("пора спать", "иди спать", "спящее лицо", "режим сна на экране")):
        show_display_face("sleep", 10)
        return "Засыпаю..."
    if any(p in t_lower for p in ("покажи смайлик", "покажи рожицу", "покажи эмоцию", "сделай лицо")):
        show_display_face("happy", 5)
        return "Вот смайлик!"

    # 2. Эквалайзер / Аудио-волна
    if any(p in t_lower for p in ("включи эквалайзер", "эквалайзер на экране", "покажи эквалайзер", "покажи волну", "аудио волна")):
        show_display_equalizer(15)
        return "Включила эквалайзер на экране."

    # 3. Часы / Время
    if any(p in t_lower for p in ("верни часы", "покажи часы", "включи часы", "верни время на дисплей")):
        show_display_clock()
        return "Вернула часы на дисплей."

    # 4. Выключение / Очистка
    if any(p in t_lower for p in ("погаси дисплей", "погаси экран", "выключи дисплей", "выключи экран", "очисти экран", "погаси часы")):
        clear_display()
        return "Погасила экран."

    # 5. Текст / Бегущая строка
    # Напр: «покажи на экране привет», «напиши на часах мир», «выведи на дисплей тест»
    import re
    m_text = re.search(
        r"(?:покажи|напиши|выведи|запусти)(?:\s+(?:бегущую\s+строку|текст))?\s+(?:на\s+(?:экране|дисплее|часах))\s+(.+)",
        t_lower,
    )
    if not m_text:
        m_text = re.search(r"^(?:бегущая\s+строка)\s+(.+)", t_lower)

    if m_text:
        raw_msg = m_text.group(1).strip(" .!?,")
        if raw_msg:
            # Извлекаем оригинальный регистр из исходного текста
            # Ищем подстроку в text
            idx = t_lower.find(raw_msg)
            display_str = text[idx : idx + len(raw_msg)] if idx >= 0 else raw_msg
            show_display_text(display_str, scroll=True, speed_ms=180, loops=2)
            return f"Вывела на дисплей: {display_str}."

    # 6. Яркость экрана
    if "яркост" in t_lower and any(w in t_lower for w in ("экран", "диспле", "час")):
        if any(w in t_lower for w in ("максимум", "максимальн", "на всю")):
            set_display_brightness(255)
            return "Установила максимальную яркость экрана."
        if any(w in t_lower for w in ("минимум", "минимальн")):
            set_display_brightness(5)
            return "Установила минимальную яркость экрана."
        m_val = re.search(r"(\d+)", t_lower)
        if m_val:
            val = int(m_val.group(1))
            val = max(1, min(255, val))
            set_display_brightness(val)
            return f"Яркость экрана установлена на {val}."

    return None

