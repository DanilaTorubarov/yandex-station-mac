"""Системное управление Mac: громкость, медиа, заряд батареи, блокировка/сон, скриншоты и папки."""

from __future__ import annotations

import ctypes
import datetime as dt
import logging
import os
import re
import shutil
import socket
import subprocess
import threading
from pathlib import Path

log = logging.getLogger(__name__)

# Словарь числительных для голосового управления громкостью («на пятьдесят процентов», «на тридцать»)
_RU_NUMBERS: dict[str, int] = {
    "ноль": 0,
    "десять": 10,
    "пятнадцать": 15,
    "двадцать": 20,
    "двадцать пять": 25,
    "тридцать": 30,
    "сорок": 40,
    "полтинник": 50,
    "половину": 50,
    "пятьдесят": 50,
    "шестьдесят": 60,
    "семьдесят": 70,
    "восемьдесят": 80,
    "девяносто": 90,
    "сто": 100,
    "максимум": 100,
    "полную": 100,
    "минимум": 10,
}

FOLDER_ALIASES: list[tuple[tuple[str, ...], Path, str]] = [
    (("загрузк", "скачанн", "downloads"), Path.home() / "Downloads", "папку Загрузки"),
    (("рабочий стол", "рабочего стола", "десктоп", "desktop"), Path.home() / "Desktop", "Рабочий стол"),
    (("документ", "documents"), Path.home() / "Documents", "папку Документы"),
    (("программ", "applications"), Path("/Applications"), "папку Программы"),
    (("картинк", "изображен", "pictures"), Path.home() / "Pictures", "папку Изображения"),
    (("фильм", "movies"), Path.home() / "Movies", "папку Фильмы"),
    (("домашн", "home"), Path.home(), "домашнюю папку"),
]


def find_user_folder(target_raw: str) -> Path | None:
    """Ищет пользовательскую папку на Mac по имени или синониму."""
    clean = target_raw.strip().lower()
    if not clean:
        return None

    targets = [clean]
    synonyms = {
        "скул": "school",
        "школа": "school",
        "школу": "school",
        "учеба": "school",
        "учёба": "school",
        "код": "codes",
        "коды": "codes",
        "кодес": "codes",
        "проекты": "codes",
        "обсидиан": "obsidian",
        "телеграм": "telegram",
        "телега": "telegram",
        "зум": "zoom",
        "модель": "model",
        "модели": "model",
    }
    for syn_k, syn_v in synonyms.items():
        if syn_k in clean and syn_v not in targets:
            targets.append(syn_v)

    # 1. Быстрое прямое сканирование основных каталогов пользователя
    primary_roots = [
        Path.home() / "Documents",
        Path.home() / "Desktop",
        Path.home() / "Downloads",
        Path.home() / "Codes",
        Path.home(),
        Path("/Applications"),
    ]

    # Точное совпадение имени папки
    for root in primary_roots:
        if not root.is_dir():
            continue
        try:
            for child in root.iterdir():
                if child.is_dir() and not child.name.startswith("."):
                    child_low = child.name.lower()
                    for t in targets:
                        if child_low == t:
                            return child
        except Exception:
            pass

    # Подстрочное / префиксное совпадение в основных каталогах
    for root in primary_roots:
        if not root.is_dir():
            continue
        try:
            for child in root.iterdir():
                if child.is_dir() and not child.name.startswith("."):
                    child_low = child.name.lower()
                    for t in targets:
                        if t in child_low:
                            return child
        except Exception:
            pass

    # 2. Быстрый поиск через системный Spotlight (mdfind) по домашней директории (исключая Library)
    for t in targets:
        try:
            res = subprocess.run(
                [
                    "mdfind",
                    "-onlyin",
                    str(Path.home()),
                    f"kMDItemContentType == 'public.folder' && kMDItemFSName == '*{t}*'c",
                ],
                capture_output=True,
                text=True,
                timeout=3,
            )
            candidates = []
            for line in res.stdout.splitlines():
                p = Path(line.strip())
                if p.is_dir() and not p.name.startswith(".") and "Library" not in p.parts:
                    candidates.append(p)
            if candidates:
                for c in candidates:
                    if c.name.lower() == t:
                        return c
                return candidates[0]
        except Exception:
            pass

    return None


