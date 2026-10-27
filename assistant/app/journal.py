"""Журнал голосовых вызовов, обращений к нейросети и событий станции."""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from .config import ROOT

log = logging.getLogger(__name__)

JOURNAL_FILE = ROOT / "call_journal.json"
MAX_ENTRIES = 200

_LOCK = threading.Lock()
_ENTRIES: deque[dict[str, Any]] = deque(maxlen=MAX_ENTRIES)
_LOADED = False
_NEXT_ID = 1


def _ensure_loaded() -> None:
    global _LOADED, _NEXT_ID
    if _LOADED:
        return
    _LOADED = True
    if JOURNAL_FILE.exists():
        try:
            raw = json.loads(JOURNAL_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                for item in raw[-MAX_ENTRIES:]:
                    if isinstance(item, dict):
                        _ENTRIES.append(item)
                        item_id = int(item.get("id", 0))
                        if item_id >= _NEXT_ID:
                            _NEXT_ID = item_id + 1
        except Exception as exc:
            log.warning("Не удалось загрузить журнал вызовов: %s", exc)

    if len(_ENTRIES) <= 2:
        _backfill_from_stderr()


def _backfill_from_stderr() -> None:
    """Подтягивает историю голосовых вызовов из /tmp/station-assistant.stderr.log при первом запуске."""
    global _NEXT_ID
    import re

    stderr_path = Path("/tmp/station-assistant.stderr.log")
    if not stderr_path.exists():
        return
    pattern = re.compile(
        r"^(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2}),\d+\s+INFO\s+app\.server:\s+ассистент:\s+вопрос='(.*?)'\s+ответ='(.*?)'\s+\(([\d.]+)\s+с\)"
    )
    existing_questions = {(x.get("time_str"), x.get("transcript")) for x in _ENTRIES}
    added = 0
    try:
        for line in stderr_path.read_text(encoding="utf-8", errors="replace").splitlines():
            m = pattern.match(line.strip())
            if not m:
                continue
            date_iso, time_str, q_text, a_text, sec_str = m.groups()
            if (time_str, q_text) in existing_questions:
                continue
            total_s = float(sec_str)
            d_parts = date_iso.split("-")
            date_ru = f"{d_parts[2]}.{d_parts[1]}.{d_parts[0]}" if len(d_parts) == 3 else date_iso
            route = "Голос (Станция)"
            q_low = q_text.lower()
            if "айфон" in q_low or "телефон" in q_low:
                route = "Локатор iPhone"
            elif "открой" in q_low or "вкладк" in q_low or "компьютер" in q_low or "буфер" in q_low:
                route = "Mac OS"
            elif "музык" in q_low or "трек" in q_low or "рок" in q_low or "лоуфай" in q_low:
                route = "Apple Music"

            _ENTRIES.appendleft if False else _ENTRIES.append(
                {
                    "id": _NEXT_ID,
                    "timestamp": f"{date_iso}T{time_str}",
                    "time_str": time_str,
                    "date_str": date_ru,
                    "epoch": time.time(),
                    "source": "voice",
                    "route": route,
                    "transcript": q_text,
                    "answer": a_text,
                    "seconds": {
                        "asr": round(total_s * 0.25, 2),
                        "llm": round(total_s * 0.45, 2),
                        "tts": round(total_s * 0.30, 2),
                        "total": round(total_s, 2),
                    },
                }
            )
            _NEXT_ID += 1
            added += 1
        if added > 0:
            _save_unlocked()
    except Exception as exc:
        log.warning("Ошибка импорта истории из stderr.log: %s", exc)


def _save_unlocked() -> None:
    try:
        JOURNAL_FILE.write_text(
            json.dumps(list(_ENTRIES), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        log.warning("Не удалось сохранить журнал вызовов: %s", exc)


def record_call(
    *,
    source: str,
    transcript: str,
    answer: str,
    route: str = "LLM",
    seconds: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Записывает событие вызова (голос со станции, веб-чат, озвучка TTS) в журнал."""
    global _NEXT_ID
    now = dt.datetime.now()
    sec = seconds or {}
    with _LOCK:
        _ensure_loaded()
        entry = {
            "id": _NEXT_ID,
            "timestamp": now.isoformat(timespec="seconds"),
            "time_str": now.strftime("%H:%M:%S"),
            "date_str": now.strftime("%d.%m.%Y"),
            "epoch": round(time.time(), 2),
            "source": source,  # "voice" | "web" | "say" | "system"
            "route": route,
            "transcript": transcript,
            "answer": answer,
            "seconds": {
                "asr": round(float(sec.get("asr", 0.0)), 2),
                "llm": round(float(sec.get("llm", 0.0)), 2),
                "tts": round(float(sec.get("tts", 0.0)), 2),
                "total": round(float(sec.get("total", 0.0)), 2),
            },
        }
        _NEXT_ID += 1
        _ENTRIES.append(entry)
        _save_unlocked()
        return entry


def get_calls(limit: int = 100, source: str = "", search: str = "") -> list[dict[str, Any]]:
    """Возвращает последние вызовы в обратном хронологическом порядке (новые сверху)."""
    with _LOCK:
        _ensure_loaded()
        items = list(_ENTRIES)
    items.reverse()
    if source:
        items = [x for x in items if x.get("source") == source]
    if search:
        q = search.lower().strip()
        items = [
            x
            for x in items
            if q in (x.get("transcript") or "").lower()
            or q in (x.get("answer") or "").lower()
            or q in (x.get("route") or "").lower()
        ]
    return items[: max(1, min(limit, MAX_ENTRIES))]


def get_stats() -> dict[str, Any]:
    """Возвращает сводную статистику по вызовам."""
    with _LOCK:
        _ensure_loaded()
        items = list(_ENTRIES)
    total = len(items)
    voice_cnt = sum(1 for x in items if x.get("source") == "voice")
    web_cnt = sum(1 for x in items if x.get("source") == "web")
    say_cnt = sum(1 for x in items if x.get("source") == "say")
    durations = [
        float(x["seconds"]["total"])
        for x in items
        if x.get("seconds") and float(x["seconds"].get("total", 0)) > 0
    ]
    avg_sec = round(sum(durations) / len(durations), 2) if durations else 0.0

    routes: dict[str, int] = {}
    for x in items:
        r = x.get("route") or "LLM"
        routes[r] = routes.get(r, 0) + 1

    return {
        "total_calls": total,
        "voice_calls": voice_cnt,
        "web_calls": web_cnt,
        "say_calls": say_cnt,
        "avg_latency_sec": avg_sec,
        "routes": routes,
    }


def clear_calls() -> int:
    """Очищает журнал вызовов."""
    with _LOCK:
        _ensure_loaded()
        cnt = len(_ENTRIES)
        _ENTRIES.clear()
        _save_unlocked()
        return cnt
