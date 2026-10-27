"""Инструменты реального времени для голосового ассистента: время/дата, живая погода, курсы валют/крипты, новости и веб-поиск."""

from __future__ import annotations

import datetime as dt
import html
import logging
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET

import httpx

log = logging.getLogger(__name__)

_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
}

_WEEKDAYS_RU = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)

_MONTHS_GENITIVE_RU = (
    "",
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)

# Коды погоды WMO (Open-Meteo) -> описание по-русски
_WMO_CODES_RU: dict[int, str] = {
    0: "ясно и безоблачно",
    1: "преимущественно ясно",
    2: "переменная облачность",
    3: "пасмурно",
    45: "туман",
    48: "изморозь и туман",
    51: "слабая морось",
    53: "умеренная морось",
    55: "плотная морось",
    61: "небольшой дождь",
    63: "умеренный дождь",
    65: "сильный дождь",
    66: "ледяной дождь",
    67: "сильный ледяной дождь",
    71: "небольшой снег",
    73: "умеренный снег",
    75: "сильный снегопад",
    77: "снежная крупа",
    80: "небольшой ливень",
    81: "ливень",
    82: "сильный ливень",
    85: "небольшой снегопад",
    86: "сильный снегопад",
    95: "гроза",
    96: "гроза с небольшим градом",
    99: "сильная гроза с градом",
}


# --- 1. Текущее время, дата и день недели -----------------------------------

def get_current_datetime_context() -> str:
    """Возвращает краткую строку с текущим временем и датой для системного промпта LLM."""
    now = dt.datetime.now()
    weekday = _WEEKDAYS_RU[now.weekday()]
    month = _MONTHS_GENITIVE_RU[now.month]
    return f"Сегодня {weekday}, {now.day} {month} {now.year} года, текущее время {now.strftime('%H:%M')}."


def _try_handle_time_date(t: str, t_lower: str) -> str | None:
    now = dt.datetime.now()
    weekday = _WEEKDAYS_RU[now.weekday()]
    month = _MONTHS_GENITIVE_RU[now.month]

    if any(
        p in t_lower
        for p in (
            "сколько времени",
            "сколько сейчас времени",
            "который час",
            "скажи время",
            "текущее время",
            "время сейчас",
        )
    ) or t_lower in ("время", "час"):
        return f"Сейчас {now.hour} часов {now.minute:02d} минут."

    if any(
        p in t_lower
        for p in (
            "какое сегодня число",
            "какая сегодня дата",
            "какой сегодня день",
            "день недели сегодня",
            "сегодняшняя дата",
            "какой сейчас год",
        )
    ):
        return f"Сегодня {weekday}, {now.day} {month} {now.year} года."

    return None


# --- 2. Живая погода (Open-Meteo + геолокация по IP / городу) ---------------

_GEO_CACHE: dict[str, tuple[float, float, str, float]] = {}


def _resolve_location(city_query: str | None = None) -> tuple[float, float, str]:
    """Возвращает (lat, lon, название_города_в_предложном_или_именительном)."""
    cache_key = (city_query or "__auto__").lower().strip()
    cached = _GEO_CACHE.get(cache_key)
    if cached and (time.time() - cached[3] < 1800):
        return cached[0], cached[1], cached[2]

    if city_query:
        try:
            resp = httpx.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": city_query, "count": 1, "language": "ru", "format": "json"},
                headers=_HTTP_HEADERS,
                timeout=4.0,
            )
            if resp.status_code == 200:
                results = (resp.json() or {}).get("results") or []
                if results:
                    item = results[0]
                    lat, lon = float(item["latitude"]), float(item["longitude"])
                    name = str(item.get("name") or city_query)
                    _GEO_CACHE[cache_key] = (lat, lon, name, time.time())
                    return lat, lon, name
        except Exception as exc:
            log.warning("Не удалось геокодировать город '%s': %s", city_query, exc)

    # Автоопределение города по IP
    try:
        resp = httpx.get("http://ip-api.com/json/?lang=ru", headers=_HTTP_HEADERS, timeout=3.5)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == "success":
                lat = float(data["lat"])
                lon = float(data["lon"])
                city = str(data.get("city") or "вашем городе")
                _GEO_CACHE[cache_key] = (lat, lon, city, time.time())
                return lat, lon, city
    except Exception as exc:
        log.warning("Не удалось определить город по IP: %s", exc)

    return 55.7558, 37.6173, "Москве"