# --- 1. Громкость на Mac -----------------------------------------------------

def get_mac_volume() -> int:
    res = subprocess.run(
        ["osascript", "-e", "output volume of (get volume settings)"],
        capture_output=True,
        text=True,
        timeout=4,
    )
    try:
        return int(res.stdout.strip())
    except Exception:
        return 50


def set_mac_volume(level: int) -> int:
    clamped = max(0, min(100, int(level)))
    subprocess.run(
        ["osascript", "-e", f"set volume without output muted\nset volume output volume {clamped}"],
        capture_output=True,
        text=True,
        timeout=4,
    )
    return clamped


def set_mac_mute(muted: bool) -> None:
    clause = "with output muted" if muted else "without output muted"
    subprocess.run(
        ["osascript", "-e", f"set volume {clause}"],
        capture_output=True,
        text=True,
        timeout=4,
    )


def _extract_volume_number(t_lower: str) -> int | None:
    m = re.search(r"\b(\d{1,3})\s*(?:%|процент)?", t_lower)
    if m:
        return max(0, min(100, int(m.group(1))))
    for phrase, val in sorted(_RU_NUMBERS.items(), key=lambda x: len(x[0]), reverse=True):
        if phrase in t_lower:
            return val
    return None


STATION_IP = os.environ.get("STATION_IP", "192.168.2.83")
STATION_SSH_PORT = os.environ.get("STATION_SSH_PORT", "2222")

# Акустическая кривая (dB) для аппаратного усилителя SPK Master на Яндекс Станции Мини 2:
# Безопасный потолок 239 (-8 dB): максимальная громкость без перегруза и крика.
# Уровень 3 (30%) - комфортный слышный фон (-30 dB), 5 (50%) - стандартный дневной (-22 dB).
SPK_VOL_MAP = [
    0,    # 0: mute
    175,  # 1: тихий ночной (-40.0 dB)
    185,  # 2: тихий вечерний (-35.0 dB)
    195,  # 3: комфортный фоновый (-30.0 dB)
    203,  # 4: мягкий (-26.0 dB)
    211,  # 5: стандартный средний (-22.0 dB)
    217,  # 6: (-19.0 dB)
    223,  # 7: уверенно громкий без перегруза (-16.0 dB)
    229,  # 8: (-13.0 dB)
    234,  # 9: (-10.5 dB)
    239,  # 10: максимум (безопасный потолок, не орёт, -8.0 dB)
]


def get_station_volume() -> int:
    """Возвращает уровень громкости Яндекс Станции (от 1 до 10)."""
    try:
        res = subprocess.run(
            [
                "ssh",
                "-p",
                STATION_SSH_PORT,
                "-o",
                "ConnectTimeout=2",
                "-o",
                "StrictHostKeyChecking=no",
                f"root@{STATION_IP}",
                "amixer sget 'SPK Master' | awk '/Mono: Playback/ {print $3}'",
            ],
            capture_output=True,
            text=True,
            timeout=3,
        )
        val = int(res.stdout.strip())
        best_lvl = 5
        best_diff = 999
        for i in range(1, 11):
            d = abs(val - SPK_VOL_MAP[i])
            if d < best_diff:
                best_diff = d
                best_lvl = i
        return best_lvl
    except Exception:
        return 5


def set_station_volume(lvl: int) -> int:
    """Устанавливает уровень громкости на Яндекс Станции (от 1 до 10)."""
    clamped = max(1, min(10, int(lvl)))
    master_val = int(round(clamped * 25.5))
    if master_val > 255:
        master_val = 255
    spk_val = SPK_VOL_MAP[clamped]
    cmd = (
        f"amixer sset 'Master' {master_val} >/dev/null 2>&1 && "
        f"amixer sset 'SPK Master' {spk_val} >/dev/null 2>&1"
    )
    try:
        subprocess.run(
            [
                "ssh",
                "-p",
                STATION_SSH_PORT,
                "-o",
                "ConnectTimeout=2",
                "-o",
                "StrictHostKeyChecking=no",
                f"root@{STATION_IP}",
                cmd,
            ],
            capture_output=True,
            timeout=3,
        )
    except Exception as exc:
        log.warning("Не удалось изменить громкость на станции: %s", exc)

    # Если в Apple Music играет AirPlay, синхронизируем ползунок на Mac
    try:
        script = f"""
        var music = Application("Music");
        var dev = music.airplayDevices.whose({{name: "Яндекс Станция Мини 2"}})[0];
        if (dev && dev.selected()) {{
            dev.soundVolume = {clamped * 10};
        }}
        """
        subprocess.run(["osascript", "-l", "JavaScript", "-e", script], capture_output=True, timeout=2)
    except Exception:
        pass

    return clamped


