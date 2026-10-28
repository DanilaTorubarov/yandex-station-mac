"""Интеграция голосового ассистента с macOS: открытие программ, сайтов, набор текста, зрение экрана и управление внутри приложений."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

# Словарь русских и разговорных названий приложений macOS
APP_ALIASES: dict[str, list[str]] = {
    "телеграм": ["Telegram"],
    "телегу": ["Telegram"],
    "телега": ["Telegram"],
    "telegram": ["Telegram"],
    "дискорд": ["Discord"],
    "discord": ["Discord"],
    "вотсап": ["WhatsApp"],
    "вацап": ["WhatsApp"],
    "ватсап": ["WhatsApp"],
    "whatsapp": ["WhatsApp"],
    "хром": ["Google Chrome"],
    "гугл хром": ["Google Chrome"],
    "браузер": ["Google Chrome", "Safari"],
    "chrome": ["Google Chrome"],
    "сафари": ["Safari"],
    "safari": ["Safari"],
    "ворд": ["Microsoft Word", "LibreOffice", "Google Docs", "TextEdit"],
    "word": ["Microsoft Word", "LibreOffice", "Google Docs", "TextEdit"],
    "либреофис": ["LibreOffice"],
    "libreoffice": ["LibreOffice"],
    "гугл докс": ["Google Docs"],
    "гугл документы": ["Google Docs"],
    "документы": ["Google Docs", "LibreOffice", "TextEdit"],
    "таблицы": ["Google Sheets", "LibreOffice"],
    "эксель": ["Microsoft Excel", "Google Sheets", "LibreOffice"],
    "презентации": ["Google Slides", "LibreOffice"],
    "блокнот": ["TextEdit", "Notes"],
    "текстовый редактор": ["TextEdit"],
    "textedit": ["TextEdit"],
    "заметки": ["Notes"],
    "notes": ["Notes"],
    "обсидиан": ["Obsidian"],
    "obsidian": ["Obsidian"],
    "вс код": ["Visual Studio Code"],
    "вскод": ["Visual Studio Code"],
    "vs code": ["Visual Studio Code"],
    "vscode": ["Visual Studio Code"],
    "код": ["Visual Studio Code", "OpenCode", "Antigravity"],
    "антигравити": ["Antigravity"],
    "antigravity": ["Antigravity"],
    "терминал": ["Warp", "Terminal"],
    "варп": ["Warp"],
    "warp": ["Warp"],
    "стим": ["Steam"],
    "steam": ["Steam"],
    "роблокс": ["Roblox"],
    "roblox": ["Roblox"],
    "майнкрафт": ["Prism Launcher"],
    "призм": ["Prism Launcher"],
    "лоджик": ["Logic Pro"],
    "logic": ["Logic Pro"],
    "обс": ["OBS"],
    "obs": ["OBS"],
    "чатгпт": ["ChatGPT"],
    "чат гпт": ["ChatGPT"],
    "chatgpt": ["ChatGPT"],
    "джемини": ["Gemini"],
    "gemini": ["Gemini"],
    "калькулятор": ["Calculator"],
    "календарь": ["Calendar"],
    "настройки": ["System Settings"],
    "системные настройки": ["System Settings"],
    "музыка": ["Music"],
    "музыку": ["Music"],
    "проводник": ["Finder"],
    "файндер": ["Finder"],
    "finder": ["Finder"],
    "зум": ["zoom.us"],
    "zoom": ["zoom.us"],
    "энидеск": ["AnyDesk"],
    "anydesk": ["AnyDesk"],
}

SITE_ALIASES: dict[str, str] = {
    "ютуб": "https://www.youtube.com",
    "ютьюб": "https://www.youtube.com",
    "youtube": "https://www.youtube.com",
    "гитхаб": "https://github.com",
    "github": "https://github.com",
    "гугл": "https://www.google.com",
    "google": "https://www.google.com",
    "яндекс": "https://ya.ru",
    "почту": "https://mail.google.com",
    "почта": "https://mail.google.com",
    "gmail": "https://mail.google.com",
    "переводчик": "https://translate.google.com",
    "solw": "https://solw.me",
    "solw.me": "https://solw.me",
    "a.solw.me": "https://a.solw.me",
}


def extract_website_url(target: str) -> tuple[str, str] | None:
    """
    Преобразует голосовой запрос на открытие сайта или веб-страницы в (URL, человекопонятное_имя).
    Поддерживает:
    - Алиасы: ютуб, гитхаб, яндекс, гугл, почта, переводчик
    - Домены Danila (solw.me, a.solw.me) с защитой от созвучных опечаток Whisper (solve.me, soft.me, сольв)
    - Прямые адреса и домены: a.solw.me, github.com, t.me, https://...
    - Голосовые точки: «а точка солв точка ми»
    - Явные запросы «открой сайт <любой>»
    """
    raw = target.strip(" .,!?:;")
    clean = re.sub(
        r"\b(?:на\s+компе|на\s+компьютере|на\s+маке|у\s+меня|в\s+браузере|пожалуйста|плиз|быстро|сейчас)\b",
        "",
        raw,
        flags=re.IGNORECASE,
    ).strip(" .,!?:;")

    if not clean:
        return None

    low = clean.lower()

    # Специфический перехват solw.me / a.solw.me с защитой от ошибок ASR (solve.me, soft.me, сольв)
    if any(k in low for k in ("solw", "solve.me", "soft.me", "сольв", "солв")):
        if any(p in low for p in ("a.", "a ", "а.", "а ", "a-")):
            return ("https://a.solw.me", "a.solw.me")
        return ("https://solw.me", "solw.me")

    is_explicit_site = bool(
        re.search(r"\b(?:сайт|веб-?сайт|страниц\w*|ссылк\w*|адрес|вкладк\w*)\b", clean, flags=re.IGNORECASE)
    )
    domain_part = re.sub(
        r"^(?:открой|открыть|перейди\s+на|зайди\s+на)?\s*(?:на\s+)?(?:сайт|веб-?сайт|страницу|страница|ссылку|ссылка|адрес)\s+",
        "",
        clean,
        flags=re.IGNORECASE,
    ).strip(" .,!?:;")

    d_low = domain_part.lower()
    if d_low in SITE_ALIASES:
        return (SITE_ALIASES[d_low], domain_part)

    domain_part_dots = re.sub(r"\s+(?:точка|дот|dot)\s+", ".", domain_part, flags=re.IGNORECASE)
    domain_part_dots = re.sub(
        r"(?<=[a-zA-Z0-9])\s+(?:точка|дот|dot)(?=\s+[a-zA-Z0-9]|$)", ".", domain_part_dots, flags=re.IGNORECASE
    )
    domain_clean = domain_part_dots.replace(" ", "")

    d_clean_low = domain_clean.lower()
    if d_clean_low in SITE_ALIASES:
        return (SITE_ALIASES[d_clean_low], domain_clean)

    if re.match(r"^https?://", domain_clean, flags=re.IGNORECASE):
        return (domain_clean, domain_clean)

    if re.match(r"^[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-\.]+(?::\d+)?(?:/.*)?$", domain_clean):
        return (f"https://{domain_clean}", domain_clean)

    if re.match(r"^localhost(?::\d+)?(?:/.*)?$", domain_clean, flags=re.IGNORECASE):
        return (f"http://{domain_clean}", domain_clean)

    if is_explicit_site and domain_part:
        return (f"https://www.google.com/search?q={urllib.parse.quote(domain_part)}", f"сайт {domain_part}")

    return None

DOC_EDITOR_CANDIDATES = [
    "Microsoft Word",
    "LibreOffice",
    "Google Docs",
    "Pages",
    "TextEdit",
    "Obsidian",
    "Notes",
    "Google Chrome",
    "Safari",
    "Visual Studio Code",
]

DOC_SYSTEM_PROMPT = (
    "Ты помощник, который пишет готовые тексты документов, записок, заявлений, писем и заметок "
    "для вставки в текстовый редактор (Word / Google Docs / Блокнот) на компьютере пользователя. "
    "Пиши ТОЛЬКО сам готовый текст документа на русском языке, с правильными абзацами и переносами строк. "
    "Не пиши никаких вступлений вроде «Вот ваша записка:» или «Конечно, я подготовил текст:», "
    "не используй блоки кода ``` и не используй символы markdown (** или ##)."
)

VISION_VOICE_SYSTEM_PROMPT = (
    "Ты умный голосовой ассистент со зрением, который видит скриншот экрана компьютера Mac пользователя. "
    "Отвечай строго по-русски. Твой ответ пойдёт сразу в синтезатор речи, поэтому категорически запрещены "
    "markdown, звёздочки, списки, блоки кода и спецсимволы. Отвечай ёмко и понятно в 2-4 предложениях:\n"
    "1) Если просят решить задачу, тест или пример на экране — назови правильный ответ и кратко поясни ход решения.\n"
    "2) Если просят объяснить ошибку в коде или терминале — скажи, в чём причина ошибки и как её исправить.\n"
    "3) Если просят перевести или сделать конспект открытого PDF, слайда или страницы — передай перевод или главную суть простыми словами.\n"
    "Не представляйся Алисой и не упоминай Яндекс."
)

VISION_DOC_SYSTEM_PROMPT = (
    "Ты умный помощник со зрением, который видит скриншот экрана компьютера Mac пользователя "
    "и готовит подробный текст (решение задачи, перевод, конспект или исправленный код) для вставки в документ. "
    "Пиши ТОЛЬКО сам готовый текст на русском языке с аккуратными абзацами. "
    "Не используй символы markdown (** или ##) и не пиши лишних вступлений."
)


def _installed_apps() -> dict[str, Path]:
    """Возвращает карту lowercase_stem -> путь к .app."""
    found: dict[str, Path] = {}
    for root in (Path("/Applications"), Path("/System/Applications"), Path("/System/Applications/Utilities")):
        if not root.is_dir():
            continue
        for entry in root.iterdir():
            if entry.suffix == ".app":
                found[entry.stem.lower()] = entry
    return found


def resolve_app(query: str) -> str | None:
    """Находит установленное приложение по русскому или английскому названию."""
    q = query.strip().lower()
    q = re.sub(r"\b(приложение|программу|программа|на компе|на компьютере|на маке|пожалуйста|мне)\b", "", q)
    q = " ".join(q.split()).strip(" .,!?:;\"'«»")
    if not q:
        return None

    installed = _installed_apps()

    # 1. Точное совпадение со словарём алиасов
    for alias, targets in APP_ALIASES.items():
        if q == alias or alias in q.split():
            for target in targets:
                if target.lower() in installed or target == "Finder":
                    return target

    # 2. Вхождение алиаса как подстроки
    for alias, targets in sorted(APP_ALIASES.items(), key=lambda x: len(x[0]), reverse=True):
        if alias in q:
            for target in targets:
                if target.lower() in installed or target == "Finder":
                    return target

    # 3. Прямой поиск по именам в /Applications
    for stem_lower, path in installed.items():
        if q == stem_lower or q in stem_lower:
            return path.stem

    return None


def copy_to_clipboard(text: str) -> None:
    """Копирует UTF-8 текст в системный буфер обмена macOS."""
    env = os.environ.copy()
    env["LANG"] = "en_US.UTF-8"
    env["LC_CTYPE"] = "UTF-8"
    subprocess.run(["pbcopy"], input=text.encode("utf-8"), env=env, check=False, timeout=5)


def read_from_clipboard() -> str:
    """Читает UTF-8 текст из системного буфера обмена macOS."""
    env = os.environ.copy()
    env["LANG"] = "en_US.UTF-8"
    env["LC_CTYPE"] = "UTF-8"
    res = subprocess.run(["pbpaste"], capture_output=True, env=env, check=False, timeout=5)
    if res.returncode != 0 or not res.stdout:
        return ""
    return res.stdout.decode("utf-8", errors="replace").strip()


def get_running_apps() -> list[str]:
    """Возвращает список видимых запущенных приложений в порядке активности."""
    script = (
        'tell application "System Events" to get name of every application process '
        "whose background only is false"
    )
    res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
    if res.returncode != 0 or not res.stdout.strip():
        return []
    return [name.strip() for name in res.stdout.strip().split(",") if name.strip()]


def get_frontmost_app() -> str:
    script = 'tell application "System Events" to get name of first application process whose frontmost is true'
    res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
    return res.stdout.strip() if res.returncode == 0 else ""


def capture_screen_base64() -> tuple[str | None, str]:
    """
    Делает мгновенный бесшумный скриншот экрана Mac, сжимает до 1440px по длинной стороне
    и возвращает (base64_jpeg, имя_активного_приложения).
    """
    import socket

    shot_path = Path("/tmp/assistant_screen.jpg")
    frontmost = get_frontmost_app()
    try:
        shot_path.unlink(missing_ok=True)
        res = subprocess.run(
            ["/usr/sbin/screencapture", "-x", "-t", "jpg", str(shot_path)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode == 0 and shot_path.is_file() and shot_path.stat().st_size >= 1000:
            subprocess.run(
                ["/usr/bin/sips", "-Z", "1440", str(shot_path)],
                capture_output=True,
                text=True,
                timeout=5,
            )
        else:
            # Фоллбэк через сокет screen_helper (у которого уже есть TCC-разрешение Screen Recording)
            sock_path = Path("/tmp/station-screen-helper.sock")
            if sock_path.exists():
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(5.0)
                    s.connect(str(sock_path))
                    resp = s.recv(16)
                    if not resp.startswith(b"OK"):
                        log.warning("screen_helper вернул ошибку: %r", resp)
                        return None, frontmost
            else:
                log.warning("screencapture не удался (code=%s): %s", res.returncode, res.stderr.strip())
                return None, frontmost

        if not shot_path.is_file() or shot_path.stat().st_size < 1000:
            return None, frontmost
        raw = shot_path.read_bytes()
        shot_path.unlink(missing_ok=True)
        return base64.b64encode(raw).decode("ascii"), frontmost
    except Exception as exc:
        log.warning("Ошибка при создании скриншота экрана: %s", exc)
        return None, frontmost


def paste_into_app(target_app: str | None = None) -> tuple[bool, str]:
    """Активирует приложение (если указано) и отправляет физический Cmd+V (key code 9) через System Events."""
    if target_app:
        subprocess.run(["open", "-a", target_app], check=False, timeout=5)
        time.sleep(0.35)

    script = 'tell application "System Events" to key code 9 using command down'
    res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
    if res.returncode == 0:
        return True, target_app or get_frontmost_app() or "текущее окно"
    log.warning("Не удалось отправить Cmd+V через System Events: %s", res.stderr.strip())
    return False, res.stderr.strip()


def write_into_notes(text: str) -> bool:
    """Создаёт новую заметку в штатном приложении Notes (Заметки)."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "<br>")
    script = (
        'tell application "Notes"\n'
        "    activate\n"
        f'    make new note at folder "Notes" with properties {{body:"{escaped}"}}\n'
        "end tell"
    )
    res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=8)
    return res.returncode == 0


