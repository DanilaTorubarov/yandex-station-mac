"""Интеграция с Apple Reminders (Напоминаниями) на macOS через AppleScript и JXA."""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import subprocess
from typing import Any

log = logging.getLogger(__name__)


# ==============================================================================
# 1. СИСТЕМНЫЕ ВЫЗОВЫ APPLE REMINDERS
# ==============================================================================

def create_apple_reminder(
    title: str,
    due_dt: dt.datetime | None = None,
    list_name: str | None = None,
) -> dict[str, Any]:
    """Создаёт новое напоминание в Apple Reminders через нативный AppleScript."""
    clean_title = (title or "").strip()
    if not clean_title:
        return {"ok": False, "error": "empty_title"}

    safe_title = clean_title.replace("\\", "\\\\").replace('"', '\\"')

    if due_dt:
        y, m, d = due_dt.year, due_dt.month, due_dt.day
        sec_of_day = due_dt.hour * 3600 + due_dt.minute * 60 + due_dt.second
        date_block = f"""
        set d to current date
        set year of d to {y}
        set month of d to {m}
        set day of d to {d}
        set time of d to {sec_of_day}
        """
        prop_block = f'{{name:"{safe_title}", due date:d}}'
    else:
        date_block = ""
        prop_block = f'{{name:"{safe_title}"}}'

    if list_name:
        safe_list = list_name.replace("\\", "\\\\").replace('"', '\\"')
        list_block = f"""
        if exists (list "{safe_list}") then
            set targetList to list "{safe_list}"
        else
            set targetList to default list
        end if
        """
    else:
        list_block = "set targetList to default list"

    script = f"""
    tell application "Reminders"
        {date_block}
        {list_block}
        set newRem to make new reminder at targetList with properties {prop_block}
        return (id of newRem as text) & "|||" & (name of targetList as text)
    end tell
    """

    try:
        res = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=15.0,
        )
        if res.returncode == 0 and res.stdout.strip():
            parts = res.stdout.strip().split("|||")
            rem_id = parts[0]
            l_name = parts[1] if len(parts) > 1 else ""
            log.info("Создано напоминание в Reminders: '%s' (список: %s)", clean_title, l_name)
            return {
                "ok": True,
                "id": rem_id,
                "name": clean_title,
                "list": l_name,
                "due": due_dt.isoformat() if due_dt else None,
            }
        log.warning("Ошибка создания напоминания: %s", res.stderr)
        return {"ok": False, "error": res.stderr.strip()}
    except Exception as exc:
        log.warning("Исключение при создании напоминания: %s", exc)
        return {"ok": False, "error": str(exc)}


def add_items_to_shopping_list(items: list[str]) -> dict[str, Any]:
    """Добавляет несколько позиций в список 'Shopping' (или 'Покупки')."""
    clean_items = [i.strip().capitalize() for i in items if i.strip()]
    if not clean_items:
        return {"ok": False, "error": "empty_items"}

    makes = []
    for it in clean_items:
        safe_it = it.replace("\\", "\\\\").replace('"', '\\"')
        makes.append(f'make new reminder at targetList with properties {{name:"{safe_it}"}}')
    makes_code = "\n".join(makes)

    script = f"""
    tell application "Reminders"
        set targetList to missing value
        if exists (list "Shopping") then
            set targetList to list "Shopping"
        else if exists (list "Покупки") then
            set targetList to list "Покупки"
        else
            set targetList to make new list with properties {{name:"Shopping"}}
        end if
        {makes_code}
        return name of targetList
    end tell
    """

    try:
        res = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=15.0,
        )
        if res.returncode == 0:
            log.info("Добавлены позиции в список покупок: %s", clean_items)
            return {"ok": True, "count": len(clean_items), "items": clean_items}
        log.warning("Ошибка добавления в список покупок: %s", res.stderr)
        return {"ok": False, "error": res.stderr.strip()}
    except Exception as exc:
        log.warning("Исключение при добавлении в список покупок: %s", exc)
        return {"ok": False, "error": str(exc)}