def _try_handle_volume_command(t: str, t_lower: str) -> str | None:
    # Не перехватываем «выключи звук в дс» (это обрабатывается в discord_tools)
    if any(w in t_lower for w in (" в дс", " в дискорд", " discord")):
        return None

    has_vol_word = any(w in t_lower for w in ("громкост", "звук", "тише", "громче", "без звука"))
    if not has_vol_word:
        return None

    is_mac_target = any(
        w in t_lower
        for w in ("на компе", "на маке", "на компьютере", "на ноуте", "на ноутбуке", "в маке")
    )

    # 1. Запрос уровня громкости («какая громкость»)
    if any(w in t_lower for w in ("какая громкость", "сколько громкость", "уровень громкости")):
        if is_mac_target:
            cur = get_mac_volume()
            return f"Сейчас громкость на компьютере {cur} процентов."
        cur = get_station_volume()
        return f"Сейчас громкость колонки {cur} из десяти."

    # 2. Выключить звук («выключи звук»)
    if any(w in t_lower for w in ("выключи звук", "отключи звук", "без звука", "убери звук")):
        if is_mac_target:
            set_mac_mute(True)
            return "Выключила звук на компьютере."
        set_station_volume(1)
        return "Убавила звук на колонке до минимума."

    # 3. Включить звук
    if any(w in t_lower for w in ("включи звук", "верни звук")):
        if is_mac_target:
            set_mac_mute(False)
            return "Включила звук на компьютере."
        set_station_volume(4)
        return "Включила громкость на колонке на четыре."

    target_num = _extract_volume_number(t_lower)

    # 4. Сделай тише
    if any(w in t_lower for w in ("тише", "потише", "убавь", "уменьши")):
        if is_mac_target:
            cur = get_mac_volume()
            step = target_num if (target_num is not None and target_num <= 50) else 20
            new_v = set_mac_volume(cur - step)
            return f"Сделала звук на компьютере тише — {new_v} процентов."
        cur = get_station_volume()
        step = target_num if (target_num is not None and target_num <= 4) else 1
        new_v = set_station_volume(cur - step)
        return f"Сделала тише — громкость {new_v}."

    # 5. Сделай громче
    if any(w in t_lower for w in ("громче", "погромче", "прибавь", "увеличь")):
        if is_mac_target:
            cur = get_mac_volume()
            step = target_num if (target_num is not None and target_num <= 50) else 20
            new_v = set_mac_volume(cur + step)
            return f"Сделала звук на компьютере громче — {new_v} процентов."
        cur = get_station_volume()
        step = target_num if (target_num is not None and target_num <= 4) else 1
        new_v = set_station_volume(cur + step)
        return f"Сделала громче — громкость {new_v}."

    # 6. Установка конкретной громкости («громкость 5», «поставь громкость на 7», «звук 40%»)
    if target_num is not None and any(w in t_lower for w in ("громкост", "звук", "процент", "%", "на ")):
        if is_mac_target:
            new_v = set_mac_volume(target_num)
            return f"Установила громкость на компьютере на {new_v} процентов."
        target_lvl = target_num
        if target_lvl > 10:
            target_lvl = int(round(target_lvl / 10.0))
        new_v = set_station_volume(target_lvl)
        return f"Установила громкость колонки на {new_v}."

    return None


# --- 2. Питание и безопасность (заряд, блокировка экрана, спящий режим) ------