def open_in_text_editor_fallback(text: str, prefer_app: str | None = None) -> str:
    """Сохраняет текст в файл в ~/Documents и открывает его на экране в редакторе."""
    docs_dir = Path.home() / "Documents"
    docs_dir.mkdir(parents=True, exist_ok=True)
    doc_path = docs_dir / "Документ_ассистента.txt"
    doc_path.write_text(text, encoding="utf-8")

    installed = _installed_apps()
    if prefer_app and prefer_app.lower() in installed:
        subprocess.run(["open", "-a", prefer_app, str(doc_path)], check=False, timeout=5)
        return prefer_app
    subprocess.run(["open", "-a", "TextEdit", str(doc_path)], check=False, timeout=5)
    return "TextEdit"


def insert_document_text(text: str, user_command: str) -> str:
    """
    Вставляет сгенерированный текст в открытый документ / Word / LibreOffice / Google Docs / Заметки.
    Возвращает краткое голосовое подтверждение для озвучки колонкой.
    """
    copy_to_clipboard(text)
    cmd_lower = user_command.lower()

    # 1. Если явно попросили в Заметки (Notes)
    if "заметк" in cmd_lower or "notes" in cmd_lower:
        if write_into_notes(text):
            return "Готово, создала новую заметку с этим текстом на компьютере."

    running = get_running_apps()
    running_lower = {app.lower(): app for app in running}
    frontmost = get_frontmost_app()

    # 2. Если явно попросили «открой ворд/блокнот/документ и напиши...»
    explicit_open = bool(re.search(r"\b(открой|запусти|создай)\b", cmd_lower))
    if explicit_open:
        prefer = "TextEdit" if ("блокнот" in cmd_lower or "textedit" in cmd_lower) else None
        opened_in = open_in_text_editor_fallback(text, prefer_app=prefer)
        return f"Готово, открыла {opened_in} с готовым текстом и скопировала его в буфер обмена."

    # 3. Определяем уже открытый целевой редактор
    target_app: str | None = None
    if any(w in cmd_lower for w in ("ворд", "word", "документ", "либре", "гугл док")):
        for candidate in (
            "Microsoft Word",
            "LibreOffice",
            "soffice.bin",
            "Google Docs",
            "Pages",
            "TextEdit",
            "Obsidian",
        ):
            if candidate.lower() in running_lower:
                target_app = "LibreOffice" if candidate == "soffice.bin" else running_lower[candidate.lower()]
                break
        if not target_app and frontmost in ("Google Chrome", "Safari"):
            target_app = frontmost
    elif "блокнот" in cmd_lower or "текстовый" in cmd_lower:
        target_app = "TextEdit"
    elif "обсидиан" in cmd_lower or "obsidian" in cmd_lower:
        target_app = "Obsidian"
    elif "телеграм" in cmd_lower or "телегу" in cmd_lower:
        target_app = "Telegram"
    else:
        target_app = frontmost if frontmost and frontmost != "Finder" else None

    # 4. Пробуем вставить напрямую через Cmd+V в активное/целевое окно
    if target_app:
        ok, used_app = paste_into_app(target_app)
        if ok:
            return f"Готово, написала текст в приложении {used_app}."

    # 5. Надёжный фоллбэк: открываем готовый файл в редакторе на экране + оставляем в буфере обмена
    opened_in = open_in_text_editor_fallback(text, prefer_app=target_app)
    return (
        f"Готово, открыла текст в {opened_in} и скопировала его в буфер обмена."
    )


