"""Интеграция с Apple Notes (Заметки) и Apple Calendar (Календарь) на macOS."""

from __future__ import annotations

import datetime as dt
import html
import json
import logging
import re
import subprocess
from typing import Any

log = logging.getLogger(__name__)


# ==============================================================================
# 1. APPLE NOTES (Заметки)
# ==============================================================================

def create_apple_note(title: str, text: str) -> dict[str, Any]:
    """Создаёт новую заметку в штатном приложении Заметки (Apple Notes)."""
    clean_title = (title or "").strip()
    clean_text = (text or "").strip()

    if not clean_title and clean_text:
        lines = clean_text.splitlines()
        clean_title = lines[0][:60].strip(" .,!?:;")
        clean_text = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""

    if not clean_title:
        clean_title = "Новая заметка"

    html_body = f"<h1>{html.escape(clean_title)}</h1>"
    if clean_text:
        for para in clean_text.splitlines():
            p_strip = para.strip()
            if p_strip:
                html_body += f"<p>{html.escape(p_strip)}</p>"
    else:
        html_body += "<p></p>"

    script = """
    on run argv
        set note_title to item 1 of argv
        set note_body to item 2 of argv
        tell application "Notes"
            tell folder "Notes"
                set newNote to make new note with properties {name:note_title, body:note_body}
                return id of newNote
            end tell
        end tell
    end run
    """
    try:
        res = subprocess.run(
            ["/usr/bin/osascript", "-e", script, clean_title, html_body],
            capture_output=True,
            text=True,
            timeout=8.0,
        )
        if res.returncode == 0:
            log.info("Создана заметка в Apple Notes: '%s'", clean_title)
            return {"ok": True, "title": clean_title, "id": res.stdout.strip()}
        log.warning("Ошибка создания заметки: %s", res.stderr)
        return {"ok": False, "error": res.stderr.strip()}
    except Exception as exc:
        log.warning("Исключение при создании заметки: %s", exc)
        return {"ok": False, "error": str(exc)}


def append_to_latest_apple_note(text: str) -> dict[str, Any]:
    """Добавляет текст в последнюю изменённую заметку."""
    clean_text = (text or "").strip()
    if not clean_text:
        return {"ok": False, "error": "empty_text"}

    escaped_addition = f"<p>{html.escape(clean_text)}</p>"
    script = f"""
    tell application "Notes"
        tell folder "Notes"
            if (count of notes) is 0 then
                make new note with properties {{name:"Заметка", body:"<h1>Заметка</h1>{escaped_addition}"}}
                return "created"
            else
                set lastNote to note 1
                set currentBody to body of lastNote
                set body of lastNote to (currentBody & "{escaped_addition}")
                return name of lastNote
            end if
        end tell
    end tell
    """
    try:
        res = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=8.0,
        )
        if res.returncode == 0:
            return {"ok": True, "note": res.stdout.strip()}
        return {"ok": False, "error": res.stderr.strip()}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def get_latest_apple_notes(limit: int = 3) -> list[dict[str, str]]:
    """Возвращает список последних заметок из Apple Notes."""
    script = f"""
    (function() {{
        var notesApp = Application("Notes");
        var notes = notesApp.folders.byName("Notes").notes();
        var count = Math.min({limit}, notes.length);
        var res = [];
        for (var i = 0; i < count; i++) {{
            res.push({{
                title: notes[i].name(),
                date: notes[i].modificationDate().toLocaleDateString()
            }});
        }}
        return JSON.stringify(res);
    }})();
    """
    try:
        res = subprocess.run(
            ["/usr/bin/osascript", "-l", "JavaScript", "-e", script],
            capture_output=True,
            text=True,
            timeout=8.0,
        )
        if res.returncode == 0 and res.stdout.strip():
            return json.loads(res.stdout.strip())
    except Exception as exc:
        log.warning("Ошибка чтения заметок: %s", exc)
    return []


# ==============================================================================
# 2. APPLE CALENDAR (Календарь)
# ==============================================================================