def _format_temp(val: float) -> str:
    r = round(val)
    if r > 0:
        return f"плюс {r}"
    if r < 0:
        return f"минус {abs(r)}"
    return "0"


def _extract_city_from_weather_query(t: str) -> str | None:
    m = re.search(
        r"\b(?:погода|температура|градусов|на\s+улице)\s+(?:сейчас\s+|сегодня\s+|завтра\s+)*в\s+(?:городе\s+)?([А-Яа-яЁёA-Za-z\-]+)",
        t,
        re.IGNORECASE,
    )
    if not m:
        m = re.search(r"\bв\s+(?:городе\s+)?([А-Яа-яЁё\-]{3,})\s+(?:какая\s+)?погода", t, re.IGNORECASE)
    if not m:
        return None
    raw_city = m.group(1).strip(" .,!?:;")
    if raw_city.lower() in ("улице", "окном", "доме", "комнате", "мире", "природе"):
        return None
    # Приводим предложный падеж («в Москве» -> «Москва», «в Питере» -> «Санкт-Петербург», «в Казани» -> «Казань»)
    low = raw_city.lower()
    city_aliases = {
        "москве": "Москва",
        "питере": "Санкт-Петербург",
        "петербурге": "Санкт-Петербург",
        "спб": "Санкт-Петербург",
        "казани": "Казань",
        "тверь": "Тверь",
        "твери": "Тверь",
        "перми": "Пермь",
        "туле": "Тула",
        "уфе": "Уфа",
        "самаре": "Самара",
        "волгограде": "Волгоград",
        "воронеже": "Воронеж",
        "екатеринбурге": "Екатеринбург",
        "новосибирске": "Новосибирск",
        "краснодаре": "Краснодар",
        "ростове": "Ростов-на-Дону",
        "нижнем": "Нижний Новгород",
        "калининграде": "Калининград",
        "владивостоке": "Владивосток",
        "омске": "Омск",
        "томске": "Томск",
        "челябинске": "Челябинск",
        "красноярске": "Красноярск",
        "саратове": "Саратов",
        "тюмени": "Тюмень",
        "ярославле": "Ярославль",
        "иркутске": "Иркутск",
        "хабаровске": "Хабаровск",
        "махачкале": "Махачкала",
        "минске": "Минск",
        "алматы": "Алматы",
        "астане": "Астана",
    }
    if low in city_aliases:
        return city_aliases[low]
    if low.endswith("е") and len(raw_city) > 4:
        return raw_city[:-1] + "а" if low[-2] in "влднрстмбпгкх" else raw_city[:-1]
    return raw_city


def _try_handle_weather(t: str, t_lower: str) -> str | None:
    if not any(
        w in t_lower
        for w in ("погод", "градус", "на улице", "за окном", "дождь сегодня", "снег сегодня", "холодно на улице", "тепло на улице")
    ):
        return None

    city_q = _extract_city_from_weather_query(t)
    lat, lon, city_name = _resolve_location(city_q)

    try:
        resp = httpx.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
                "timezone": "auto",
                "forecast_days": 2,
            },
            headers=_HTTP_HEADERS,
            timeout=4.5,
        )
        resp.raise_for_status()
        data = resp.json()
        cur = data.get("current") or {}
        daily = data.get("daily") or {}

        if "завтра" in t_lower and daily.get("temperature_2m_max") and len(daily["temperature_2m_max"]) >= 2:
            t_max = _format_temp(float(daily["temperature_2m_max"][1]))
            t_min = _format_temp(float(daily["temperature_2m_min"][1]))
            w_code = int((daily.get("weather_code") or [0, 0])[1])
            desc = _WMO_CODES_RU.get(w_code, "переменная облачность")
            return f"Завтра в городе {city_name} ожидается {desc}, температура днём до {t_max} градусов, ночью около {t_min}."

        temp = _format_temp(float(cur.get("temperature_2m", 0.0)))
        feels = _format_temp(float(cur.get("apparent_temperature", 0.0)))
        w_code = int(cur.get("weather_code", 0))
        desc = _WMO_CODES_RU.get(w_code, "переменная облачность")
        wind = round(float(cur.get("wind_speed_10m", 0.0)) / 3.6, 1)  # км/ч -> м/с

        # Автоматически выводим температуру со значком градуса на дисплей колонки
        try:
            from .display_tools import show_display_temperature
            raw_temp = round(float(cur.get("temperature_2m", 0.0)))
            show_display_temperature(raw_temp)
        except Exception:
            pass

        extra = ""
        if daily.get("temperature_2m_max"):
            d_max = _format_temp(float(daily["temperature_2m_max"][0]))
            extra = f" Днём воздух прогреется до {d_max} градусов."

        return (
            f"Сейчас в городе {city_name} {temp} градусов, ощущается как {feels}, {desc}, "
            f"ветер {wind} метра в секунду.{extra}"
        )
    except Exception as exc:
        log.warning("Ошибка получения погоды Open-Meteo: %s", exc)
        return "Не удалось получить свежие данные о погоде. Проверьте подключение к интернету."