# --- Парсер команд управления компьютером ------------------------------------

_WRITE_PATTERNS = [
    # «у меня на компе открыт ворд напиши в документе типовую записку о уходе со школы»
    re.compile(
        r"(?:напиши|напечатай|вставь|введи|составь|сделай\s+текст|сделай\s+записку|сделай\s+заявление)"
        r"[\s,.:;-]+(?P<rest>.+)",
        re.IGNORECASE | re.DOTALL,
    ),
]

_WRITE_CONTEXT_WORDS = (
    "ворд",
    "word",
    "документ",
    "блокнот",
    "заметк",
    "комп",
    "мак",
    "редактор",
    "текст",
    "записк",
    "заявлен",
    "письм",
    "напечатай",
    "вставь",
)

_OPEN_Pattern = re.compile(
    r"(?:^(?:а\s+)?(?:ты\s+)?(?:можешь|сможешь)?(?:\s+пожалуйста)?(?:\s+мне)?(?:\s+на\s+(?:компе|компьютере|маке))?\s*)?"
    r"\b(?:открой|открыть|запусти|запустить|включи|включить|перейди|перейти|зайди|зайти)(?:\s+мне)?(?:\s+на\s+(?:компе|компьютере|маке))?"
    r"[\s,.:;-]+(?P<target>.+)$",
    re.IGNORECASE,
)

_CLOSE_PATTERN = re.compile(
    r"(?:^(?:а\s+)?(?:ты\s+)?(?:можешь|сможешь)?(?:\s+пожалуйста)?(?:\s+на\s+(?:компе|компьютере|маке))?\s*)?"
    r"\b(?:закрой|закрыть|выключи\s+программу|выйди\s+из)(?:\s+на\s+(?:компе|компьютере|маке))?"
    r"[\s,.:;-]+(?P<target>.+)$",
    re.IGNORECASE,
)

_SEARCH_PATTERN = re.compile(
    r"\b(?:найди|найти|поищи|загугли|загуглить|открой\s+поиск)\s+(?:в\s+(?:браузере|ютубе)|на\s+(?:компе|компьютере|маке))"
    r"[\s,.:;-]+(?P<query>.+)$",
    re.IGNORECASE,
)


def _clean_write_topic(raw_rest: str) -> str:
    """Очищает формулировку темы документа от служебных слов («в документе», «на компе», «в ворде»)."""
    cleaned = re.sub(
        r"\b(?:в\s+документе|в\s+открытом\s+документе|в\s+ворде|в\s+word|в\s+блокноте|"
        r"в\s+заметках|на\s+компе|на\s+компьютере|на\s+маке|туда|там|тут|мне)\b",
        " ",
        raw_rest,
        flags=re.IGNORECASE,
    )
    cleaned = " ".join(cleaned.split()).strip(" .,!?:;")
    return cleaned or raw_rest.strip()


_VISION_EXPLICIT_PHRASES = (
    "на экран",
    "с экрана",
    "в экран",
    "на монитор",
    "с монитора",
    "скриншот",
    "снимок экрана",
    "что открыто",
    "что у меня открыто",
    "что сейчас открыто",
    "что там открыто",
    "что видишь",
    "что ты видишь",
    "посмотри что",
    "глянь что",
    "в этой задаче",
    "в этом задании",
    "в этом тесте",
    "в этом примере",
    "в этом коде",
    "для задач на",
    "в задаче на",
)

_VISION_TASK_PHRASES = (
    "объясни ошибку",
    "в чем ошибка",
    "в чём ошибка",
    "почему ошибка",
    "почему не работает",
    "исправь ошибку",
    "найди ошибку",
    "реши задачу",
    "реши тест",
    "реши пример",
    "реши задание",
    "подскажи решение",
    "подскажи ответ",
    "какой тут ответ",
    "какой правильный ответ",
    "переведи то что",
    "переведи то, что",
    "переведи слайд",
    "переведи текст",
    "переведи страницу",
    "переведи документ",
    "сделай конспект",
    "законспектируй",
)


def _is_vision_command(t_lower: str) -> bool:
    if any(p in t_lower for p in _VISION_EXPLICIT_PHRASES):
        return True
    if any(p in t_lower for p in _VISION_TASK_PHRASES):
        return True
    if re.search(r"\b(посмотри|глянь)\b", t_lower) and any(
        w in t_lower
        for w in ("ошибк", "задач", "тест", "код", "терминал", "перевед", "слайд", "пдф", "pdf", "экран", "комп")
    ):
        return True
    return False


# --- 1. Управление мессенджерами (Telegram, WhatsApp, Discord) ----------------

MESSENGER_PARSE_SYSTEM_PROMPT = (
    "Ты парсер голосовых команд для мессенджеров (Telegram, WhatsApp, Discord).\n"
    "Извлеки из команды пользователя:\n"
    "1) contact — имя контакта или название чата в ИМЕНИТЕЛЬНОМ падеже (например: «с Мишей» -> «Миша», "
    "«Мише» -> «Миша», «с мамой» -> «Мама», «с Даней» -> «Даня», «Артёму» -> «Артём», «с Сашей» -> «Саша»).\n"
    "2) message — текст сообщения, если пользователь просит что-то написать или отправить. "
    "Если сообщение передано в косвенной речи («напиши, что я выхожу»), преобразуй в прямую речь («Я выхожу»). "
    "Если просят только открыть чат без отправки сообщения — оставь message пустой строкой.\n"
    "Ответь СТРОГО одной строкой валидного JSON без markdown:\n"
    '{"contact": "Имя", "message": "Текст"}'
)


def _normalize_russian_name_fallback(raw: str) -> str:
    """Быстрая эвристика приведения русских имён из творительного/дательного падежа к именительному."""
    w = raw.strip(" .,!?:;\"'«»")
    if not w or " " in w:
        return w
    w_low = w.lower()
    # Частые разговорные имена
    direct_map = {
        "мишей": "Миша",
        "мише": "Миша",
        "мамой": "Мама",
        "маме": "Мама",
        "папой": "Папа",
        "папе": "Папа",
        "сашей": "Саша",
        "саше": "Саша",
        "даней": "Даня",
        "дане": "Даня",
        "данилой": "Данила",
        "даниле": "Данила",
        "димой": "Дима",
        "диме": "Дима",
        "катей": "Катя",
        "кате": "Катя",
        "аней": "Аня",
        "ане": "Аня",
        "ваней": "Ваня",
        "ване": "Ваня",
        "ильей": "Илья",
        "ильёй": "Илья",
        "илье": "Илья",
        "никитой": "Никита",
        "никите": "Никита",
        "ксюшей": "Ксюша",
        "ксюше": "Ксюша",
        "настей": "Настя",
        "насте": "Настя",
        "соней": "Соня",
        "соне": "Соня",
        "олей": "Оля",
        "оле": "Оля",
        "юлей": "Юля",
        "юле": "Юля",
        "таней": "Таня",
        "тане": "Таня",
        "леной": "Лена",
        "лене": "Лена",
    }
    if w_low in direct_map:
        return direct_map[w_low]

    if len(w) >= 4:
        if w_low.endswith(("ей", "ею")):
            # Мишей -> Миша (после ш/ж/ч/щ), Катей -> Катя
            stem = w[:-2]
            return stem + ("а" if stem[-1].lower() in "шжчщц" else "я")
        if w_low.endswith(("ой", "ою")):
            return w[:-2] + "а"
        if w_low.endswith(("ом", "ем", "ём")):
            stem = w[:-2]
            return stem
    return w


def _detect_messenger_app(t_lower: str) -> str:
    if any(w in t_lower for w in ("дискорд", "discord")):
        return "Discord"
    if any(w in t_lower for w in ("вотсап", "вацап", "ватсап", "whatsapp")):
        return "WhatsApp"
    if any(w in t_lower for w in ("телеграм", "телегу", "телеге", "тг", "telegram")):
        return "Telegram"
    front = get_frontmost_app()
    if front in ("Telegram", "WhatsApp", "Discord"):
        return front
    return "Telegram"


