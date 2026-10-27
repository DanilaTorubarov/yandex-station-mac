"""Интеграция с qBittorrent Web API и голосовой поиск торрентов (фильмы, сериалы, музыка, софт)."""

from __future__ import annotations

import json
import logging
import os
import re
import ssl
import subprocess
import time
import urllib.parse
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

QBITTORRENT_WEBUI_URL = "http://127.0.0.1:8080"
RUTOR_MIRRORS = [
    "https://rutor.info",
    "https://rutor.is",
    "https://zerkalo-rutor.org",
]

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


# ==============================================================================
# 1. ПРОВЕРКА И ЗАПУСК QBITTORRENT
# ==============================================================================

def is_qbittorrent_running() -> bool:
    """Проверяет, запущен ли процесс qBittorrent."""
    try:
        res = subprocess.run(["pgrep", "-ix", "qbittorrent"], capture_output=True, text=True)
        return res.returncode == 0
    except Exception:
        return False


def is_webui_ready() -> bool:
    """Проверяет доступность Web API qBittorrent."""
    url = f"{QBITTORRENT_WEBUI_URL}/api/v2/app/version"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "StationAssistant/1.0"})
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            return resp.status == 200
    except Exception:
        return False


def ensure_qbittorrent() -> bool:
    """Гарантирует, что qBittorrent запущен и Web API доступен."""
    if is_webui_ready():
        return True

    log.info("Запуск qBittorrent...")
    try:
        subprocess.Popen(["open", "-a", "qBittorrent"])
    except Exception as exc:
        log.warning("Не удалось открыть qBittorrent через open: %s", exc)

    # Ждём инициализации WebUI до 4 секунд
    for _ in range(8):
        time.sleep(0.5)
        if is_webui_ready():
            log.info("qBittorrent WebUI готов к работе")
            return True

    return is_qbittorrent_running()


# ==============================================================================
# 2. ПОИСК ТОРРЕНТОВ (RUTOR)
# ==============================================================================

def search_rutor(query: str, limit: int = 40) -> list[dict[str, Any]]:
    """Ищет раздачи на Rutor и возвращает список найденных релизов."""
    q_clean = query.strip()
    if not q_clean:
        return []

    encoded_query = urllib.parse.quote(q_clean)
    pattern = re.compile(
        r"<tr[^>]*>\s*<td>(?P<date>[^<]+)</td>\s*"
        r"<td[^>]*>.*?<a[^>]+href=[\"'](?P<magnet>magnet:\?xt=[^\"']+)[\"'].*?"
        r"<a[^>]+href=[\"']/torrent/[^\"']+[\"']>(?P<title>[^<]+)</a>.*?"
        r"<td align=[\"']right[\"']>(?P<size>[^<]+)</td>\s*"
        r"<td align=[\"']center[\"']><span class=[\"']green[\"']><img[^>]*>&nbsp;(?P<seeds>\d+)</span>"
        r".*?<span class=[\"']red[\"']>&nbsp;(?P<leeches>\d+)</span>",
        re.DOTALL,
    )

    for mirror in RUTOR_MIRRORS:
        url = f"{mirror}/search/0/0/0/0/{encoded_query}"
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
            )
            with urllib.request.urlopen(req, timeout=6.0, context=_SSL_CTX) as resp:
                if resp.status != 200:
                    continue
                html = resp.read().decode("utf-8", errors="replace")

            results = []
            for match in pattern.finditer(html):
                data = match.groupdict()
                size_str = data["size"].replace("&nbsp;", " ").strip()
                seeds = int(data["seeds"])
                leeches = int(data["leeches"])
                title = data["title"].strip()
                magnet = data["magnet"]
                results.append({
                    "title": title,
                    "size": size_str,
                    "seeds": seeds,
                    "leeches": leeches,
                    "magnet": magnet,
                })
                if len(results) >= limit:
                    break

            if results:
                log.info("Rutor поиск '%s': найдено %d релизов на %s", query, len(results), mirror)
                return results
        except Exception as exc:
            log.debug("Ошибка поиска на %s: %s", mirror, exc)
            continue

    log.warning("Поиск на Rutor не дал результатов или все зеркала недоступны для: '%s'", query)
    return []