# --- 3. Курсы валют (ЦБ РФ) и криптовалют (Binance / CoinGecko) -------------

_CBR_CACHE: tuple[dict, float] | None = None


def _get_cbr_rates() -> dict:
    global _CBR_CACHE
    if _CBR_CACHE and (time.time() - _CBR_CACHE[1] < 600):
        return _CBR_CACHE[0]
    resp = httpx.get("https://www.cbr-xml-daily.ru/daily_json.js", headers=_HTTP_HEADERS, timeout=4.5)
    resp.raise_for_status()
    valutes = (resp.json() or {}).get("Valute") or {}
    _CBR_CACHE = (valutes, time.time())
    return valutes


def _get_crypto_price_usd(symbol_pair: str, cg_id: str) -> float | None:
    try:
        r = httpx.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": symbol_pair},
            headers=_HTTP_HEADERS,
            timeout=3.5,
        )
        if r.status_code == 200:
            return float(r.json()["price"])
    except Exception:
        pass
    try:
        r2 = httpx.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": cg_id, "vs_currencies": "usd"},
            headers=_HTTP_HEADERS,
            timeout=4.0,
        )
        if r2.status_code == 200:
            return float(r2.json()[cg_id]["usd"])
    except Exception:
        pass
    return None


def _try_handle_rates(t: str, t_lower: str) -> str | None:
    has_rate_word = any(
        w in t_lower
        for w in (
            "курс",
            "доллар",
            "евро",
            "юан",
            "биткоин",
            "биткойн",
            "btc",
            "эфириум",
            "эфир",
            "eth",
            "крипт",
            "фунт",
            "тенге",
            "лир",
        )
    )
    if not has_rate_word:
        return None
    if not any(
        w in t_lower
        for w in ("курс", "сколько стоит", "почем", "почём", "цена", "в рублях", "в долларах", "стоит")
    ):
        return None

    # 1. Криптовалюты
    crypto_targets = [
        (("биткоин", "биткойн", "битка", "биток", "bitcoin", "btc"), "BTCUSDT", "bitcoin", "Биткоин"),
        (("эфириум", "эфир", "ethereum", "eth"), "ETHUSDT", "ethereum", "Эфириум"),
        (("тонкоин", "тонкойн", " курс тона", "стоит тон", "toncoin", "ton"), "TONUSDT", "the-open-network", "Тонкоин"),
        (("солан", "solana", "sol"), "SOLUSDT", "solana", "Солана"),
    ]
    for keys, pair, cg_id, name_ru in crypto_targets:
        if any(k in t_lower for k in keys):
            usd_price = _get_crypto_price_usd(pair, cg_id)
            if usd_price is None:
                return f"Не удалось получить актуальный курс {name_ru}."
            usd_str = f"{round(usd_price):,}".replace(",", " ") if usd_price >= 100 else f"{usd_price:.2f}"
            try:
                valutes = _get_cbr_rates()
                usd_rub = float(valutes["USD"]["Value"])
                rub_price = round(usd_price * usd_rub)
                rub_str = f"{rub_price:,}".replace(",", " ")
                return f"Сейчас {name_ru} стоит {usd_str} долларов, или около {rub_str} рублей."
            except Exception:
                return f"Сейчас {name_ru} стоит {usd_str} долларов."

    # 2. Курсы валют к рублю (ЦБ РФ)
    try:
        valutes = _get_cbr_rates()
    except Exception as exc:
        log.warning("Не удалось получить курсы ЦБ РФ: %s", exc)
        return "Не удалось загрузить свежие курсы валют."

    fiat_targets = [
        (("доллар", "бакс", "usd"), "USD", "доллара"),
        (("евро", "eur"), "EUR", "евро"),
        (("юан", "cny"), "CNY", "юаня"),
        (("фунт", "gbp"), "GBP", "фунта стерлингов"),
        (("тенге", "kzt"), "KZT", "казахстанского тенге"),
        (("лир", "try"), "TRY", "турецкой лиры"),
        (("дирхам", "aed"), "AED", "дирхама ОАЭ"),
    ]
    matched_parts: list[str] = []
    for keys, code, gen_name in fiat_targets:
        if any(k in t_lower for k in keys) and code in valutes:
            v = valutes[code]
            nominal = int(v.get("Nominal", 1))
            val = float(v["Value"]) / nominal
            rub_whole = int(val)
            kop = round((val - rub_whole) * 100)
            matched_parts.append(f"курс {gen_name} составляет {rub_whole} рублей {kop:02d} копеек")

    if matched_parts:
        return "Сейчас " + ", а ".join(matched_parts) + "."

    # Если просто спросили «какой сейчас курс валют»
    if "USD" in valutes and "EUR" in valutes:
        u = float(valutes["USD"]["Value"])
        e = float(valutes["EUR"]["Value"])
        return f"Сейчас курс доллара — {u:.2f} рубля, курс евро — {e:.2f} рубля."

    return None