def open_messenger_chat_and_send(app_name: str, contact: str, message: str = "") -> str:
    """
    Открывает мессенджер (Telegram / WhatsApp / Discord), ищет контакт через глобальный поиск,
    открывает диалог и при наличии message отправляет сообщение.
    """
    running = get_running_apps()
    was_running = app_name in running
    subprocess.run(["open", "-a", app_name], check=False, timeout=5)
    time.sleep(0.45 if was_running else 1.2)

    contact_clean = contact.strip()
    msg_clean = message.strip()

    if contact_clean:
        copy_to_clipboard(contact_clean)
        if app_name == "Discord":
            # В Discord: Cmd+K (key code 40) открывает Quick Switcher -> Cmd+V -> Enter
            script = (
                'tell application "System Events"\n'
                '    key code 53\n'
                '    delay 0.15\n'
                '    key code 40 using command down\n'
                '    delay 0.3\n'
                '    key code 9 using command down\n'
                '    delay 0.55\n'
                '    key code 36\n'
                'end tell'
            )
        elif app_name == "WhatsApp":
            # В WhatsApp: Cmd+F (key code 3) -> Cmd+A -> Cmd+V -> Стрелка вниз -> Enter
            script = (
                'tell application "System Events"\n'
                '    key code 53\n'
                '    delay 0.15\n'
                '    key code 3 using command down\n'
                '    delay 0.25\n'
                '    key code 0 using command down\n'
                '    key code 9 using command down\n'
                '    delay 0.65\n'
                '    key code 125\n'
                '    delay 0.15\n'
                '    key code 36\n'
                'end tell'
            )
        else:
            # В Telegram (Telegram for macOS): Escape x2 сбрасывает открытый чат,
            # затем Cmd+F фокусирует глобальный поиск чатов -> Cmd+V -> Стрелка вниз -> Enter
            script = (
                'tell application "System Events"\n'
                '    key code 53\n'
                '    delay 0.12\n'
                '    key code 53\n'
                '    delay 0.15\n'
                '    key code 3 using command down\n'
                '    delay 0.25\n'
                '    key code 0 using command down\n'
                '    key code 9 using command down\n'
                '    delay 0.7\n'
                '    key code 125\n'
                '    delay 0.18\n'
                '    key code 36\n'
                'end tell'
            )
        subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=8)
        time.sleep(0.45)

    if msg_clean:
        copy_to_clipboard(msg_clean)
        send_script = (
            'tell application "System Events"\n'
            '    key code 9 using command down\n'
            '    delay 0.2\n'
            '    key code 36\n'
            'end tell'
        )
        subprocess.run(["osascript", "-e", send_script], capture_output=True, text=True, timeout=5)
        if contact_clean:
            return f"Открыла чат с {contact_clean} в {app_name} и отправила сообщение."
        return f"Отправила сообщение в {app_name}."

    return f"Открыла чат с {contact_clean} в {app_name}."


_CHAT_OPEN_RE = re.compile(
    r"(?:открой|открыть|найди|найти|включи|включить|покажи)\s+(?:мне\s+)?(?:чат|переписку|диалог|лс|личку)\s+"
    r"(?:с|у)\s+(?P<tail>.+)",
    re.IGNORECASE,
)

_CHAT_SEND_DIRECT_RE = re.compile(
    r"\b(?:напиши|написать|отправь|отправить(?:\s+сообщение)?|ответь|ответить)\s+"
    r"(?:в\s+(?:телеграм[а-я]*|тг|ватсап[а-я]*|вотсап[а-я]*|вацап[а-я]*|дискорд[а-я]*)\s+)?"
    r"(?:пользователю\s+|контакту\s+)?"
    r"(?P<tail>.+)$",
    re.IGNORECASE,
)

_DATIVE_CONTACTS = (
    "маме",
    "мамой",
    "мамы",
    "папе",
    "папой",
    "папы",
    "мише",
    "мишей",
    "дане",
    "даней",
    "даниле",
    "саше",
    "сашей",
    "диме",
    "димой",
    "кате",
    "катей",
    "ане",
    "аней",
    "ване",
    "ваней",
    "илье",
    "никите",
    "ксюше",
    "насте",
    "соне",
    "оле",
    "юле",
    "тане",
    "лене",
    "ему",
    "ей",
)

COMPOSE_REPLY_SYSTEM_PROMPT = (
    "Ты помогаешь пользователю быстро ответить на входящее сообщение в мессенджере (Telegram / WhatsApp / Discord). "
    "Напиши ТОЛЬКО сам готовый текст ответа от первого лица (1 короткое, естественное и дружелюбное предложение на русском языке). "
    "Никаких кавычек, никаких пояснений вроде «Вот ответ:» и никакого markdown."
)


def _try_handle_messenger_command(t: str, t_lower: str, llm) -> str | None:
    """Обрабатывает голосовые команды открытия чатов и отправки/составления сообщений в Telegram/WhatsApp/Discord."""
    # Не перехватываем команды про документы (ворд, блокнот, заметки)
    if any(w in t_lower for w in ("ворд", "word", "документ", "блокнот", "заметк", "записк", "заявлен")):
        return None

    has_chat_word = any(w in t_lower for w in ("чат", "переписк", "диалог", "личк"))
    has_messenger_word = any(
        w in t_lower
        for w in ("телеграм", "телегу", "телеге", " в тг", "ватсап", "вотсап", "вацап", "whatsapp", "дискорд", "discord")
    )
    has_reply_or_contact = bool(
        re.search(r"\b(напиши|написать|отправь|отправить|ответь|ответить)\b", t_lower)
        and (
            any(w in t_lower for w in ("сообщен", "ответ"))
            or any(re.search(rf"\b{c}\b", t_lower) for c in _DATIVE_CONTACTS)
        )
    )

    if not has_chat_word and not has_Reply_or_Messenger(has_messenger_word, has_reply_or_contact, t_lower):
        return None

    m_open = _CHAT_OPEN_RE.search(t)
    m_send = _CHAT_SEND_DIRECT_RE.search(t) if not m_open else None
    if not m_open and not m_send:
        return None

    app_name = _detect_messenger_app(t_lower)
    wants_clipboard_only = any(w in t_lower for w in ("в буфер", "буфер обмен", "скопируй"))
    is_compose_reply_request = bool(
        re.search(
            r"\b(?:ответ\s+на\s+(?:е[её]|его|последн\w+)?\s*сообщен\w*|ответь\s+на\s+(?:последн\w+\s+)?сообщен\w*)\b",
            t_lower,
        )
    )

    contact = ""
    message = ""

    # Пробуем быстрый парсинг через LLM (приводит имя к именительному падежу и отделяет текст сообщения)
    try:
        raw_json = llm.chat_custom(
            system_prompt=MESSENGER_PARSE_SYSTEM_PROMPT,
            user_prompt=t,
            num_predict=80,
        )
        m_json = re.search(r"\{.*\}", raw_json, re.DOTALL)
        if m_json:
            parsed = json.loads(m_json.group(0))
            contact = str(parsed.get("contact") or "").strip()
            message = str(parsed.get("message") or "").strip()
    except Exception as exc:
        log.warning("Не удалось распарсить команду мессенджера через LLM: %s", exc)

    # Надёжный регулярный фоллбэк, если LLM не вернула имя контакта
    if not contact and m_open:
        tail = m_open.group("tail").strip()
        parts = re.split(
            r"\s+(?:и\s+)?(?:напиши|отправь|скажи)(?:\s+ему|\s+ей|\s+туда)?(?:\s+что)?[\s,.:;-]+",
            tail,
            maxsplit=1,
            flags=re.IGNORECASE,
        )
        raw_contact = re.sub(
            r"\b(?:в\s+(?:телеграм[а-я]*|тг|ватсап[а-я]*|вотсап[а-я]*|вацап[а-я]*|дискорд[а-я]*)|на\s+(?:компе|компьютере|маке))\b",
            "",
            parts[0],
            flags=re.IGNORECASE,
        ).strip(" .,!?:;")
        contact = _normalize_russian_name_fallback(raw_contact)
        if len(parts) > 1 and not message:
            message = parts[1].strip(" .,!?:;")
    elif contact:
        contact = _normalize_russian_name_fallback(contact)

    # Если просят придумать/написать ответ на сообщение («напиши маме ответ на её сообщение» / «ответь на сообщение мамы, еду принесли»)
    if is_compose_reply_request:
        img_b64, _ = capture_screen_base64()
        try:
            if img_b64:
                prompt = (
                    f"Запрос пользователя: «{t}».\n"
                    f"Посмотри на открытый на экране чат (контакт: {contact or 'собеседник'}), прочитай последнее входящее сообщение "
                    "и напиши короткий естественный ответ от первого лица (1 предложение без кавычек)."
                )
                composed = llm.chat_vision(
                    system_prompt=COMPOSE_REPLY_SYSTEM_PROMPT,
                    user_prompt=prompt,
                    image_b64=img_b64,
                    num_predict=120,
                )
            else:
                composed = llm.chat_custom(
                    system_prompt=COMPOSE_REPLY_SYSTEM_PROMPT,
                    user_prompt=f"Запрос пользователя: «{t}». Сформулируй короткий естественный ответ от первого лица.",
                    num_predict=120,
                )
            composed_clean = _clean_edited_clipboard_text(composed)
            if composed_clean:
                message = composed_clean
        except Exception as exc:
            log.warning("Не удалось сгенерировать ответ по экрану: %s", exc)

    if not contact and not message:
        return None

    if message:
        copy_to_clipboard(message)

    if wants_clipboard_only and message:
        return f"Скопировала ответ в буфер обмена: {message}"

    log.info("Команда Mac messenger (%s): contact='%s', message='%s'", app_name, contact, message)
    res = open_messenger_chat_and_send(app_name, contact, message)
    if is_compose_reply_request and message:
        return f"{res} Текст ответа: {message}"
    return res