def select_best_release(
    results: list[dict[str, Any]],
    media_type: str = "general",
    quality: str | None = None,
) -> dict[str, Any] | None:
    """Выбирает оптимальный релиз по качеству и количеству сидов."""
    if not results:
        return None

    # 1. Если запрошено конкретное качество
    if quality:
        q = quality.lower().strip()
        matched = []
        for r in results:
            t = r["title"].lower()
            if q in ("4k", "2160p", "uhd"):
                if "2160p" in t or "4k" in t or "uhd" in t:
                    matched.append(r)
            elif q in ("1080p", "1080"):
                if "1080p" in t or "1080i" in t:
                    matched.append(r)
            elif q in ("720p", "720"):
                if "720p" in t:
                    matched.append(r)
            elif q == "flac":
                if "flac" in t:
                    matched.append(r)
            elif q == "mp3":
                if "mp3" in t:
                    matched.append(r)
        if matched:
            matched.sort(key=lambda x: x["seeds"], reverse=True)
            return matched[0]

    # 2. Если качество не указано, для фильмов отсекаем огромные 80+ ГБ ремуксы, если есть адекватные 1080p/BDRip
    if media_type == "movie":
        moderate_releases = []
        for r in results:
            t = r["title"].lower()
            # Проверяем размер (до 30 ГБ)
            size_m = re.match(r"([\d\.,]+)\s*GB", r["size"], re.IGNORECASE)
            if size_m:
                try:
                    val = float(size_m.group(1).replace(",", "."))
                    if val <= 35.0:
                        moderate_releases.append(r)
                except Exception:
                    pass
            elif "MB" in r["size"].upper():
                moderate_releases.append(r)

        if moderate_releases:
            # Сортируем по сидам
            moderate_releases.sort(key=lambda x: x["seeds"], reverse=True)
            # Если у умеренного релиза хорошее количество сидов (>= 3), берем его
            if moderate_releases[0]["seeds"] >= 3:
                return moderate_releases[0]

    # 3. Общая сортировка по максимальному числу сидов
    sorted_all = sorted(results, key=lambda x: x["seeds"], reverse=True)
    return sorted_all[0]


# ==============================================================================
# 3. QBITTORRENT WEB API УПРАВЛЕНИЕ
# ==============================================================================

def add_torrent_magnet(magnet: str, category: str = "") -> dict[str, Any]:
    """Добавляет magnet-ссылку в qBittorrent через Web API (с fallback на CLI)."""
    ensure_qbittorrent()

    if is_webui_ready():
        url = f"{QBITTORRENT_WEBUI_URL}/api/v2/torrents/add"
        payload = {"urls": magnet}
        if category:
            payload["category"] = category
        data = urllib.parse.urlencode(payload).encode("utf-8")
        try:
            req = urllib.request.Request(url, data=data, headers={"User-Agent": "StationAssistant/1.0"})
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                if resp.status == 200:
                    log.info("Торрент успешно добавлен через Web API")
                    return {"ok": True, "method": "webui"}
        except Exception as exc:
            log.warning("Web API add failed: %s, пробуем CLI", exc)

    # Fallback на прямой CLI qBittorrent
    try:
        cmd = [
            "/Applications/qbittorrent.app/Contents/MacOS/qbittorrent",
            "--skip-dialog=true",
        ]
        if category:
            cmd.append(f"--category={category}")
        cmd.append(magnet)
        subprocess.run(cmd, capture_output=True, timeout=5.0)
        log.info("Торрент добавлен через qBittorrent CLI")
        return {"ok": True, "method": "cli"}
    except Exception as exc:
        log.error("Ошибка добавления торрента через CLI: %s", exc)
        return {"ok": False, "error": str(exc)}


def get_torrents_status() -> list[dict[str, Any]]:
    """Получает список торрентов из qBittorrent Web API."""
    if not is_webui_ready():
        if not ensure_qbittorrent():
            return []

    url = f"{QBITTORRENT_WEBUI_URL}/api/v2/torrents/info"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "StationAssistant/1.0"})
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        log.warning("Не удалось получить список торрентов: %s", exc)
    return []


def control_torrents(action: str) -> bool:
    """Управляет торрентами (stop/pause, start/resume)."""
    ensure_qbittorrent()
    if not is_webui_ready():
        return False

    endpoints = []
    if action in ("stop", "pause"):
        endpoints = ["torrents/stop", "torrents/pause"]
    elif action in ("start", "resume"):
        endpoints = ["torrents/start", "torrents/resume"]

    data = urllib.parse.urlencode({"hashes": "all"}).encode("utf-8")
    for ep in endpoints:
        url = f"{QBITTORRENT_WEBUI_URL}/api/v2/{ep}"
        try:
            req = urllib.request.Request(url, data=data, headers={"User-Agent": "StationAssistant/1.0"})
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                if resp.status == 200:
                    log.info("Действие %s выполнено через %s", action, ep)
                    return True
        except Exception:
            continue
    return False