def get_battery_status_speech() -> str:
    """Читает статус батареи через pmset -g batt и формирует голосовой ответ по-русски."""
    res = subprocess.run(["/usr/bin/pmset", "-g", "batt"], capture_output=True, text=True, timeout=4)
    out = res.stdout or ""
    m_pct = re.search(r"(\d{1,3})%", out)
    if not m_pct:
        return "Не удалось определить уровень заряда батареи на маке."
    pct = int(m_pct.group(1))

    if "charging" in out and "discharging" not in out:
        state_str = "ноутбук сейчас заряжается от сети"
    elif "charged" in out:
        state_str = "батарея полностью заряжена, ноутбук подключён к сети"
    else:
        state_str = "работает от аккумулятора"

    m_rem = re.search(r"(\d+):(\d+)\s+remaining", out)
    if m_rem:
        hours, mins = int(m_rem.group(1)), int(m_rem.group(2))
        if hours > 0:
            return f"На маке сейчас {pct} процентов заряда, {state_str}. Осталось примерно {hours} часов {mins} минут работы."
        return f"На маке сейчас {pct} процентов заряда, {state_str}. Осталось около {mins} минут."

    return f"На маке сейчас {pct} процентов заряда, {state_str}."


def _lock_screen_now() -> None:
    """Мгновенно блокирует экран macOS через login.framework (SACLockScreenImmediate) с фоллбэком Ctrl+Cmd+Q."""
    try:
        login = ctypes.cdll.LoadLibrary(
            "/System/Library/PrivateFrameworks/login.framework/Versions/Current/login"
        )
        login.SACLockScreenImmediate()
        return
    except Exception as exc:
        log.warning("SACLockScreenImmediate не сработал: %s", exc)
    subprocess.run(
        [
            "osascript",
            "-e",
            'tell application "System Events" to key code 12 using {control down, command down}',
        ],
        capture_output=True,
        text=True,
        timeout=4,
    )


def _sleep_mac_now() -> None:
    """Отправляет Mac в спящий режим."""
    res = subprocess.run(["/usr/bin/pmset", "sleepnow"], capture_output=True, text=True, timeout=4)
    if res.returncode != 0:
        subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to sleep'],
            capture_output=True,
            text=True,
            timeout=4,
        )


def _try_handle_power_command(t: str, t_lower: str) -> str | None:
    # 1. Уровень заряда батареи
    if any(
        w in t_lower
        for w in (
            "заряд",
            "батаре",
            "аккумулятор",
            "сколько процентов на маке",
            "сколько процентов на компе",
        )
    ):
        return get_battery_status_speech()

    # 2. Заблокировать экран
    if any(
        p in t_lower
        for p in (
            "заблокируй экран",
            "заблокируй комп",
            "заблокируй мак",
            "заблокируй ноутбук",
            "блокируй экран",
            "блокируй комп",
            "блокируй мак",
            "залочь экран",
            "залочь комп",
            "залочь мак",
        )
    ) or bool(re.search(r"\b(?:заблокируй|блокируй|залочь)\b.*\b(?:экран|комп\w*|мак\w*|ноут\w*)\b", t_lower)):
        # Небольшая задержка 1.0 с, чтобы сервер успел отдать аудио-ответ колонке до блокировки
        threading.Timer(1.0, _lock_screen_now).start()
        return "Блокирую экран компьютера."

    # 3. Спящий режим («усыпи мак», «отправь комп в сон», «переведи компьютер в сон»)
    if any(
        p in t_lower
        for p in (
            "усыпи мак",
            "усыпи комп",
            "усыпи ноутбук",
            "отправь мак в сон",
            "отправь комп в сон",
            "переведи компьютер в сон",
            "переведи комп в сон",
            "переведи мак в сон",
            "переведи ноутбук в сон",
            "спящий режим",
            "режим сна",
            "выключи экран мака",
            "выключи экран компьютера",
            "погаси экран",
        )
    ) or bool(
        re.search(
            r"\b(?:усыпи|отправь|переведи|погрузи|включи)\b.*\b(?:в\s+сон|спящ\w*\s+режим|режим\s+сна)\b",
            t_lower,
        )
    ):
        threading.Timer(1.8, _sleep_mac_now).start()
        return "Перевожу компьютер в спящий режим."

    return None


# --- 3. Скриншоты и открытие папок -------------------------------------------