def _try_handle_active_chat_type_command(t: str, t_lower: str, llm) -> str | None:
    """
    Печать/вставка текста в текущее активное окно, чат или IDE (Antigravity, Cursor, Terminal, чат и т.д.):
    - «в чат напиши: используй telegram.org для проверки»
    - «напиши в antigravity: продолжай работу»
    - «отправь в чат: проверено, запускай»
    - «вставь в поле ввода: ...»
    """
    if any(w in t_lower for w in ("ворд", "word", "документ", "блокнот", "заметк", "записк", "заявлен")):
        return None

    # Не перехватываем, если указан мессенджер с конкретным контактом (напр. «в телеграм маме напиши...»)
    if any(w in t_lower for w in ("телеграм", "телегу", "телеге", "ватсап", "дискорд")) and re.search(
        r"\b(маме|папе|брату|сестре|[а-яёa-z]+)\b", t_lower
    ):
        return None

    m = re.search(
        r"(?:^(?:а\s+)?(?:ты\s+)?(?:можешь\s+|пожалуйста\s+)*(?:мне\s+)?)"
        r"(?:напиши|отправь|вставь|напечатай|закинь|скинь)\s+"
        r"(?:в\s+(?:чат|антигравити|antigravity|окно|поле\s+ввода|консоль|терминал)|антигравити|antigravity)"
        r"[\s,.:;-]+(?P<text>.+)$",
        t,
        re.IGNORECASE,
    )
    if not m:
        m = re.search(
            r"(?:^(?:а\s+)?(?:ты\s+)?(?:можешь\s+|пожалуйста\s+)*(?:мне\s+)?)"
            r"(?:в\s+(?:чат|антигравити|antigravity|окно|поле\s+ввода)|антигравити|antigravity)\s+"
            r"(?:напиши|отправь|вставь|напечатай|закинь|скинь)"
            r"[\s,.:;-]+(?P<text>.+)$",
            t,
            re.IGNORECASE,
        )

    if not m:
        return None

    raw_text = m.group("text").strip(" .!?,:;")
    if not raw_text:
        return None

    # Если пользователь сказал «чтобы он использовал...», формулируем от второго лица через LLM
    if re.search(r"\b(?:чтобы\s+(?:он|она|они|ассистент|бот))\b", raw_text, re.IGNORECASE):
        user_prompt = (
            f"Пользователь диктует указание для ИИ-ассистента в чате: «{raw_text}».\n"
            "Сформулируй готовое чёткое указание от лица пользователя для чата (1-2 предложения).\n"
            "Отвечай ТОЛЬКО готовым текстом сообщения без кавычек."
        )
        msg_text = llm.chat_custom(
            system_prompt="Ты преобразуешь косвенную просьбу пользователя в прямое обращение/команду для чата. Отвечай только готовым текстом.",
            user_prompt=user_prompt,
            num_predict=150,
        ).strip().strip('"').strip("'")
    else:
        msg_text = raw_text

    copy_to_clipboard(msg_text)
    front_app = get_frontmost_app() or "чат"
    paste_into_app(front_app)

    is_send = any(w in t_lower for w in ("отправь", "пошли", "закинь"))
    if is_send:
        time.sleep(0.25)
        subprocess.run(["osascript", "-e", 'tell application "System Events" to key code 36'], check=False)
        return f"Отправила в {front_app}: {msg_text}"

    return f"Вставила в {front_app}: {msg_text}"


def has_Reply_or_Messenger(has_messenger_word: bool, has_reply_or_contact: bool, t_lower: str) -> bool:
    if has_reply_or_contact:
        return True
    if has_messenger_word and re.search(r"\b(напиши|отправь|ответь|сообщение|чат|переписк|диалог|с\s+[а-яёa-z]+)\b", t_lower):
        return True
    return False


# --- 2. YouTube с автозапуском на весь экран и голосовая пауза/управление -----

def _find_first_youtube_video_id(query: str) -> str | None:
    """Находит videoId первого видео в выдаче YouTube без API-ключа."""
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
        }
        resp = httpx.get(
            "https://www.youtube.com/results",
            params={"search_query": query},
            headers=headers,
            timeout=4.5,
            follow_redirects=True,
        )
        if resp.status_code == 200:
            m = re.search(r'"videoRenderer":\{"videoId":"([a-zA-Z0-9_-]{11})"', resp.text)
            if m:
                return m.group(1)
            m2 = re.search(r'"/watch\?v=([a-zA-Z0-9_-]{11})"', resp.text)
            if m2:
                return m2.group(1)
    except Exception as exc:
        log.warning("Не удалось получить первый videoId YouTube для '%s': %s", query, exc)
    return None


def _fullscreen_youtube_after_load(delay_sec: float = 3.2) -> None:
    """В фоне ждёт загрузки плеера YouTube и нажимает физическую клавишу F (key code 3) для полного экрана."""
    time.sleep(delay_sec)
    front = get_frontmost_app()
    if front in ("Google Chrome", "Safari"):
        # Нажимаем физическую клавишу F (key code 3) — штатный хоткей полноэкранного режима YouTube
        subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to key code 3'],
            capture_output=True,
            text=True,
            timeout=5,
        )


def play_youtube_video(query: str, fullscreen: bool = True) -> str:
    """Находит первое видео на YouTube по запросу, сразу запускает его и разворачивает на весь экран."""
    q_clean = re.sub(
        r"\b(?:на\s+компе|на\s+компьютере|на\s+маке|на\s+весь\s+экран|в\s+полном\s+экране|на\s+ютубе|в\s+ютубе|youtube)\b",
        " ",
        query,
        flags=re.IGNORECASE,
    )
    q_clean = " ".join(q_clean.split()).strip(" .,!?:;") or query.strip()

    video_id = _find_first_youtube_video_id(q_clean)
    if video_id:
        watch_url = f"https://www.youtube.com/watch?v={video_id}"
        subprocess.run(["open", watch_url], check=False, timeout=5)
        if fullscreen:
            threading.Thread(target=_fullscreen_youtube_after_load, daemon=True).start()
        return f"Включаю на Ютубе видео про {q_clean}."

    search_url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(q_clean)}"
    subprocess.run(["open", search_url], check=False, timeout=5)
    return f"Открыла поиск видео {q_clean} на Ютубе."


def _get_target_browser(t_lower: str = "") -> str:
    if "сафари" in t_lower or "safari" in t_lower:
        return "Safari"
    if "хром" in t_lower or "chrome" in t_lower:
        return "Google Chrome"
    front = get_frontmost_app()
    if front in ("Google Chrome", "Safari"):
        return front
    running = get_running_apps()
    if "Google Chrome" in running:
        return "Google Chrome"
    if "Safari" in running:
        return "Safari"
    return "Google Chrome"


_YT_PLAY_RE = re.compile(
    r"\b(?:включи|включить|запусти|запустить|поставь|поставить|открой|открыть|найди|найти|покажи)(?:\s+мне)?(?:\s+на\s+(?:компе|компьютере|маке))?"
    r"(?:\s+на\s+(?:ютубе|ютьюбе|youtube))?\s+"
    r"(?:(?:новое|последнее|свежее|первое|любое|крутое|интересное)\s+)?"
    r"(?:видео|ролик|клип|видос)(?:\s+про|\s+о|\s+об|\s+на\s+тему|\s+у|\s+от)?[\s,.:;-]+(?P<query>.+)$",
    re.IGNORECASE,
)

_YT_PLAY_DIRECT_RE = re.compile(
    r"\b(?:включи|включить|запусти|запустить|поставь|поставить)(?:\s+мне)?(?:\s+на\s+(?:компе|компьютере|маке))?"
    r"\s+(?:на\s+(?:ютубе|ютьюбе|youtube)|в\s+(?:ютубе|ютьюбе|youtube))[\s,.:;-]+(?P<query>.+)$",
    re.IGNORECASE,
)


def _try_handle_youtube_and_media_command(t: str, t_lower: str) -> str | None:
    """Обрабатывает автозапуск видео на YouTube и голосовое управление плеером (пауза, продолжить, полный экран)."""
    # 1. Автозапуск видео по запросу («включи на компе видео про...», «включи на ютубе...»)
    m_yt = _YT_PLAY_RE.search(t) or _YT_PLAY_DIRECT_RE.search(t)
    if m_yt:
        query = m_yt.group("query").strip(" .,!?:;")
        log.info("Команда Mac youtube_autoplay: '%s'", query)
        return play_youtube_video(query, fullscreen=True)

    # 2. Поставить видео на паузу / продолжить воспроизведение
    if any(
        p in t_lower
        for p in (
            "поставь на паузу",
            "поставь видео на паузу",
            "пауза на компе",
            "останови видео",
            "тормозни видео",
            "сними с паузы",
            "продолжи видео",
            "включи видео обратно",
            "продолжи воспроизведение",
        )
    ) or t_lower in ("пауза", "на паузу", "продолжай"):
        browser = _get_target_browser(t_lower)
        subprocess.run(["open", "-a", browser], check=False, timeout=5)
        time.sleep(0.2)
        # Нажимаем физическую клавишу K (key code 40) — универсальная пауза/плей в YouTube
        subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to key code 40'],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if any(w in t_lower for w in ("сними", "продолж", "обратно")):
            return "Продолжаю воспроизведение видео."
        return "Поставила видео на паузу."

    # 3. Развернуть видео на весь экран / выйти из полного экрана
    if any(p in t_lower for p in ("на весь экран", "полный экран", "полноэкранный режим")) and any(
        v in t_lower for v in ("сделай", "разверни", "включи", "видео", "экран")
    ):
        browser = _get_target_browser(t_lower)
        subprocess.run(["open", "-a", browser], check=False, timeout=5)
        time.sleep(0.2)
        subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to key code 3'],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return "Развернула видео на весь экран."

    if any(p in t_lower for p in ("выйди из полного экрана", "сверни полный экран", "обычный экран")):
        browser = _get_target_browser(t_lower)
        subprocess.run(["open", "-a", browser], check=False, timeout=5)
        time.sleep(0.2)
        subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to key code 53'],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return "Вышла из полноэкранного режима."

    # 4. Следующее видео / перемотка
    if any(p in t_lower for p in ("следующее видео", "переключи видео", "другое видео")):
        browser = _get_target_browser(t_lower)
        subprocess.run(["open", "-a", browser], check=False, timeout=5)
        time.sleep(0.2)
        subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to key code 45 using shift down'],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return "Переключила на следующее видео."

    return None