# ==============================================================================
# 4. ФОРМАТИРОВАНИЕ ДЛЯ ГОЛОСОВОГО АССИСТЕНТА
# ==============================================================================

def clean_title_for_speech(title: str) -> str:
    """Очищает технический мусор из названия раздачи для Piper TTS."""
    t = title
    if "/" in t:
        parts = t.split("/")
        t = parts[0].strip()
    t = re.sub(r"\[.*?\]", "", t)
    t = re.sub(r"\|.*", "", t)
    t = re.sub(
        r"\b(BDRip|DVDRip|WEBRip|WEB-DL|HDRip|HDTVRip|UHD|BDRemux|RePack|Custom|Extended|IMAX).*?",
        "",
        t,
        flags=re.IGNORECASE,
    )
    t = re.sub(r"\s+", " ", t).strip(" -:,|")
    return t or title[:40]


def format_size_ru(size_str: str) -> str:
    """Преобразует размер вида '3.17 GB' в текст для чтения вслух."""
    s = (size_str or "").strip().upper()
    m = re.match(r"([\d\.,]+)\s*(GB|MB|KB|ГБ|МБ|КБ)", s)
    if not m:
        return size_str
    val_str, unit = m.group(1).replace(",", "."), m.group(2)
    try:
        val = float(val_str)
        if unit in ("GB", "ГБ"):
            if val < 10:
                return f"{val:.1f} гигабайта".replace(".", " запятая ")
            return f"{int(round(val))} гигабайт"
        elif unit in ("MB", "МБ"):
            return f"{int(round(val))} мегабайт"
        elif unit in ("KB", "КБ"):
            return f"{int(round(val))} килобайт"
    except Exception:
        pass
    return size_str


def format_eta_ru(seconds: int) -> str:
    """Форматирует оставшееся время скачивания."""
    if seconds <= 0 or seconds >= 864000:
        return "рассчитывается"
    if seconds < 60:
        return f"{seconds} секунд"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} минут"
    hours = minutes // 60
    rem_min = minutes % 60
    if rem_min > 0:
        return f"{hours} ч {rem_min} мин"
    return f"{hours} ч"


def format_speed_ru(bytes_per_sec: int) -> str:
    """Форматирует скорость скачивания."""
    if bytes_per_sec <= 0:
        return "0 мегабайт в секунду"
    mb = bytes_per_sec / (1024 * 1024)
    if mb < 0.1:
        kb = bytes_per_sec / 1024
        return f"{int(kb)} килобайт в секунду"
    if mb < 10:
        return f"{mb:.1f} мегабайт в секунду".replace(".", " запятая ")
    return f"{int(round(mb))} мегабайт в секунду"


# ==============================================================================
# 5. ПАРСИНГ ГОЛОСОВЫХ КОМАНД
# ==============================================================================

_RE_DOWNLOAD_TRIGGER = re.compile(
    r"\b(скачай|скачать|закачай|загрузи|загрузить|поставь\s+на\s+закач\w*|найди\s+и\s+скачай)\b",
    re.IGNORECASE,
)

_RE_STATUS_TRIGGER = re.compile(
    r"\b(что\s+(?:сейчас\s+|тут\s+)?качается|какие\s+(?:сейчас\s+)?(?:торренты|закачки)|статус\s+торрент\w*|статус\s+закач\w*|сколько\s+осталось\s+качать|как\s+там\s+(?:наши\s+)?закач\w*|проверь\s+закач\w*)\b",
    re.IGNORECASE,
)

_RE_PAUSE_TRIGGER = re.compile(
    r"\b(поставь\s+торрент\w*\s+на\s+паузу|поставь\s+закач\w*\s+на\s+паузу|приостанови\s+закач\w*|приостанови\s+торрент\w*|пауза\s+торрент\w*)\b",
    re.IGNORECASE,
)

_RE_RESUME_TRIGGER = re.compile(
    r"\b(возобнови\s+закач\w*|продолжи\s+закач\w*|возобнови\s+торрент\w*|продолжи\s+скачивание\s+торрент\w*|включи\s+закач\w*)\b",
    re.IGNORECASE,
)