def get_calendar_events_for_day(target_date: dt.date) -> list[dict[str, str]]:
    """Возвращает список событий календаря на выбранный день."""
    y = target_date.year
    m = target_date.month - 1  # JS 0-based month
    d = target_date.day

    script = f"""
    (function() {{
        var cal = Application("Calendar");
        var startDay = new Date({y}, {m}, {d}, 0, 0, 0);
        var endDay = new Date({y}, {m}, {d}, 23, 59, 59);
        var res = [];
        
        var cals = cal.calendars();
        for (var c = 0; c < cals.length; c++) {{
            var cName = cals[c].name();
            // Пропускаем праздники и служебные календари
            if (cName.indexOf("мерекелер") !== -1 || cName.indexOf("Holiday") !== -1 || cName.indexOf("Birthdays") !== -1) {{
                continue;
            }}
            try {{
                var evs = cals[c].events.whose({{startDate: {{_greaterThanEquals: startDay}}, startDate: {{_lessThanEquals: endDay}}}})();
                for (var i = 0; i < evs.length; i++) {{
                    var s = evs[i].startDate();
                    var e = evs[i].endDate();
                    var sTime = s.toLocaleTimeString([], {{hour: '2-digit', minute:'2-digit'}});
                    var eTime = e.toLocaleTimeString([], {{hour: '2-digit', minute:'2-digit'}});
                    res.push({{
                        title: evs[i].summary(),
                        start: sTime,
                        end: eTime,
                        calendar: cName
                    }});
                }}
            }} catch(e) {{}}
        }}
        res.sort(function(a, b) {{ return a.start.localeCompare(b.start); }});
        return JSON.stringify(res);
    }})();
    """
    try:
        res = subprocess.run(
            ["/usr/bin/osascript", "-l", "JavaScript", "-e", script],
            capture_output=True,
            text=True,
            timeout=8.0,
        )
        if res.returncode == 0 and res.stdout.strip():
            return json.loads(res.stdout.strip())
    except Exception as exc:
        log.warning("Ошибка чтения календаря: %s", exc)
    return []


def create_calendar_event(
    title: str,
    start_dt: dt.datetime,
    duration_minutes: int = 60,
    calendar_name: str = "Calendar",
) -> dict[str, Any]:
    """Создаёт новое событие в Apple Calendar."""
    end_dt = start_dt + dt.timedelta(minutes=duration_minutes)

    s_str = start_dt.strftime("%Y-%m-%d %H:%M:%S")
    e_str = end_dt.strftime("%Y-%m-%d %H:%M:%S")

    script = f"""
    (function() {{
        var cal = Application("Calendar");
        var c = null;
        try {{
            c = cal.calendars.byName("{calendar_name}");
        }} catch(e) {{}}
        if (!c && cal.calendars.length > 0) {{
            c = cal.calendars[0];
        }}
        if (!c) {{
            return JSON.stringify({{ok: false, error: "no_calendar"}});
        }}
        
        var sDate = new Date("{s_str}".replace(/-/g, "/"));
        var eDate = new Date("{e_str}".replace(/-/g, "/"));
        
        var ev = cal.Event({{
            summary: {json.dumps(title)},
            startDate: sDate,
            endDate: eDate
        }});
        c.events.push(ev);
        return JSON.stringify({{
            ok: true,
            title: ev.summary(),
            start: sDate.toLocaleTimeString([], {{hour: '2-digit', minute:'2-digit'}}),
            date: sDate.toLocaleDateString()
        }});
    }})();
    """
    try:
        res = subprocess.run(
            ["/usr/bin/osascript", "-l", "JavaScript", "-e", script],
            capture_output=True,
            text=True,
            timeout=8.0,
        )
        if res.returncode == 0 and res.stdout.strip():
            return json.loads(res.stdout.strip())
        return {"ok": False, "error": res.stderr.strip()}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ==============================================================================
# 3. LLM ПАРСЕР И ОБРАБОТЧИК ГОЛОСОВЫХ КОМАНД
# ==============================================================================

CALENDAR_PARSE_SYSTEM_PROMPT = """
Ты парсер голосовых команд для календаря macOS.
Текущая дата и время пользователя передаются в запросе.
Извлеки:
1) "action": "add" (добавить событие) или "list" (посмотреть расписание).
2) "title": название события (кратко, например: "Встреча", "Тренировка", "Созвон по проекту").
3) "date": дата в формате YYYY-MM-DD (сегодня, завтра, послезавтра или конкретное число).
4) "time": время начала в формате HH:MM (если указано, по умолчанию "10:00").
5) "duration_minutes": длительность события в минутах (по умолчанию 60).

Ответь СТРОГО одной строкой JSON без markdown:
{"action": "add", "title": "Встреча", "date": "2026-10-28", "time": "15:00", "duration_minutes": 60}
или
{"action": "list", "date": "2026-10-27"}
"""

NOTES_PARSE_SYSTEM_PROMPT = """
Ты парсер голосовых команд для приложения Заметки (Apple Notes) на macOS.
Извлеки:
1) "action": "create" (создать новую заметку), "append" (дописать в последнюю заметку) или "list" (прочитать последние заметки).
2) "title": заголовок заметки (кратко, 2-5 слов).
3) "body": основной текст или пункты заметки. Если перечисляют список (например: "купить молоко, хлеб, яйца"), отформатируй аккуратно с переносами строк.

Ответь СТРОГО одной строкой JSON без markdown:
{"action": "create", "title": "Список покупок", "body": "Молоко\\nХлеб\\nЯйца"}
"""