def get_all_active_reminders() -> list[dict[str, Any]]:
    """Получает все незавершённые напоминания из всех списков (быстрая пакетная выборка)."""
    script = """
    (function() {
        var app = Application("Reminders");
        var lists = app.lists();
        var out = [];
        for (var i = 0; i < lists.length; i++) {
            var l = lists[i];
            var lName = l.name();
            try {
                var names = l.reminders.name();
                var dues = l.reminders.dueDate();
                var comps = l.reminders.completed();
                for (var j = 0; j < names.length; j++) {
                    if (!comps[j]) {
                        var d = dues[j];
                        out.push({
                            list: lName,
                            title: names[j],
                            due: d ? d.toISOString() : null
                        });
                    }
                }
            } catch(e) {}
        }
        return JSON.stringify(out);
    })();
    """
    try:
        res = subprocess.run(
            ["/usr/bin/osascript", "-l", "JavaScript", "-e", script],
            capture_output=True,
            text=True,
            timeout=15.0,
        )
        if res.returncode == 0 and res.stdout.strip():
            return json.loads(res.stdout.strip())
    except Exception as exc:
        log.warning("Ошибка получения напоминаний: %s", exc)
    return []


# ==============================================================================
# 2. ПАРСИНГ ГОЛОСОВЫХ КОМАНД
# ==============================================================================

REMINDERS_PARSE_SYSTEM_PROMPT = """
Ты парсер голосовых команд для Apple Reminders (Напоминаний) на macOS.
Текущие дата и время пользователя: {now_str}.

Извлеки:
1) "action": "add" (поставить напоминание), "shopping" (добавить в покупки) или "list" (прочитать напоминания).
2) "title": текст напоминания (кратко, без лишних слов).
3) "items": массив строк, если это список покупок (например: ["Молоко", "Яйца", "Кофе"]).
4) "date": дата дедлайна в формате YYYY-MM-DD (или null, если без даты).
5) "time": время в формате HH:MM (или null).

Ответь СТРОГО одной строкой JSON без markdown:
{{"action": "add", "title": "Позвонить бабушке", "date": "2026-10-27", "time": "20:00"}}
или
{{"action": "shopping", "items": ["Молоко", "Яйца", "Кофе"]}}
или
{{"action": "list", "date": "2026-10-27"}}
"""


def _fast_extract_reminder(text: str) -> tuple[str, dt.datetime | None] | None:
    """Быстрое регулярное извлечение стандартных фраз напоминаний."""
    from .timer_tools import _replace_number_words

    t = text.strip()
    # Убираем префикс команды
    t_clean = re.sub(
        r"^(?:алиса[\s,]+)?(?:напомни|поставь\s+напоминание|добавь\s+напоминание|создай\s+напоминание)(?:\s+мне)?\s+",
        "",
        t,
        flags=re.IGNORECASE,
    ).strip()

    if not t_clean:
        return None

    t_norm = _replace_number_words(t_clean)
    now = dt.datetime.now()
    due = None
    title = t_norm

    def _adjust_hour(hr: int, period: str | None) -> int:
        p = (period or "").lower().strip()
        if p in ("вечера", "дня") and 1 <= hr <= 11:
            return hr + 12
        if p == "ночи" and hr == 12:
            return 0
        return min(23, max(0, hr))

    # Проверка "сегодня в HH:MM [утра/вечера]"
    m_today = re.search(
        r"\bсегодня\s+в\s+(\d{1,2})(?:[:\.](\d{2}))?(?:\s+(утра|вечера|дня|ночи|часов|часа))?\b",
        t_norm,
        re.I,
    )
    # Проверка "завтра в HH:MM [утра/вечера]"
    m_tomorrow = re.search(
        r"\bзавтра\s+в\s+(\d{1,2})(?:[:\.](\d{2}))?(?:\s+(утра|вечера|дня|ночи|часов|часа))?\b",
        t_norm,
        re.I,
    )
    # Проверка "через X минут / часов / секунд"
    m_rel = re.search(r"\bчерез\s+(\d+)\s+(минут\w*|час\w*|секунд\w*)\b", t_norm, re.I)
    # Проверка просто "в HH:MM" или "в HH утра/вечера" (без сегодня/завтра)
    m_at_time = re.search(
        r"\bв\s+(\d{1,2})(?:[:\.](\d{2}))?(?:\s+(утра|вечера|дня|ночи|часов|часа))?\b",
        t_norm,
        re.I,
    )

    if m_today:
        hr = _adjust_hour(int(m_today.group(1)), m_today.group(3))
        mi = int(m_today.group(2) or 0)
        due = now.replace(hour=hr, minute=mi, second=0, microsecond=0)
        title = (t_norm[: m_today.start()] + " " + t_norm[m_today.end() :]).strip()
    elif m_tomorrow:
        hr = _adjust_hour(int(m_tomorrow.group(1)), m_tomorrow.group(3))
        mi = int(m_tomorrow.group(2) or 0)
        due = (now + dt.timedelta(days=1)).replace(hour=hr, minute=mi, second=0, microsecond=0)
        title = (t_norm[: m_tomorrow.start()] + " " + t_norm[m_tomorrow.end() :]).strip()
    elif m_rel:
        num = int(m_rel.group(1))
        unit = m_rel.group(2).lower()
        if "секунд" in unit:
            due = now + dt.timedelta(seconds=num)
        elif "минут" in unit:
            due = now + dt.timedelta(minutes=num)
        else:
            due = now + dt.timedelta(hours=num)
        title = (t_norm[: m_rel.start()] + " " + t_norm[m_rel.end() :]).strip()
    elif m_at_time and (m_at_time.group(2) is not None or m_at_time.group(3) is not None):
        hr = _adjust_hour(int(m_at_time.group(1)), m_at_time.group(3))
        mi = int(m_at_time.group(2) or 0)
        cand = now.replace(hour=hr, minute=mi, second=0, microsecond=0)
        if cand < now:
            cand += dt.timedelta(days=1)
        due = cand
        title = (t_norm[: m_at_time.start()] + " " + t_norm[m_at_time.end() :]).strip()
    else:
        # Если в тексте есть сложные указания даты/времени («в пятницу», «послезавтра», «вечером»),
        # отдаём разбор LLM-парсеру.
        if re.search(
            r"\b(?:послезавтра|понедельник\w*|вторник\w*|сред[уеы]|четверг\w*|пятниц\w*|суббот\w*|воскресень\w*|"
            r"утром|вечером|дн[её]м|ночью|недел\w*|месяц\w*|завтра|сегодня|через)\b",
            t_norm,
            re.I,
        ):
            return None

    title = re.sub(r"\s+", " ", title).strip(" ,.-!?:;")
    return title, due