def extract_download_request(text: str) -> dict[str, Any] | None:
    """
    Извлекает название, категорию и качество из фразы скачивания.
    Например:
    - «Скачай фильм Интерстеллар в 1080p»
    - «Скачай сериал Во все тяжкие»
    - «Скачай альбом Linkin Park в flac»
    """
    t = text.strip()
    if not _RE_DOWNLOAD_TRIGGER.search(t):
        return None

    # Очищаем префикс команды скачивания
    t_clean = _RE_DOWNLOAD_TRIGGER.sub("", t).strip()

    # Определение качества и удаление предлогов перед ним
    quality = None
    if re.search(r"\b(?:в\s+)?(4k|2160p|4к)\b", t_clean, re.I):
        m = re.search(r"\b(?:в\s+)?(4k|2160p|4к)\b", t_clean, re.I)
        quality = "4k"
        t_clean = t_clean[:m.start()] + " " + t_clean[m.end():]
    elif re.search(r"\b(?:в\s+)?(1080p|1080|full\s*hd)\b", t_clean, re.I):
        m = re.search(r"\b(?:в\s+)?(1080p|1080|full\s*hd)\b", t_clean, re.I)
        quality = "1080p"
        t_clean = t_clean[:m.start()] + " " + t_clean[m.end():]
    elif re.search(r"\b(?:в\s+)?(720p|720|hd)\b", t_clean, re.I):
        m = re.search(r"\b(?:в\s+)?(720p|720|hd)\b", t_clean, re.I)
        quality = "720p"
        t_clean = t_clean[:m.start()] + " " + t_clean[m.end():]
    elif re.search(r"\b(?:в\s+)?(flac|lossless)\b", t_clean, re.I):
        m = re.search(r"\b(?:в\s+)?(flac|lossless)\b", t_clean, re.I)
        quality = "flac"
        t_clean = t_clean[:m.start()] + " " + t_clean[m.end():]
    elif re.search(r"\b(?:в\s+)?(mp3)\b", t_clean, re.I):
        m = re.search(r"\b(?:в\s+)?(mp3)\b", t_clean, re.I)
        quality = "mp3"
        t_clean = t_clean[:m.start()] + " " + t_clean[m.end():]

    # Очищаем фразы "в хорошем качестве", "в высоком качестве"
    t_clean = re.sub(r"\bв\s+(хорош\w*|высок\w*|лучш\w*)\s+качестве\b", "", t_clean, flags=re.I)

    # Определение категории и очистка типов контента
    media_type = "general"
    category = ""
    if re.search(r"\b(фильм|кино)\b", t_clean, re.I):
        media_type = "movie"
        category = "Movies"
        t_clean = re.sub(r"\b(фильм|кино)\b", "", t_clean, flags=re.I)
    elif re.search(r"\b(сериал|сезон)\b", t_clean, re.I):
        media_type = "series"
        category = "Series"
        t_clean = re.sub(r"\b(сериал|сезон)\b", "", t_clean, flags=re.I)
    elif re.search(r"\b(мультфильм|мультик|аниме)\b", t_clean, re.I):
        media_type = "animation"
        category = "Cartoons"
        t_clean = re.sub(r"\b(мультфильм|мультик|аниме)\b", "", t_clean, flags=re.I)
    elif re.search(r"\b(альбом|песню|песни|музыку|трек|треки|дискографию)\b", t_clean, re.I):
        media_type = "music"
        category = "Music"
        t_clean = re.sub(r"\b(альбом|песню|песни|музыку|трек|треки|дискографию)\b", "", t_clean, flags=re.I)
    elif re.search(r"\b(игру|игра|игрушку)\b", t_clean, re.I):
        media_type = "game"
        category = "Games"
        t_clean = re.sub(r"\b(игру|игра|игрушку)\b", "", t_clean, flags=re.I)

    # Убираем слово торрент/торренты/через торрент
    t_clean = re.sub(r"\b(через\s+торрент\w*|торрент\w*)\b", "", t_clean, flags=re.I)

    # Убираем предлоги и мусор в начале и конце
    t_clean = re.sub(r"^(пожалуйста|мне|нам|быстро|срочно)\s+", "", t_clean, flags=re.I)
    t_clean = re.sub(r"\s+\b(в|на|с|под)\b\s*$", "", t_clean, flags=re.I)
    t_clean = t_clean.strip(" ,.-!?:;")

    if not t_clean:
        return None

    return {
        "title": t_clean,
        "media_type": media_type,
        "category": category,
        "quality": quality,
    }


