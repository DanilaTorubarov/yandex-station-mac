"""Поиск iPhone через Apple Shortcuts (Команды) и приложение Локатор (Find My)."""

from __future__ import annotations

import logging
import re
import subprocess
import threading
import time

log = logging.getLogger(__name__)

_SHORTCUT_CACHE: tuple[float, str | None] = (0.0, None)

# Шаблоны голосовых запросов на поиск телефона
_FIND_PHONE_PATTERN = re.compile(
    r"\b("
    r"где\s+(?:мой\s+|мой\s+же\s+)?(?:айфон\w*|iphone|телефон\w*|трубк\w*)|"
    r"найди\s+(?:мой\s+)?(?:айфон\w*|iphone|телефон\w*)|"
    r"(?:подай|включи|запусти)\s+(?:звук\w*|сигнал\w*)\s+на\s+(?:айфон\w*|iphone|телефон\w*)|"
    r"позвони\s+на\s+(?:мой\s+)?(?:айфон\w*|iphone|телефон\w*)|"
    r"поиск\s+(?:айфон\w*|телефон\w*|iphone)|"
    r"посигналь\s+(?:на\s+)?(?:айфон\w*|iphone|телефон\w*)"
    r")\b",
    re.IGNORECASE,
)

# Имена шорткатов, которые считаются подходящими
_KNOWN_SHORTCUT_NAMES = (
    "найти iphone",
    "найти айфон",
    "где мой iphone",
    "где мой айфон",
    "find iphone",
    "find my iphone",
    "найти телефон",
    "где телефон",
    "где айфон",
    "поиск телефона",
    "поиск айфона",
    "ping iphone",
    "сигнал на айфон",
)


def find_phone_shortcut_name() -> str | None:
    """Ищет в системе Apple Shortcut для поиска iPhone (с кэшем на 60 с)."""
    global _SHORTCUT_CACHE
    now = time.monotonic()
    if now - _SHORTCUT_CACHE[0] < 60.0:
        return _SHORTCUT_CACHE[1]
    try:
        res = subprocess.run(
            ["/usr/bin/shortcuts", "list"],
            capture_output=True,
            text=True,
            timeout=3.0,
        )
        if res.returncode != 0:
            _SHORTCUT_CACHE = (now, None)
            return None
        lines = [line.strip() for line in res.stdout.splitlines() if line.strip()]

        # 1. Точное совпадение
        for name in lines:
            if name.lower() in _KNOWN_SHORTCUT_NAMES:
                _SHORTCUT_CACHE = (now, name)
                return name

        # 2. Нечёткое совпадение по ключевым словам
        for name in lines:
            n_low = name.lower()
            has_device = any(w in n_low for w in ("iphone", "айфон", "телефон"))
            has_action = any(v in n_low for v in ("find", "найти", "где", "поиск", "sound", "звук", "сигнал", "ping"))
            if has_device and has_action:
                _SHORTCUT_CACHE = (now, name)
                return name
    except Exception as exc:
        log.warning("Ошибка проверки списка shortcuts: %s", exc)
    _SHORTCUT_CACHE = (now, None)
    return None


def open_find_my_devices() -> bool:
    """Активирует Find My и переключает на вкладку Devices (Устройства)."""
    script = """
    tell application "FindMy" to activate
    delay 0.3
    tell application "System Events"
        tell process "FindMy"
            try
                click menu item "Devices" of menu 1 of menu bar item "View" of menu bar 1
            end try
        end tell
    end tell
    """
    try:
        subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, timeout=4.0)
        return True
    except Exception as exc:
        log.warning("Ошибка переключения на Devices в FindMy: %s", exc)
        return False


def trigger_find_phone() -> str:
    """Запускает поиск iPhone через Shortcut или открывает Локатор на вкладке Устройства."""
    shortcut = find_phone_shortcut_name()
    if shortcut:
        def _run_shortcut() -> None:
            try:
                subprocess.run(
                    ["/usr/bin/shortcuts", "run", shortcut],
                    capture_output=True,
                    text=True,
                    timeout=6.0,
                )
            except Exception as exc:
                log.warning("Ошибка вызова shortcuts run '%s': %s", shortcut, exc)

        threading.Thread(target=_run_shortcut, daemon=True).start()
        log.info("Запущен шорткат поиска телефона в фоне: '%s'", shortcut)
        return f"Подаю сигнал на ваш iPhone через команду «{shortcut}». Телефон сейчас зазвонит."

    # Открываем приложение Find My в фоновом потоке, чтобы голосовой ответ вернулся за 1 мс
    threading.Thread(target=open_find_my_devices, daemon=True).start()
    log.info("Открыто приложение FindMy (вкладка Devices) в фоне")
    return "Открыла Локатор на вкладке ваших устройств, нажмите воспроизвести звук рядом с айфоном."


def try_handle_find_phone_command(text: str) -> str | None:
    """
    Обрабатывает команду поиска телефона:
    «Где мой айфон?», «Где мой телефон?», «Найди мой айфон», «Подай сигнал на айфон».
    """
    t = " ".join(text.split()).strip()
    if not t:
        return None

    if _FIND_PHONE_PATTERN.search(t):
        return trigger_find_phone()

    return None