def parse_shopping_items(text: str) -> list[str]:
    """Извлекает товары для списка покупок."""
    t = text.strip()
    m = re.search(
        r"(?:в\s+список\s+покупок|в\s+покупки|в\s+список\s+продуктов|в\s+продукты)\s*[:\-]?\s*(.+)",
        t,
        re.I,
    )
    if not m:
        return []
    items_raw = m.group(1).strip()
    parts = re.split(r"[,;\n]|\s+и\s+|\s+а\s+также\s+", items_raw)
    return [p.strip().capitalize() for p in parts if p.strip()]


# ==============================================================================
# 3. ОСНОВНОЙ ДИСПЕТЧЕР КОМАНД REMINDERS
# ==============================================================================

def try_handle_reminders_command(text: str, llm=None) -> str | None:
    """
    Обрабатывает голосовые команды Напоминаний (Apple Reminders):
    - «Напомни сегодня в 20:00 позвонить бабушке»
    - «Добавь в список покупок: молоко, яйца, кофе»
    - «Какие у меня напоминания на сегодня?»
    """
    t = " ".join(text.split()).strip()
    if not t:
        return None
    t_low = t.lower()

    # 1. Список покупок: добавление
    if any(p in t_low for p in ("в список покупок", "в покупки", "в список продуктов", "в продукты")) and any(
        w in t_low for w in ("добавь", "запиши", "занеси", "купи", "купить")
    ):
        items = parse_shopping_items(t)
        if items:
            res = add_items_to_shopping_list(items)
            if res.get("ok"):
                joined = ", ".join(items[:-1]) + (" и " + items[-1] if len(items) > 1 else items[0])
                return f"Добавила в список покупок: {joined}."
            return "Не удалось добавить позиции в список покупок."

    # 1b. Список покупок: просмотр
    if any(p in t_low for p in ("что в списке покупок", "что в покупках", "список покупок", "покажи покупки", "прочитай список покупок")):
        all_rems = get_all_active_reminders()
        shopping_items = [r["title"] for r in all_rems if r.get("list") in ("Shopping", "Покупки")]
        if not shopping_items:
            return "Список покупок пуст."
        joined = ", ".join(shopping_items[:-1]) + (" и " + shopping_items[-1] if len(shopping_items) > 1 else shopping_items[0])
        return f"В списке покупок {len(shopping_items)} позиций: {joined}."

    # 2. Просмотр напоминаний (на сегодня / вообще)
    if any(p in t_low for p in ("какие напоминания", "что в напоминаниях", "прочитай напоминания", "какие у меня напоминания", "напоминания на сегодня", "напоминания на завтра")):
        all_rems = get_all_active_reminders()
        now = dt.datetime.now()
        is_today = "сегодня" in t_low or ("завтра" not in t_low and "недел" not in t_low)
        is_tomorrow = "завтра" in t_low

        target_date = (now + dt.timedelta(days=1)).date() if is_tomorrow else now.date()

        matched = []
        for r in all_rems:
            # Исключаем позиции чистого списка покупок из общего расписания дел
            if r.get("list") in ("Shopping", "Покупки"):
                continue
            due_iso = r.get("due")
            if due_iso:
                try:
                    d_dt = dt.datetime.fromisoformat(due_iso.replace("Z", "+00:00")).astimezone()
                    if d_dt.date() == target_date:
                        matched.append(f"{r['title']} в {d_dt.strftime('%H:%M')}")
                except Exception:
                    pass
            elif not is_tomorrow and not due_iso:
                matched.append(r["title"])

        day_word = "завтра" if is_tomorrow else "сегодня"
        if not matched:
            # Посмотрим, есть ли вообще какие-то ближайшие напоминания
            dated_rems = []
            for r in all_rems:
                if r.get("list") not in ("Shopping", "Покупки") and r.get("due"):
                    dated_rems.append(r)
            if dated_rems:
                nearest = dated_rems[0]["title"]
                return f"На {day_word} запланированных напоминаний нет. Ближайшее: {nearest}."
            return f"На {day_word} напоминаний нет."

        if len(matched) == 1:
            return f"На {day_word} у вас одно напоминание: {matched[0]}."
        return f"На {day_word} у вас {len(matched)} напоминаний: " + ", ".join(matched) + "."

    # 3. Добавление напоминания
    is_remind_trigger = any(
        t_low.startswith(p) or f" {p}" in t_low
        for p in ("напомни", "поставь напоминание", "добавь напоминание", "создай напоминание")
    )
    if not is_remind_trigger:
        return None

    # Сначала быстрый регулярный парсинг
    fast_res = _fast_extract_reminder(t)
    if fast_res and fast_res[0]:
        title, due = fast_res
        res = create_apple_reminder(title, due)
        if res.get("ok"):
            if due:
                now = dt.datetime.now()
                day_w = "сегодня" if due.date() == now.date() else ("завтра" if due.date() == (now + dt.timedelta(days=1)).date() else due.strftime("%d.%m"))
                time_w = due.strftime("%H:%M")
                return f"Поставила напоминание: {title} на {day_w} в {time_w}."
            return f"Добавила напоминание: {title}."

    # Если регулярка не справилась со сложной формулировкой — используем LLM
    if llm:
        now_str = dt.datetime.now().strftime("%Y-%m-%d %H:%M, %A")
        sys_prompt = REMINDERS_PARSE_SYSTEM_PROMPT.format(now_str=now_str)
        try:
            parsed_raw = llm.chat_custom(
                system_prompt=sys_prompt,
                user_prompt=t,
                num_predict=150,
            )
            # Чистим JSON
            m_json = re.search(r"\{.*\}", parsed_raw, re.DOTALL)
            if m_json:
                data = json.loads(m_json.group(0))
                act = data.get("action")
                if act == "shopping" and data.get("items"):
                    res = add_items_to_shopping_list(data["items"])
                    if res.get("ok"):
                        return f"Добавила в список покупок: {', '.join(data['items'])}."
                elif act == "add" and data.get("title"):
                    title = data["title"]
                    due_date = None
                    if data.get("date"):
                        t_str = data.get("time") or "10:00"
                        due_date = dt.datetime.strptime(f"{data['date']} {t_str}", "%Y-%m-%d %H:%M")
                    res = create_apple_reminder(title, due_date)
                    if res.get("ok"):
                        if due_date:
                            return f"Поставила напоминание: {title} на {due_date.strftime('%d.%m в %H:%M')}."
                        return f"Добавила напоминание: {title}."
        except Exception as exc:
            log.warning("Ошибка LLM-парсера напоминаний: %s", exc)

    return None