def try_handle_torrent_command(text: str, llm=None) -> str | None:
    """
    Основная точка входа для ассистента.
    Обрабатывает команды:
    - «Скачай фильм [название]»
    - «Что сейчас качается?» / «Статус торрентов»
    - «Поставь торренты на паузу» / «Продолжи закачку торрентов»
    """
    t_low = (text or "").strip().lower()

    # 1. Проверка статуса закачек
    if _RE_STATUS_TRIGGER.search(t_low):
        torrents = get_torrents_status()
        if not torrents:
            return "Сейчас активных торрент-загрузок нет."

        active = [t for t in torrents if t.get("state") in ("downloading", "stalledDL", "metaDL")]
        if not active:
            completed = [t for t in torrents if t.get("progress", 0) >= 1.0]
            if completed:
                last_name = clean_title_for_speech(completed[0].get("name", "файл"))
                return f"Все активные закачки завершены. Последняя загрузка: {last_name}."
            return "Активных закачек сейчас нет."

        top = active[0]
        name = clean_title_for_speech(top.get("name", "файл"))
        prog = int(round(top.get("progress", 0) * 100))
        speed = format_speed_ru(top.get("dlspeed", 0))
        eta = format_eta_ru(top.get("eta", 0))

        if len(active) == 1:
            return f"Скачивается {name}. Загружено {prog} процентов, скорость {speed}. Осталось примерно {eta}."
        return f"Скачивается {name} и ещё {len(active) - 1} файл. Загрузка {prog} процентов, скорость {speed}."

    # 2. Пауза закачек
    if _RE_PAUSE_TRIGGER.search(t_low):
        if control_torrents("stop"):
            return "Поставила загрузку торрентов на паузу."
        return "Не удалось приостановить торренты. Проверьте запущен ли qBittorrent."

    # 3. Возобновление закачек
    if _RE_RESUME_TRIGGER.search(t_low):
        if control_torrents("start"):
            return "Возобновила скачивание торрентов."
        return "Не удалось возобновить торренты. Проверьте запущен ли qBittorrent."

    # 4. Скачивание торрента
    req_info = extract_download_request(text)
    if not req_info and _RE_DOWNLOAD_TRIGGER.search(t_low) and any(w in t_low for w in ("торрент", "фильм", "сериал", "музыку", "альбом")):
        # Если регулярка не поймала название, используем fallback
        words = t_low.split()
        for i, w in enumerate(words):
            if any(tr in w for tr in ("фильм", "сериал", "торрент", "альбом")):
                rest = " ".join(words[i + 1:]).strip()
                if rest:
                    req_info = {"title": rest, "media_type": "general", "category": "", "quality": None}
                break

    if not req_info or not req_info.get("title"):
        return None

    title_query = req_info["title"]
    log.info("Запрос на скачивание торрента: '%s' (качество: %s, тип: %s)", title_query, req_info.get("quality"), req_info.get("media_type"))

    results = search_rutor(title_query)
    if not results:
        return f"Я не нашла раздач для запроса {title_query} на трекерах."

    chosen = select_best_release(
        results,
        media_type=req_info.get("media_type", "general"),
        quality=req_info.get("quality"),
    )
    if not chosen:
        return f"Не удалось подобрать подходящий релиз для {title_query}."

    magnet = chosen["magnet"]
    # Подставляем реальное название раздачи в display name (dn) magnet-ссылки
    if "dn=rutor.info" in magnet:
        magnet = magnet.replace("dn=rutor.info", "dn=" + urllib.parse.quote(chosen["title"]))
    elif "&dn=" not in magnet:
        magnet += "&dn=" + urllib.parse.quote(chosen["title"])

    add_res = add_torrent_magnet(magnet, category=req_info.get("category", ""))
    if not add_res.get("ok"):
        return f"Не удалось передать торрент в qBittorrent: {add_res.get('error', 'ошибка клиента')}."

    spoken_title = clean_title_for_speech(chosen["title"])
    spoken_size = format_size_ru(chosen["size"])
    seeds = chosen["seeds"]

    log.info("Торрент запущен: '%s' (%s, seeds=%d)", spoken_title, chosen["size"], seeds)
    return f"Поставила на закачку: {spoken_title}. Размер {spoken_size}, {seeds} раздающих."