# --- 3. Управление вкладками браузера (Chrome / Safari) и чтение страницы -----

def _get_browser_active_tab_info(browser: str) -> tuple[str, str]:
    """Возвращает (title, url) активной вкладки Chrome или Safari."""
    if browser == "Safari":
        script = (
            'tell application "Safari"\n'
            '    if (count of windows) > 0 then\n'
            '        return (name of current tab of front window) & "|||" & (URL of current tab of front window)\n'
            '    end if\n'
            'end tell'
        )
    else:
        script = (
            'tell application "Google Chrome"\n'
            '    if (count of windows) > 0 then\n'
            '        return (title of active tab of front window) & "|||" & (URL of active tab of front window)\n'
            '    end if\n'
            'end tell'
        )
    res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
    if res.returncode == 0 and "|||" in res.stdout:
        title, url = res.stdout.strip().split("|||", 1)
        return title.strip(), url.strip()
    return "", ""


def _get_browser_tab_content_applescript(browser: str) -> tuple[str, str, str]:
    """
    Извлекает (title, url, text) активной вкладки через AppleScript.
    В Safari использует встроенное свойство `text of current tab`.
    В Chrome использует нативные команды `select all` + `copy selection` (с восстановлением буфера).
    """
    if browser == "Safari":
        script = """
        tell application "Safari"
            if (count of windows) > 0 then
                set curTab to current tab of front window
                set pageTitle to name of curTab
                set pageURL to URL of curTab
                set pageText to text of curTab
                return pageTitle & "|||" & pageURL & "|||" & pageText
            end if
        end tell
        """
        try:
            res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=6)
            if res.returncode == 0 and "|||" in res.stdout:
                parts = res.stdout.strip().split("|||", 2)
                t = parts[0].strip()
                u = parts[1].strip() if len(parts) > 1 else ""
                txt = parts[2].strip() if len(parts) > 2 else ""
                return t, u, txt
        except Exception as exc:
            log.warning("Ошибка чтения Safari по AppleScript: %s", exc)
        return "", "", ""

    # Google Chrome
    old_clip = read_from_clipboard()
    script = """
    tell application "Google Chrome"
        if (count of windows) > 0 then
            set curTab to active tab of front window
            set pageTitle to title of curTab
            set pageURL to URL of curTab
            try
                set pageText to (execute curTab javascript "document.body.innerText")
            on error
                select all curTab
                copy selection curTab
                delay 0.1
                set pageText to the clipboard
            end try
            return pageTitle & "|||" & pageURL & "|||" & pageText
        end if
    end tell
    """
    try:
        res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=6)
        content = res.stdout.strip()
        copy_to_clipboard(old_clip)  # восстанавливаем исходный буфер пользователя
        if res.returncode == 0 and "|||" in content:
            parts = content.split("|||", 2)
            t = parts[0].strip()
            u = parts[1].strip() if len(parts) > 1 else ""
            txt = parts[2].strip() if len(parts) > 2 else ""
            return t, u, txt
    except Exception as exc:
        log.warning("Ошибка чтения Chrome по AppleScript: %s", exc)
        copy_to_clipboard(old_clip)

    return "", "", ""


_TAB_NUM_KEYCODES: dict[str, tuple[int, int]] = {
    "перв": (1, 18),
    "1": (1, 18),
    "втор": (2, 19),
    "2": (2, 19),
    "треть": (3, 20),
    "3": (3, 20),
    "четверт": (4, 21),
    "четвёрт": (4, 21),
    "4": (4, 21),
    "пят": (5, 23),
    "5": (5, 23),
    "шест": (6, 22),
    "6": (6, 22),
    "седьм": (7, 26),
    "7": (7, 26),
    "восьм": (8, 28),
    "8": (8, 28),
    "девят": (9, 25),
    "последн": (9, 25),
}


def _try_handle_browser_command(t: str, t_lower: str, llm) -> str | None:
    """
    Управление вкладками браузера (Google Chrome / Safari):
    закрыть вкладку, переключить вкладку, новая вкладка, вернуть закрытую вкладку,
    открыть историю/загрузки, прочитать вслух или пересказать открытую страницу.
    """
    browser = _get_target_browser(t_lower)

    # 1. Закрыть текущую вкладку (Cmd+W)
    if any(
        p in t_lower
        for p in (
            "закрой вкладку",
            "закрой эту вкладку",
            "закрой текущую вкладку",
            "закрыть вкладку",
            "закрыть эту вкладку",
            "закрой страницу",
            "закрой эту страницу",
            "закрыть страницу",
        )
    ):
        if browser == "Safari":
            script = 'tell application "Safari" to close current tab of front window'
        else:
            script = 'tell application "Google Chrome" to close active tab of front window'
        res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=3)
        if res.returncode != 0:
            subprocess.run(["open", "-a", browser], check=False, timeout=3)
            time.sleep(0.15)
            subprocess.run(
                ["osascript", "-e", 'tell application "System Events" to key code 13 using command down'],
                check=False,
                timeout=3,
            )
        return "Закрыла вкладку в браузере."

    # 2. Вернуть / восстановить закрытую вкладку
    if any(
        p in t_lower
        for p in ("верни вкладку", "верни закрытую вкладку", "восстанови вкладку", "открой закрытую вкладку")
    ):
        subprocess.run(["open", "-a", browser], check=False, timeout=5)
        time.sleep(0.25)
        subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to key code 17 using {command down, shift down}'],
            check=False,
            timeout=5,
        )
        return "Вернула закрытую вкладку."

    # 3. Новая вкладка
    if any(p in t_lower for p in ("новая вкладка", "новую вкладку", "создай вкладку")):
        subprocess.run(["open", "-a", browser], check=False, timeout=5)
        time.sleep(0.25)
        subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to key code 17 using command down'],
            check=False,
            timeout=5,
        )
        return "Открыла новую вкладку в браузере."

    # 4. Переключение на следующую вкладку (Ctrl+Tab)
    if any(
        p in t_lower
        for p in (
            "следующую вкладку",
            "следующая вкладка",
            "переключись на следующую вкладку",
            "переключи на следующую вкладку",
            "перейди на следующую вкладку",
            "соседнюю вкладку",
            "соседняя вкладка",
            "переключи вкладку",
            "переключись на другую вкладку",
        )
    ):
        subprocess.run(["open", "-a", browser], check=False, timeout=3)
        time.sleep(0.15)
        subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to key code 48 using control down'],
            check=False,
            timeout=3,
        )
        return "Переключила на следующую вкладку."

    # 4b. Переключение вкладок (предыдущая / по номеру)
    if "вкладк" in t_lower:
        for prefix, (tab_num, key_code) in _TAB_NUM_KEYCODES.items():
            if prefix in t_lower:
                subprocess.run(["open", "-a", browser], check=False, timeout=5)
                time.sleep(0.2)
                subprocess.run(
                    [
                        "osascript",
                        "-e",
                        f'tell application "System Events" to key code {key_code} using command down',
                    ],
                    check=False,
                    timeout=5,
                )
                return f"Переключила на вкладку номер {tab_num}."

        if any(w in t_lower for w in ("предыдущ", "назад", "влево", "прошл")):
            subprocess.run(["open", "-a", browser], check=False, timeout=5)
            time.sleep(0.2)
            subprocess.run(
                [
                    "osascript",
                    "-e",
                    'tell application "System Events" to key code 48 using {control down, shift down}',
                ],
                check=False,
                timeout=5,
            )
            return "Переключила на предыдущую вкладку."

    # 5. Открыть историю или загрузки браузера
    if any(
        p in t_lower
        for p in ("открой историю", "историю браузера", "покажи историю", "история в браузере")
    ):
        subprocess.run(["open", "-a", browser], check=False, timeout=5)
        time.sleep(0.25)
        subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to key code 16 using command down'],
            check=False,
            timeout=5,
        )
        return f"Открыла историю в {browser}."

    if any(
        p in t_lower
        for p in ("загрузки в браузере", "загрузки в хроме", "загрузки в сафари", "историю загрузок")
    ):
        subprocess.run(["open", "-a", browser], check=False, timeout=5)
        time.sleep(0.25)
        subprocess.run(
            [
                "osascript",
                "-e",
                'tell application "System Events" to key code 37 using {command down, option down}',
            ],
            check=False,
            timeout=5,
        )
        return f"Открыла загрузки в {browser}."

    # 6. Кратко пересказать открытую статью / страницу (через AppleScript)
    if any(
        p in t_lower
        for p in (
            "кратко перескажи открытую статью",
            "кратко перескажи открытую страницу",
            "кратко перескажи статью",
            "кратко перескажи страницу",
            "кратко перескажи что открыто",
            "кратко перескажи",
            "перескажи открытую статью",
            "перескажи открытую страницу",
            "перескажи статью",
            "перескажи страницу",
            "перескажи эту статью",
            "перескажи эту страницу",
            "перескажи что открыто",
            "прочитай страницу",
            "прочитай вслух",
            "прочитай статью",
            "прочитай что открыто",
            "о чем эта страница",
            "о чём эта страница",
            "о чем эта статья",
            "о чём эта статья",
            "что написано на странице",
            "что написано в статье",
            "суть открытой страницы",
            "суть статьи",
        )
    ):
        subprocess.run(["open", "-a", browser], check=False, timeout=3)
        time.sleep(0.2)
        title, url, page_text = _get_browser_tab_content_applescript(browser)
        log.info("AppleScript чтение вкладки (%s): title='%s', text_len=%d", browser, title, len(page_text))

        if page_text and len(page_text.strip()) > 40:
            prompt = (
                f"Перед тобой текст веб-страницы или статьи, открытой в браузере пользователя ({browser}).\n"
                f"Заголовок: {title}\n"
                f"URL: {url}\n\n"
                f"Текст со страницы:\n{page_text[:6500]}\n\n"
                "Твоя задача: Кратко и понятно перескажи главную суть открытой статьи или страницы в 2-4 простых предложениях на русском языке.\n"
                "Ответ пойдёт в голосовой ассистент (TTS), поэтому говори естественно, без спецсимволов, звездочек, markdown и ссылок."
            )
            messages = [{"role": "user", "content": prompt}]
            try:
                answer = llm.chat(messages)
                if answer:
                    copy_to_clipboard(answer)
                    return answer
            except Exception as exc:
                log.warning("Ошибка генерации пересказа страницы LLM: %s", exc)

        # Fallback на Vision, если AppleScript не дал текста (например, графический PDF/холст)
        img_b64, _ = capture_screen_base64()
        if img_b64:
            meta = f"Открыта вкладка «{title}» ({url}). " if title else ""
            user_prompt = (
                f"{meta}Пользователь просит: «{t}».\n"
                "Прочитай содержимое открытой страницы на скриншоте и понятно перескажи вслух главную суть "
                "в 2-4 предложениях на русском языке без спецсимволов."
            )
            answer = llm.chat_vision(
                system_prompt=VISION_VOICE_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                image_b64=img_b64,
                num_predict=240,
            )
            copy_to_clipboard(answer)
            return answer

        return f"Не удалось прочитать содержимое открытой страницы в {browser}."

    return None