# --- 4. Свежие новости и живой интернет-поиск -------------------------------

def _fetch_google_news_rss(query: str | None = None, limit: int = 6) -> list[str]:
    """Загружает свежие русскоязычные заголовки из Google News RSS или Lenta.ru RSS."""
    items: list[str] = []
    if query:
        url = f"https://news.google.com/rss/search?q={urllib.parse.quote(query)}&hl=ru&gl=RU&ceid=RU:ru"
    else:
        url = "https://lenta.ru/rss/top7"

    try:
        resp = httpx.get(url, headers=_HTTP_HEADERS, timeout=4.5, follow_redirects=True)
        if resp.status_code == 200 and resp.text:
            root = ET.fromstring(resp.text)
            for item in root.findall(".//item")[:limit]:
                title = (item.findtext("title") or "").strip()
                pub = (item.findtext("pubDate") or "").strip()
                if title:
                    items.append(f"- {title}" + (f" ({pub[:16]})" if pub else ""))
    except Exception as exc:
        log.warning("Ошибка чтения RSS (%s): %s", url, exc)

    if not items and not query:
        try:
            resp = httpx.get(
                "https://news.google.com/rss?hl=ru&gl=RU&ceid=RU:ru",
                headers=_HTTP_HEADERS,
                timeout=4.5,
                follow_redirects=True,
            )
            if resp.status_code == 200:
                root = ET.fromstring(resp.text)
                for item in root.findall(".//item")[:limit]:
                    title = (item.findtext("title") or "").strip()
                    if title:
                        items.append(f"- {title}")
        except Exception:
            pass
    return items


def _fetch_web_snippets(query: str, limit: int = 5) -> list[str]:
    """Ищет актуальные сниппеты в DuckDuckGo HTML + Русской Википедии + Google News RSS."""
    snippets: list[str] = []

    # 1. DuckDuckGo HTML
    try:
        resp = httpx.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query, "kl": "ru-ru"},
            headers=_HTTP_HEADERS,
            timeout=4.5,
            follow_redirects=True,
        )
        if resp.status_code == 200:
            raw_snippets = re.findall(
                r'<a class="result__snippet"[^>]*>(.*?)</a>',
                resp.text,
                flags=re.DOTALL | re.IGNORECASE,
            )
            for s in raw_snippets[:limit]:
                clean = html.unescape(re.sub(r"<[^>]+>", "", s)).strip()
                clean = " ".join(clean.split())
                if clean:
                    snippets.append(f"- {clean}")
    except Exception as exc:
        log.warning("Ошибка поиска DuckDuckGo для '%s': %s", query, exc)

    # 2. Русская Википедия (быстрый API поиска фактов)
    try:
        w_resp = httpx.get(
            "https://ru.wikipedia.org/w/api.php",
            params={
                "action": "query",
                "list": "search",
                "srsearch": query,
                "utf8": "1",
                "format": "json",
                "srlimit": "3",
            },
            headers=_HTTP_HEADERS,
            timeout=3.5,
        )
        if w_resp.status_code == 200:
            for item in (w_resp.json().get("query") or {}).get("search") or []:
                title = item.get("title", "")
                snip = html.unescape(re.sub(r"<[^>]+>", "", item.get("snippet", ""))).strip()
                if snip:
                    snippets.append(f"- [Википедия: {title}] {snip}")
    except Exception:
        pass

    # 3. Свежие новости по теме из Google News RSS
    news_items = _fetch_google_news_rss(query=query, limit=3)
    snippets.extend(news_items)
    return snippets[:8]