def try_handle_notes_command(text: str, llm) -> str | None:
    """Обрабатывает голосовые команды Apple Notes (Заметки)."""
    t = " ".join(text.split()).strip()
    if not t:
        return None
    t_low = t.lower()

    has_notes_word = any(
        w in t_low for w in ("заметк", "заметку", "заметки", "заметках", "в заметки", "в заметку")
    )
    if not has_notes_word:
        return None

    # Просмотр последних заметок
    if any(p in t_low for p in ("какие заметки", "что в заметках", "прочитай заметки", "покажи заметки")):
        notes = get_latest_apple_notes(limit=3)
        if not notes:
            return "В Заметках пока ничего нет."
        titles = [n["title"] for n in notes if n.get("title")]
        return f"Последние заметки: {', '.join(titles)}."

    # Создание / дополнение заметки через LLM парсер
    try:
        raw_json = llm.chat_custom(
            system_prompt=NOTES_PARSE_SYSTEM_PROMPT,
            user_prompt=t,
            num_predict=150,
        )
        m = re.search(r"\{.*\}", raw_json, re.DOTALL)
        if m:
            data = json.loads(m.group(0))
            action = data.get("action", "create")
            title = data.get("title") or "Заметка"
            body = data.get("body") or title

            if action == "append":
                res = append_to_latest_apple_note(body)
                if res.get("ok"):
                    return f"Дописала в заметку: {body}."
            else:
                res = create_apple_note(title, body)
                if res.get("ok"):
                    return f"Создала заметку «{title}» в Apple Notes."
    except Exception as exc:
        log.warning("Ошибка парсинга заметки: %s", exc)

    # Быстрый регулярный фоллбэк
    m_create = re.search(r"\b(?:запиши|создай|добавь|напиши)\s+(?:в\s+заметк\w*|заметку)\s*[:—–-]?\s*(?P<content>.+)", t, re.IGNORECASE)
    if m_create:
        content = m_create.group("content").strip()
        res = create_apple_note("Заметка", content)
        if res.get("ok"):
            return f"Записала в заметки: {content}."

    return None


def try_handle_calendar_command(text: str, llm) -> str | None:
    """Обрабатывает голосовые команды Apple Calendar (Календарь)."""
    t = " ".join(text.split()).strip()
    if not t:
        return None
    t_low = t.lower()

    has_cal_word = any(
        w in t_low for w in ("календар", "встреч", "расписани", "планы", "событи", "созвон", "дела на сегодня", "дела на завтра")
    )
    if not has_cal_word:
        return None

    now = dt.datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M (день недели: %A)")
    user_query = f"Текущее время: {now_str}.\nЗапрос пользователя: «{t}»."

    try:
        raw_json = llm.chat_custom(
            system_prompt=CALENDAR_PARSE_SYSTEM_PROMPT,
            user_prompt=user_query,
            num_predict=120,
        )
        m = re.search(r"\{.*\}", raw_json, re.DOTALL)
        if m:
            data = json.loads(m.group(0))
            action = data.get("action")

            if action == "list":
                date_str = data.get("date") or now.strftime("%Y-%m-%d")
                try:
                    target_d = dt.date.fromisoformat(date_str)
                except ValueError:
                    target_d = now.date()

                events = get_calendar_events_for_day(target_d)
                day_name = "сегодня" if target_d == now.date() else ("завтра" if target_d == now.date() + dt.timedelta(days=1) else target_d.strftime("%d.%m"))
                if not events:
                    return f"На {day_name} в календаре ничего не запланировано."

                # Фильтруем события на сегодня: если день уже идёт, показываем предстоящие
                if target_d == now.date():
                    cur_hm = now.strftime("%H:%M")
                    upcoming = [ev for ev in events if ev["end"] >= cur_hm or ev["start"] >= cur_hm]
                    if upcoming:
                        events_to_show = upcoming[:6]
                        extra_cnt = len(upcoming) - len(events_to_show)
                    else:
                        events_to_show = events[-4:]
                        extra_cnt = 0
                else:
                    events_to_show = events[:6]
                    extra_cnt = len(events) - len(events_to_show)

                parts = [f"{ev['start']} {ev['title']}" for ev in events_to_show]
                res_text = f"Планы на {day_name}: {'; '.join(parts)}."
                if extra_cnt > 0:
                    res_text += f" И ещё {extra_cnt} событий."
                return res_text

            elif action == "add":
                title = data.get("title") or "Встреча"
                date_str = data.get("date") or now.strftime("%Y-%m-%d")
                time_str = data.get("time") or "10:00"
                dur = int(data.get("duration_minutes") or 60)

                try:
                    start_dt = dt.datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
                except ValueError:
                    start_dt = now.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)

                res = create_calendar_event(title, start_dt, duration_minutes=dur)
                if res.get("ok"):
                    day_label = "сегодня" if start_dt.date() == now.date() else ("завтра" if start_dt.date() == now.date() + dt.timedelta(days=1) else start_dt.strftime("%d.%m"))
                    return f"Добавила в календарь: «{title}» на {day_label} в {res.get('start', time_str)}."
    except Exception as exc:
        log.warning("Ошибка парсинга календаря: %s", exc)

    return None