# --- 4. Умный буфер обмена (Clipboard AI) ------------------------------------

CLIPBOARD_EDIT_SYSTEM_PROMPT = (
    "Ты профессиональный редактор и корректор текста. "
    "Твоя задача: взять исходный текст и вернуть строго исправленный/отредактированный вариант без ошибок. "
    "КРИТИЧЕСКОЕ ТРЕБОВАНИЕ: "
    "Результат сразу вставляется в документ пользователя. "
    "Выведи ТОЛЬКО сам чистый исправленный текст от первого до последнего слова. "
    "КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО: "
    "- Писать любые комментарии, пояснения или вводные слова (например: «Скопированный текст...», «Я исправил...», «Вот вариант:»). "
    "- Писать заключительные фразы (например: «Теперь вы можете отправить...»). "
    "- Оборачивать текст в кавычки или блоки кода ```. "
    "Только сам текст и ничего кроме текста."
)

CLIPBOARD_VOICE_SYSTEM_PROMPT = (
    "Ты умный голосовой ассистент, который работает с текстом из буфера обмена компьютера пользователя. "
    "Отвечай строго по-русски. Твой ответ пойдёт сразу в синтезатор речи, поэтому категорически запрещены "
    "markdown, звёздочки, списки и спецсимволы. Отвечай ёмко и понятно в 2-4 предложениях."
)