WEB_ANSWER_SYSTEM_PROMPT = (
    "Ты умный голосовой ассистент с доступом к свежему интернет-поиску и новостям. "
    "Отвечай строго по-русски. Используй приведённые ниже актуальные данные из интернета, "
    "чтобы точно и по делу ответить на вопрос пользователя в 2-3 предложениях. "
    "Твой ответ пойдёт сразу в синтезатор речи: категорически запрещены markdown, списки, ссылки и спецсимволы."
)


def _should_use_web_search(t_lower: str) -> bool:
    if any(
        p in t_lower
        for p in (
            "новост",
            "что нового в мире",
            "что происходит в мире",
            "что случилось",
            "найди в интернете",
            "поищи в интернете",
            "загугли",
            "погугли",
            "в интернете",
            "кто выиграл",
            "какой счёт",
            "какой счет",
            "когда выйдет",
            "когда вышел",
            "сколько стоит",
            "кто сейчас президент",
            "кто сейчас премьер",
            "последние события",
            "свежие данные",
            "в 2025",
            "в 2026",
        )
    ):
        return True
    return False


def _try_handle_news_and_web_search(t: str, t_lower: str, llm) -> str | None:
    if not _should_use_web_search(t_lower):
        return None

    dt_ctx = get_current_datetime_context()

    # 1. Запрос общих главных новостей («какие сегодня новости», «что нового в мире»)
    if any(w in t_lower for w in ("новост", "что нового в мире", "что происходит в мире")) and not re.search(
        r"\b(?:про|о|об)\s+[а-яёa-z]{3,}", t_lower
    ):
        log.info("Запрос главных новостей: '%s'", t)
        headlines = _fetch_google_news_rss(query=None, limit=6)
        if not headlines:
            return "Не удалось загрузить свежую ленту новостей."
        joined = "\n".join(headlines)
        user_prompt = (
            f"{dt_ctx}\nСвежие заголовки новостей:\n{joined}\n\n"
            f"Запрос пользователя: «{t}».\n"
            "Кратко и связно расскажи вслух 3-4 главные новости в нескольких предложениях без списков и спецсимволов."
        )
        return llm.chat_custom(
            system_prompt=WEB_ANSWER_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            num_predict=240,
        )

    # 2. Поиск по конкретному вопросу / теме в интернете
    clean_q = re.sub(
        r"^(?:пожалуйста[\s,]+)?(?:найди\s+в\s+интернете|поищи\s+в\s+интернете|загугли|погугли|скажи|расскажи)[\s,.:;-]*",
        "",
        t,
        flags=re.IGNORECASE,
    ).strip(" .,!?:;") or t

    log.info("Веб-поиск по запросу: '%s'", clean_q)
    snippets = _fetch_web_snippets(clean_q, limit=6)
    if not snippets:
        return None

    joined_snippets = "\n".join(snippets)
    user_prompt = (
        f"{dt_ctx}\nРезультаты поиска в интернете по запросу «{clean_q}»:\n{joined_snippets}\n\n"
        f"Вопрос пользователя: «{t}».\n"
        "Ответь на вопрос кратко (2-3 предложения) на основе найденных данных без спецсимволов."
    )
    return llm.chat_custom(
        system_prompt=WEB_ANSWER_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        num_predict=220,
    )


def try_handle_realtime_query(text: str, llm) -> str | None:
    """
    Проверяет, требует ли вопрос данных реального времени (время/дата, погода, курсы валют/крипты,
    свежие новости или веб-поиск). Если да — возвращает готовый голосовой ответ.
    """
    t = " ".join(text.split()).strip()
    if not t:
        return None
    t_lower = t.lower()

    # 1. Точное время / дата / день недели
    td_res = _try_handle_time_date(t, t_lower)
    if td_res:
        return td_res

    # 2. Живая погода
    w_res = _try_handle_weather(t, t_lower)
    if w_res:
        return w_res

    # 3. Курсы валют и криптовалют
    r_res = _try_handle_rates(t, t_lower)
    if r_res:
        return r_res

    # 4. Свежие новости и веб-поиск
    web_res = _try_handle_news_and_web_search(t, t_lower, llm)
    if web_res:
        return web_res

    return None