def take_and_save_screenshot() -> str:
    """
    Делает скриншот экрана, сохраняет его на Рабочий стол (~/Desktop)
    и копирует изображение в буфер обмена macOS.
    """
    desktop = Path.home() / "Desktop"
    desktop.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    out_path = desktop / f"Скриншот_{stamp}.jpg"

    res = subprocess.run(
        ["/usr/sbin/screencapture", "-x", "-t", "jpg", str(out_path)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if res.returncode != 0 or not out_path.is_file() or out_path.stat().st_size < 1000:
        # Используем сокет screen_helper (с TCC-разрешением Screen Recording)
        sock_path = Path("/tmp/station-screen-helper.sock")
        if sock_path.exists():
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(5.0)
                    s.connect(str(sock_path))
                    resp = s.recv(16)
                    if resp.startswith(b"OK") and Path("/tmp/assistant_screen.jpg").is_file():
                        shutil.copyfile("/tmp/assistant_screen.jpg", out_path)
            except Exception as exc:
                log.warning("Не удалось получить скриншот через screen_helper: %s", exc)

    if not out_path.is_file():
        return "Не удалось сохранить скриншот экрана."

    # Копируем картинку в системный буфер обмена macOS, чтобы её можно было вставить через Cmd+V
    subprocess.run(
        [
            "osascript",
            "-e",
            f'set the clipboard to (read (POSIX file "{out_path}") as JPEG picture)',
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    return "Сделала скриншот экрана, сохранила на рабочий стол и скопировала в буфер обмена."


def _try_handle_files_and_screenshots_command(t: str, t_lower: str) -> str | None:
    # 1. Сделать и сохранить скриншот (если не просят проанализировать/объяснить/решить то, что на экране)
    if any(
        p in t_lower
        for p in (
            "сделай скриншот",
            "сделай снимок экрана",
            "сохрани скриншот",
            "сфоткай экран",
            "скринь экран",
            "заскринь экран",
        )
    ) and not any(
        q in t_lower
        for q in ("объясни", "реши", "переведи", "что там", "что открыто", "почему", "конспект", "ответь")
    ):
        return take_and_save_screenshot()

    # 2. Открыть папку в Finder («открой папку Загрузки / Рабочий стол / Документы»)
    if any(v in t_lower for v in ("открой", "открыть", "покажи", "запусти")):
        if "корзин" in t_lower:
            subprocess.run(
                ["osascript", "-e", 'tell application "Finder"\nactivate\nopen trash\nend tell'],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return "Открыла Корзину на компьютере."

        has_folder_word = "папк" in t_lower or "finder" in t_lower or "файндер" in t_lower
        for keywords, folder_path, label in FOLDER_ALIASES:
            if any(k in t_lower for k in keywords):
                # Для слова «документы» или «программы» без слова «папка» проверяем, чтобы не путать с Google Docs
                if label in ("папку Документы", "папку Программы") and not has_folder_word:
                    continue
                subprocess.run(["open", str(folder_path)], check=False, timeout=5)
                return f"Открываю {label} на компьютере."

        # Если стандартные псевдонимы не совпали, ищем пользовательскую папку по названию
        if has_folder_word:
            m_folder = re.search(
                r"(?:открой|покажи|запусти)(?:\s+(?:мне|в\s+файндере|в\s+finder))?\s+папку\s+([A-Za-zА-Яа-я0-9_\-\s]+)",
                t_lower,
            )
            target_name = ""
            if m_folder:
                target_name = m_folder.group(1).strip()
            else:
                m_alt = re.search(r"папк[аеу]\s+([A-Za-zА-Яа-я0-9_\-]+)", t_lower)
                if m_alt:
                    target_name = m_alt.group(1).strip()

            if target_name:
                target_name = re.sub(
                    r"\s+(?:пожалуйста|плиз|быстро|сейчас|на\s+компе|на\s+компьютере|на\s+маке)$",
                    "",
                    target_name,
                ).strip()
                found = find_user_folder(target_name)
                if found:
                    subprocess.run(["open", str(found)], check=False, timeout=5)
                    return f"Открываю папку {found.name} на компьютере."

    return None


def try_handle_system_command(t: str, t_lower: str) -> str | None:
    """Единая точка входа для системных команд управления Mac."""
    vol_res = _try_handle_volume_command(t, t_lower)
    if vol_res:
        return vol_res

    pwr_res = _try_handle_power_command(t, t_lower)
    if pwr_res:
        return pwr_res

    file_res = _try_handle_files_and_screenshots_command(t, t_lower)
    if file_res:
        return file_res

    return None