def _clean_edited_clipboard_text(text: str) -> str:
    """Удаляет из сгенерированного текста случайные комментарии модели и лишнее обрамление."""
    cleaned = (text or "").strip()
    if cleaned.startswith("```") and cleaned.endswith("```"):
        lines = cleaned.splitlines()
        if len(lines) >= 2:
            cleaned = "\n".join(lines[1:-1]).strip()
    if (cleaned.startswith("«") and cleaned.endswith("»")) or (cleaned.startswith('"') and cleaned.endswith('"')):
        cleaned = cleaned[1:-1].strip()
    cleaned = re.sub(
        r"^(?:(?:Вот|Ниже|Здесь)\s+(?:исправленный|готовый|отредактированный)\s+(?:текст|вариант)|"
        r"Исправленный\s+текст|Отредактированный\s+текст|Готовый\s+вариант|Результат|Текст)[\s:—–-]+\n*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    return cleaned

_CLIPBOARD_TRIGGERS = (
    "буфер обмена",
    "буфера обмена",
    "в буфере",
    "из буфера",
    "что я скопировал",
    "что я скопировала",
    "то что скопировал",
    "то, что скопировал",
    "то что я скопировал",
    "то, что я скопировал",
    "скопированный текст",
    "скопированное",
    "скопировал",
    "скопировала",
)


def _try_handle_clipboard_command(t: str, t_lower: str, llm) -> str | None:
    """
    Умный буфер обмена (Clipboard AI):
    - «Алиса, переведи то, что я скопировал»
    - «Исправь ошибки в тексте из буфера обмена и вставь обратно»
    - «Кратко перескажи скопированный текст»
    """
    # Не перехватываем команды записи/копирования последнего ответа ассистента В буфер обмена
    if re.search(
        r"\b(?:добавь|скопируй|положи|сохрани|запиши)\b.*\b(?:в\s+буфер|буфер\s+обмен\w*)\b",
        t_lower,
    ):
        return None

    is_clip = any(p in t_lower for p in _CLIPBOARD_TRIGGERS) or (
        "исправь ошибки" in t_lower and any(w in t_lower for w in ("текст", "вставь", "обратно"))
    )
    if not is_clip:
        return None

    log.info("Команда Mac clipboard_ai: '%s'", t)
    clip_text = read_from_clipboard()
    if not clip_text:
        return "В буфере обмена сейчас нет текста. Сначала скопируйте нужный фрагмент через Command C."

    clip_trimmed = clip_text[:6000]
    wants_paste_back = any(
        p in t_lower
        for p in ("вставь обратно", "вставь туда", "и вставь", "замени текст", "вставь в документ")
    )
    wants_edit = any(
        p in t_lower
        for p in ("исправь", "отредактируй", "перепиши", "улучши", "сделай красиво", "без ошибок")
    )

    # Режим 1: Исправление ошибок / редактирование / перевод со вставкой обратно
    if wants_edit or wants_paste_back:
        user_prompt = (
            f"Задача: {t}\n\n"
            f"Исходный текст:\n{clip_trimmed}\n\n"
            "Выведи ТОЛЬКО чистый готовый текст без комментариев:"
        )
        edited_raw = llm.chat_custom(
            system_prompt=CLIPBOARD_EDIT_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            num_predict=650,
        )
        edited = _clean_edited_clipboard_text(edited_raw)
        copy_to_clipboard(edited)
        if wants_paste_back:
            ok, used_app = paste_into_app()
            if ok:
                return f"Готово, обработала текст из буфера обмена и вставила обратно в {used_app}."
        return "Готово, исправила текст и сохранила его обратно в буфер обмена — можете вставлять через Command V."

    # Режим 2: Перевод / краткий пересказ / объяснение скопированного текста вслух
    user_prompt = (
        f"Пользователь просит голосом по поводу скопированного в буфер обмена текста: «{t}».\n\n"
        f"Скопированный текст:\n{clip_trimmed}\n\n"
        "Выполни просьбу пользователя (переведи, кратко перескажи или объясни суть) "
        "в 2-4 предложениях для озвучки вслух без спецсимволов."
    )
    answer = llm.chat_custom(
        system_prompt=CLIPBOARD_VOICE_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        num_predict=250,
    )
    # Если просили именно перевод («переведи в буфере»), сохраняем в буфер, иначе оставляем исходный текст пользователя
    if "переведи" in t_lower:
        copy_to_clipboard(answer)
    return answer


def try_handle_mac_command(text: str, llm) -> str | None:
    """
    Проверяет, является ли запрос голосовой командой для управления Mac.
    Если да — выполняет действие и возвращает короткий ответ для озвучивания колонкой.
    Если это обычный вопрос — возвращает None.
    """
    t = " ".join(text.split()).strip()
    if not t:
        return None
    t_lower = t.lower()

    # 0. Управление звонком в Discord: демка (демонстрация экрана), микрофон, вебка, звук
    from .discord_tools import try_handle_discord_call_command
    from .system_tools import try_handle_system_command

    ds_res = try_handle_discord_call_command(t, t_lower, llm=llm)
    if ds_res:
        return ds_res

    # 0a. Вкладки браузера (Chrome / Safari): закрыть/переключить вкладку, история/загрузки, пересказ страницы
    browser_res = _try_handle_browser_command(t, t_lower, llm)
    if browser_res:
        return browser_res

    # 0b. Системное управление Mac: громкость, заряд батареи, блокировка/сон, скриншоты и папки
    sys_res = try_handle_system_command(t, t_lower)
    if sys_res:
        return sys_res

    # 0b. YouTube с автозапуском на весь экран и голосовая пауза/управление видео
    yt_res = _try_handle_youtube_and_media_command(t, t_lower)
    if yt_res:
        return yt_res

    # 0c. Мессенджеры (Telegram / WhatsApp / Discord): поиск контакта, открытие чата и отправка сообщения
    msg_res = _try_handle_messenger_command(t, t_lower, llm)
    if msg_res:
        return msg_res

    # 0c2. Прямая печать / вставка в открытый чат или IDE (Antigravity, Cursor, активное окно)
    chat_type_res = _try_handle_active_chat_type_command(t, t_lower, llm)
    if chat_type_res:
        return chat_type_res

    # 0d. Умный буфер обмена (Clipboard AI: переведи скопированное, исправь ошибки в буфере и вставь обратно, перескажи)
    clip_res = _try_handle_clipboard_command(t, t_lower, llm)
    if clip_res:
        return clip_res

    # 0e. «Зрение» компьютера (анализ экрана голосом: задачи, ошибки в коде/терминале, перевод PDF/слайдов)
    if _is_vision_command(t_lower):
        log.info("Команда Mac screen_vision: '%s'", t)
        img_b64, front_app = capture_screen_base64()
        if not img_b64:
            return (
                "Не удалось сделать снимок экрана. Проверьте, что у терминала есть разрешение "
                "на запись экрана в настройках мака."
            )

        app_ctx = f"Активное окно сейчас: {front_app}. " if front_app else ""

        # Проверка 1: Просьба написать/отправить текст в открытый чат или IDE (Antigravity, Cursor, Terminal и т.д.)
        wants_chat_insert = bool(
            re.search(r"\b(напиши|вставь|отправь|введи|напечатай|закинь|скинь)\b", t_lower)
            and any(w in t_lower for w in ("чат", "антигравити", "antigravity", "окно", "поле ввода", "консоль", "терминал"))
        )

        if wants_chat_insert:
            user_prompt = (
                f"{app_ctx}Пользователь смотрит на свой экран и просит написать/отправить сообщение в открытый чат (Antigravity, IDE или редактор): «{t}».\n"
                "Сформулируй ТОЧНЫЙ, готовый текст сообщения или запроса для ассистента в чате с учётом того, что открыто на экране.\n"
                "Отвечай ТОЛЬКО текстом, который нужно вставить в чат, без кавычек, вводных слов, пояснений и мета-комментариев."
            )
            chat_text = llm.chat_vision(
                system_prompt="Ты преобразуешь голосовую команду пользователя по содержимому экрана в точный готовый текст для отправки в чат проекта. Отвечай только готовым текстом.",
                user_prompt=user_prompt,
                image_b64=img_b64,
                num_predict=350,
            ).strip().strip('"').strip("'")

            copy_to_clipboard(chat_text)
            paste_into_app(front_app)
            if any(w in t_lower for w in ("отправь", "пошли", "закинь")):
                time.sleep(0.25)
                subprocess.run(["osascript", "-e", 'tell application "System Events" to key code 36'], check=False)
                return f"Отправила в чат: {chat_text}"
            return f"Вставила в чат: {chat_text}"

        wants_doc_insert = bool(
            re.search(r"\b(напиши|вставь|сохрани|запиши)\b", t_lower)
            and any(w in t_lower for w in ("ворд", "word", "документ", "блокнот", "заметк", "редактор"))
        )

        if wants_doc_insert:
            user_prompt = (
                f"{app_ctx}Запрос пользователя по содержимому экрана: «{t}».\n"
                "Подготовь полный готовый текст (решение, перевод или конспект) по скриншоту для вставки в документ."
            )
            doc_text = llm.chat_vision(
                system_prompt=VISION_DOC_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                image_b64=img_b64,
                num_predict=650,
            )
            return insert_document_text(doc_text, t)

        wants_detailed_vision = any(
            p in t_lower
            for p in (
                "подробн",
                "детальн",
                "подумай хорошо",
                "подумай глубоко",
                "шаг за шагом",
                "разбери",
                "объясни решение",
                "почему",
            )
        )
        if wants_detailed_vision:
            user_prompt = (
                f"{app_ctx}Пользователь просит подробно разобрать содержимое экрана: «{t}».\n"
                "Внимательно проанализируй скриншот, разбери задачу или ошибку по шагам и дай полный, "
                "развёрнутый ответ для озвучки вслух (5-8 предложений связным текстом без спецсимволов)."
            )
            v_tokens = 650
        else:
            user_prompt = (
                f"{app_ctx}Пользователь спрашивает голосом про то, что сейчас на экране: «{t}».\n"
                "Проанализируй скриншот и дай чёткий, полезный ответ для озвучки вслух (2-4 предложения без спецсимволов)."
            )
            v_tokens = 240
        answer = llm.chat_vision(
            system_prompt=VISION_VOICE_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            image_b64=img_b64,
            num_predict=v_tokens,
        )
        # Заодно копируем ответ/решение в буфер обмена Mac, чтобы его можно было сразу вставить через Cmd+V
        copy_to_clipboard(answer)
        return answer

    # 1. Набор / вставка текста в документ или приложение на компьютере
    for pat in _WRITE_PATTERNS:
        m = pat.search(t)
        if m and any(w in t_lower for w in _WRITE_CONTEXT_WORDS):
            raw_topic = m.group("rest")
            topic = _clean_write_topic(raw_topic)
            log.info("Команда Mac write_text: тема='%s' (исходный запрос='%s')", topic, t)

            prompt = (
                f"Напиши готовый, полный и грамотно оформленный текст по запросу: «{topic}».\n"
                "Выведи только сам текст документа без лишних комментариев."
            )
            doc_text = llm.chat_custom(
                system_prompt=DOC_SYSTEM_PROMPT,
                user_prompt=prompt,
                num_predict=650,
            )
            return insert_document_text(doc_text, t)

    # 2. Поиск в браузере / на YouTube на компе
    m_search = _SEARCH_PATTERN.match(t)
    if m_search:
        query = m_search.group("query").strip(" .,!?:;")
        if "ютуб" in t_lower or "youtube" in t_lower:
            return play_youtube_video(query, fullscreen=True)
        url = f"https://www.google.com/search?q={urllib.parse.quote(query)}"
        subprocess.run(["open", url], check=False, timeout=5)
        return f"Открыла поиск {query} в браузере на компьютере."

    # 3. Открытие приложения или сайта
    m_open = _OPEN_Pattern.match(t)
    if m_open:
        target_raw = m_open.group("target").strip(" .,!?:;")
        # Если в той же фразе просят «открой ворд и напиши ...» — она уже обработана в п. 1
        target_clean = re.sub(r"\b(на компе|на компьютере|на маке|у меня)\b", "", target_raw, flags=re.IGNORECASE).strip(" .,!?:;")

        # Проверяем сайты, URL и веб-страницы (a.solw.me, youtube.com, github.com, сайт ..., https://...)
        site_info = extract_website_url(target_clean)
        if site_info:
            site_url, site_label = site_info
            log.info("Команда Mac open_site: '%s' -> '%s'", target_clean, site_url)
            subprocess.run(["open", site_url], check=False, timeout=5)
            return f"Открываю {site_label} на компьютере."

        app_name = resolve_app(target_clean)
        if app_name:
            log.info("Команда Mac open_app: '%s' -> '%s'", target_clean, app_name)
            subprocess.run(["open", "-a", app_name], check=False, timeout=5)
            return f"Открываю {app_name} на компьютере."

        # Если не приложение и не сайт — проверяем пользовательские папки
        from .system_tools import find_user_folder
        folder = find_user_folder(target_clean)
        if folder:
            log.info("Команда Mac open_folder: '%s' -> '%s'", target_clean, folder)
            subprocess.run(["open", str(folder)], check=False, timeout=5)
            return f"Открываю папку {folder.name} на компьютере."

    # 4. Закрытие приложения
    m_close = _CLOSE_PATTERN.match(t)
    if m_close:
        target_raw = m_close.group("target").strip(" .,!?:;")
        app_name = resolve_app(target_raw)
        if app_name and app_name != "Finder":
            log.info("Команда Mac quit_app: '%s' -> '%s'", target_raw, app_name)
            subprocess.run(
                ["osascript", "-e", f'tell application "{app_name}" to quit'],
                check=False,
                timeout=5,
            )
            return f"Закрываю {app_name} на компьютере."

    return None
