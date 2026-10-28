"""REST API для веб-панели управления Яндекс Станцией Мини 2 и оркестратором."""

from __future__ import annotations

import base64
import datetime as dt
import logging
import subprocess
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from .audio import to_wav_bytes
from .config import ROOT
from .dev_tools import DEFAULT_REPO_DIR, git_status_summary, run_project_tests
from .find_phone_tools import trigger_find_phone
from .journal import clear_calls, get_calls, get_stats, record_call
from .music_tools import (
    current_track_apple_music,
    dislike_current_track_apple_music,
    favorite_current_track_apple_music,
    forward_apple_music,
    next_apple_music,
    pause_apple_music,
    play_apple_music,
    prev_apple_music,
    rewind_apple_music,
    set_apple_music_airplay_volume,
    try_handle_music_command,
)
from .notes_calendar_tools import get_latest_apple_notes
from .notify_tools import announce_on_station_async, build_notification_wav, play_wav_on_station
from .reminders_tools import (
    add_items_to_shopping_list,
    create_apple_reminder,
    get_all_active_reminders,
)
from .timer_tools import (
    STATION_HOST,
    STATION_PORT,
    _ACTIVE_ITEMS,
    _LOCK as _TIMER_LOCK,
    _format_duration_ru,
    _schedule_item,
    _start_station_timer_display,
    cancel_all_timers,
    stop_station_audio_and_timers,
)
from .tts import EdgeTTS, PiperTTS

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["dashboard"])


def _get_assistant():
    from . import server

    if server.assistant is None:
        raise HTTPException(status_code=503, detail="Оркестратор ещё инициализируется")
    return server.assistant


class WebChatRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    speak_station: bool = False
    return_audio: bool = False
    detailed: bool | None = None


class WebTTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    station: bool = True
    chime: bool = True
    return_audio: bool = True
    voice: str = ""
    rate: str = "+0%"


class VoiceConfigRequest(BaseModel):
    provider: str = "edge"  # "edge" | "piper"
    voice: str = "ru-RU-SvetlanaNeural"
    rate: str = "+0%"
    detailed_mode: bool | None = None


class StationActionRequest(BaseModel):
    action: str
    value: Any = None


class MusicActionRequest(BaseModel):
    action: str
    target: str = ""
    seconds: int = 20


class TimerCreateRequest(BaseModel):
    seconds: int = Field(ge=5, le=86400)
    label: str = "Таймер"
    kind: str = "timer"  # "timer" | "reminder"


class QuickReminderRequest(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    list_type: str = "reminder"  # "reminder" | "shopping"
    due_iso: str = ""


class DisplayActionRequest(BaseModel):
    action: str  # "show" | "scroll" | "temp" | "face" | "eq" | "timer" | "clock" | "clear" | "brightness"
    text: str = ""
    value: int | str = ""
    duration: int = 5
    speed_ms: int = 180
    loops: int = 1


def _active_timers_list() -> list[dict[str, Any]]:
    now = time.time()
    out: list[dict[str, Any]] = []
    with _TIMER_LOCK:
        for item in _ACTIVE_ITEMS.values():
            if item.cancelled:
                continue
            rem = max(0, int(round(item.fire_at - now)))
            out.append(
                {
                    "id": item.id,
                    "kind": item.kind,
                    "label": item.label,
                    "remaining_sec": rem,
                    "remaining_str": _format_duration_ru(max(1, rem)),
                    "duration_sec": int(item.duration_sec),
                }
            )
    return out


def _update_env_key(key: str, value: str) -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
        found = False
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith(f"{key}="):
                new_lines.append(f"{key}={value}")
                found = True
            else:
                new_lines.append(line)
        if not found:
            new_lines.append(f"{key}={value}")
        env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    except Exception as exc:
        log.warning("Не удалось обновить .env (%s): %s", key, exc)


@router.get("/overview")
def api_overview() -> dict[str, Any]:
    assistant = _get_assistant()
    health = assistant.health()
    stats = get_stats()
    timers = _active_timers_list()
    music = current_track_apple_music()
    current_voice = getattr(assistant.tts, "voice", "") or "piper-irina"
    current_rate = getattr(assistant.tts, "rate", "+0%")
    return {
        "ok": True,
        "pipeline": health,
        "detailed_mode": assistant.detailed_mode,
        "tts_config": {
            "provider": getattr(assistant.tts, "name", "edge"),
            "voice": str(current_voice),
            "rate": str(current_rate),
        },
        "stats": stats,
        "timers": timers,
        "music": music,
        "history": list(assistant.history),
    }


@router.get("/calls")
def api_get_calls(
    limit: int = Query(80, ge=1, le=200),
    source: str = Query(""),
    search: str = Query(""),
) -> dict[str, Any]:
    assistant = _get_assistant()
    return {
        "ok": True,
        "calls": get_calls(limit=limit, source=source, search=search),
        "stats": get_stats(),
        "history": list(assistant.history),
    }


@router.delete("/calls")
def api_clear_calls() -> dict[str, Any]:
    removed = clear_calls()
    return {"ok": True, "removed": removed}


@router.post("/chat")
def api_chat(req: WebChatRequest) -> dict[str, Any]:
    assistant = _get_assistant()
    prev_detailed = assistant.detailed_mode
    if req.detailed is not None:
        assistant.detailed_mode = req.detailed

    started = time.perf_counter()
    try:
        answer = assistant.answer(req.text)
    finally:
        if req.detailed is not None:
            assistant.detailed_mode = prev_detailed
    llm_done = time.perf_counter()

    audio_b64 = None
    tts_sec = 0.0
    if req.speak_station or req.return_audio:
        t0 = time.perf_counter()
        if req.speak_station and not req.return_audio:
            announce_on_station_async(answer, assistant.speak, with_chime=False)
            tts_sec = time.perf_counter() - t0
        else:
            wav, _ = assistant.speak(answer)
            tts_sec = time.perf_counter() - t0
            if req.speak_station:
                play_wav_on_station(wav, fallback_mac=True)
            if req.return_audio:
                audio_b64 = base64.b64encode(wav).decode("ascii")

    total_sec = time.perf_counter() - started
    secs = {
        "asr": 0.0,
        "llm": llm_done - started,
        "tts": tts_sec,
        "total": total_sec,
    }
    entry = record_call(
        source="web",
        transcript=req.text,
        answer=answer,
        route=assistant.last_route,
        seconds=secs,
    )
    return {
        "ok": True,
        "answer": answer,
        "route": assistant.last_route,
        "seconds": entry["seconds"],
        "audio_wav_base64": audio_b64,
        "call": entry,
    }


@router.post("/tts")
def api_tts(req: WebTTSRequest) -> dict[str, Any]:
    assistant = _get_assistant()
    started = time.perf_counter()

    # Если выбран конкретный голос для разового синтеза
    custom_voice = req.voice.strip()
    custom_rate = req.rate.strip() or "+0%"

    def _speak_fn(txt: str) -> tuple[bytes, int]:
        if custom_voice == "piper":
            p_tts = PiperTTS(assistant.settings.piper_voice)
            s, r = p_tts.synth(txt)
            return to_wav_bytes(s, r), r
        if custom_voice:
            e_tts = EdgeTTS(voice=custom_voice, rate=custom_rate)
            s, r = e_tts.synth(txt)
            return to_wav_bytes(s, r), r
        return assistant.speak(txt)

    wav, rate = build_notification_wav(req.text, _speak_fn, with_chime=req.chime)
    tts_sec = time.perf_counter() - started

    if req.station:
        import threading

        threading.Thread(target=play_wav_on_station, args=(wav, True), daemon=True).start()

    entry = record_call(
        source="say",
        transcript=f"[Озвучка: {custom_voice or getattr(assistant.tts, 'voice', 'default')}]",
        answer=req.text,
        route="TTS Студия",
        seconds={"tts": tts_sec, "total": tts_sec},
    )
    return {
        "ok": True,
        "sample_rate": rate,
        "seconds": round(tts_sec, 2),
        "audio_wav_base64": base64.b64encode(wav).decode("ascii") if req.return_audio else None,
        "call": entry,
    }


@router.post("/config/voice")
def api_config_voice(req: VoiceConfigRequest) -> dict[str, Any]:
    assistant = _get_assistant()
    provider = req.provider.lower().strip()
    if provider == "piper":
        assistant.tts = PiperTTS(assistant.settings.piper_voice)
        _update_env_key("TTS_PROVIDER", "piper")
    else:
        voice = req.voice.strip() or "ru-RU-SvetlanaNeural"
        rate = req.rate.strip() or "+0%"
        assistant.tts = EdgeTTS(voice=voice, rate=rate)
        _update_env_key("TTS_PROVIDER", "edge")
        _update_env_key("EDGE_VOICE", voice)

    if req.detailed_mode is not None:
        assistant.detailed_mode = bool(req.detailed_mode)

    return {
        "ok": True,
        "provider": getattr(assistant.tts, "name", provider),
        "voice": getattr(assistant.tts, "voice", "piper"),
        "rate": getattr(assistant.tts, "rate", "+0%"),
        "detailed_mode": assistant.detailed_mode,
    }


@router.get("/station/telemetry")
def api_station_telemetry() -> dict[str, Any]:
    """Опрашивает Яндекс Станцию Мини 2 по SSH и возвращает состояние процессов, памяти и аптайма."""
    t0 = time.perf_counter()
    remote_script = (
        "cat /proc/uptime 2>/dev/null | awk '{print $1}'; "
        "echo '---'; "
        "cat /proc/loadavg 2>/dev/null | awk '{print $1, $2, $3}'; "
        "echo '---'; "
        "grep -E 'MemTotal|MemAvailable' /proc/meminfo 2>/dev/null; "
        "echo '---'; "
        "ps -ef | grep -E 'touch-assistant|shairport-sync|maind' | grep -v grep"
    )
    try:
        res = subprocess.run(
            [
                "ssh",
                "-p",
                str(STATION_PORT),
                "-o",
                "ConnectTimeout=2",
                "-o",
                "StrictHostKeyChecking=no",
                f"root@{STATION_HOST}",
                remote_script,
            ],
            capture_output=True,
            text=True,
            timeout=3.5,
        )
        ping_ms = round((time.perf_counter() - t0) * 1000)
        if res.returncode != 0:
            return {
                "online": False,
                "host": STATION_HOST,
                "port": STATION_PORT,
                "error": res.stderr.strip() or "SSH недоступен",
            }
        parts = res.stdout.split("---")
        uptime_sec = float(parts[0].strip() or 0) if len(parts) > 0 else 0.0
        loadavg = parts[1].strip() if len(parts) > 1 else ""
        mem_raw = parts[2].strip() if len(parts) > 2 else ""
        ps_raw = parts[3].strip() if len(parts) > 3 else ""

        mem_total_kb = 0
        mem_avail_kb = 0
        for line in mem_raw.splitlines():
            if "MemTotal" in line:
                mem_total_kb = int("".join(ch for ch in line if ch.isdigit()) or 0)
            elif "MemAvailable" in line:
                mem_avail_kb = int("".join(ch for ch in line if ch.isdigit()) or 0)

        mem_total_mb = round(mem_total_kb / 1024)
        mem_used_mb = max(0, round((mem_total_kb - mem_avail_kb) / 1024))

        return {
            "online": True,
            "host": STATION_HOST,
            "port": STATION_PORT,
            "ping_ms": ping_ms,
            "uptime_str": _format_duration_ru(int(uptime_sec)) if uptime_sec > 0 else "н/д",
            "loadavg": loadavg,
            "mem_used_mb": mem_used_mb,
            "mem_total_mb": mem_total_mb,
            "services": {
                "mictap": "touch-assistant" in ps_raw,
                "glagold": "shairport-sync" in ps_raw,
                "minid": "maind" in ps_raw,
            },
        }
    except Exception as exc:
        return {
            "online": False,
            "host": STATION_HOST,
            "port": STATION_PORT,
            "error": str(exc),
        }


@router.post("/station/action")
def api_station_action(req: StationActionRequest) -> dict[str, Any]:
    assistant = _get_assistant()
    act = req.action

    if act == "stop_all":
        stop_station_audio_and_timers()
        record_call(
            source="system",
            transcript="[Экстренный стоп из панели]",
            answer="Остановлены все звуки, музыка и таймеры.",
            route="Стоп",
        )
        return {"ok": True, "message": "Звук, музыка и таймеры остановлены."}

    if act == "chime":
        wav, _ = build_notification_wav(
            "Проверка связи со станцией.",
            assistant.speak,
            with_chime=True,
        )
        import threading

        threading.Thread(target=play_wav_on_station, args=(wav, True), daemon=True).start()
        return {"ok": True, "message": "Тестовый сигнал отправлен на колонку."}

    if act == "find_iphone":
        msg = trigger_find_phone()
        record_call(
            source="web",
            transcript="Где мой айфон?",
            answer=msg,
            route="Локатор iPhone",
        )
        return {"ok": True, "message": msg}

    if act == "mac_volume":
        vol = max(0, min(100, int(req.value or 50)))
        subprocess.run(
            ["/usr/bin/osascript", "-e", f"set volume output volume {vol}"],
            capture_output=True,
            timeout=3.0,
        )
        return {"ok": True, "volume": vol, "message": f"Громкость Mac установлена на {vol}%."}

    if act == "airplay_volume":
        lvl = max(0, min(10, int(req.value or 5)))
        res = set_apple_music_airplay_volume(lvl)
        return {"ok": True, "result": res, "message": f"Громкость AirPlay: {lvl * 10}%."}

    if act == "restart_mictap":
        try:
            subprocess.run(
                [
                    "ssh",
                    "-p",
                    str(STATION_PORT),
                    "-o",
                    "ConnectTimeout=3",
                    "-o",
                    "StrictHostKeyChecking=no",
                    f"root@{STATION_HOST}",
                    "/etc/init.d/S99touch-assistant restart >/dev/null 2>&1 &",
                ],
                capture_output=True,
                timeout=5.0,
            )
            return {"ok": True, "message": "Демон touch-assistant на станции перезапущен."}
        except Exception as exc:
            return {"ok": False, "message": f"Ошибка SSH: {exc}"}

    raise HTTPException(status_code=400, detail=f"Неизвестное действие: {act}")


@router.post("/music/action")
def api_music_action(req: MusicActionRequest) -> dict[str, Any]:
    act = req.action
    if act == "play":
        if req.target:
            msg = try_handle_music_command(f"включи {req.target}")
            return {"ok": True, "message": msg or f"Включаю {req.target}"}
        res = play_apple_music()
        return {"ok": True, "result": res, "message": "Включаю вашу станцию в Apple Music."}
    if act == "resume":
        res = play_apple_music(resume_only=True)
        return {"ok": True, "result": res, "message": "Воспроизведение продолжено."}
    if act == "pause":
        res = pause_apple_music()
        return {"ok": True, "result": res, "message": "Музыка на паузе."}
    if act == "next":
        res = next_apple_music()
        return {"ok": True, "result": res, "message": "Следующий трек."}
    if act == "prev":
        res = prev_apple_music()
        return {"ok": True, "result": res, "message": "Предыдущий трек."}
    if act == "rewind":
        res = rewind_apple_music(req.seconds)
        return {"ok": True, "result": res, "message": f"Перемотка назад на {req.seconds} с."}
    if act == "forward":
        res = forward_apple_music(req.seconds)
        return {"ok": True, "result": res, "message": f"Перемотка вперёд на {req.seconds} с."}
    if act == "favorite":
        res = favorite_current_track_apple_music()
        tr = res.get("track", "")
        return {
            "ok": bool(res.get("ok")),
            "result": res,
            "message": f"Трек «{tr}» добавлен в любимые." if res.get("ok") else "Сейчас ничего не играет.",
        }
    if act == "dislike":
        res = dislike_current_track_apple_music()
        return {"ok": bool(res.get("ok")), "result": res, "message": "Поставлен дизлайк, переключаю трек."}
    if act == "status":
        return {"ok": True, "music": current_track_apple_music()}

    raise HTTPException(status_code=400, detail=f"Неизвестная команда музыки: {act}")


@router.post("/display/action")
def api_display_action(req: DisplayActionRequest) -> dict[str, Any]:
    from .display_tools import (
        clear_display,
        reset_display_to_native,
        set_display_brightness,
        set_night_mode,
        show_display_clock,
        show_display_date,
        show_display_equalizer,
        show_display_face,
        show_display_temperature,
        show_display_text,
        show_display_timer,
        show_display_volume,
    )
    act = req.action
    if act == "show":
        return show_display_text(req.text or str(req.value), scroll=False)
    if act == "scroll":
        return show_display_text(req.text or str(req.value), scroll=True, speed_ms=req.speed_ms, loops=req.loops)
    if act == "temp":
        return show_display_temperature(req.value or req.text or 0)
    if act == "face":
        face_name = req.text or str(req.value) or "happy"
        return show_display_face(face_name, req.duration)
    if act == "eq":
        return show_display_equalizer(req.duration)
    if act == "timer":
        sec = int(req.value) if str(req.value).isdigit() else (req.duration or 60)
        return show_display_timer(sec)
    if act == "clock":
        return show_display_clock()
    if act == "date":
        return show_display_date()
    if act == "vol":
        lvl = int(req.value) if str(req.value).isdigit() else 50
        return show_display_volume(lvl)
    if act == "night":
        on = str(req.value).lower() in ("1", "true", "on")
        return set_night_mode(on)
    if act in ("reset", "native"):
        return reset_display_to_native()
    if act == "clear":
        return clear_display()
    if act == "brightness":
        lvl = int(req.value) if str(req.value).isdigit() else 25
        return set_display_brightness(lvl)

    raise HTTPException(status_code=400, detail=f"Неизвестное действие: {act}")


@router.post("/timers/create")
def api_timer_create(req: TimerCreateRequest) -> dict[str, Any]:
    assistant = _get_assistant()
    dur_str = _format_duration_ru(req.seconds)
    label = req.label.strip() or dur_str
    if req.kind == "reminder":
        alert = f"Внимание! Напоминаю: {label}."
    else:
        alert = f"Внимание! Время вышло — сработал таймер: {label}."
        _start_station_timer_display(req.seconds)

    item = _schedule_item(req.kind, label, float(req.seconds), alert, assistant.speak)
    return {
        "ok": True,
        "id": item.id,
        "message": f"Запущен таймер «{label}» на {dur_str}.",
        "timers": _active_timers_list(),
    }


@router.post("/timers/cancel")
def api_timer_cancel() -> dict[str, Any]:
    cnt = cancel_all_timers()
    return {"ok": True, "cancelled": cnt, "timers": []}


@router.get("/productivity")
def api_productivity() -> dict[str, Any]:
    reminders = get_all_active_reminders()
    shopping = [r for r in reminders if r.get("list") in ("Shopping", "Покупки")]
    tasks = [r for r in reminders if r.get("list") not in ("Shopping", "Покупки")]
    notes = get_latest_apple_notes(limit=5)

    # Быстрый git status
    git_files: list[dict[str, str]] = []
    git_branch = "main"
    try:
        b_res = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(DEFAULT_REPO_DIR),
            capture_output=True,
            text=True,
            timeout=3.0,
        )
        if b_res.returncode == 0 and b_res.stdout.strip():
            git_branch = b_res.stdout.strip()

        s_res = subprocess.run(
            ["git", "status", "--short"],
            cwd=str(DEFAULT_REPO_DIR),
            capture_output=True,
            text=True,
            timeout=4.0,
        )
        for line in s_res.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(maxsplit=1)
            if len(parts) == 2:
                git_files.append({"status": parts[0], "file": parts[1]})
    except Exception:
        pass

    return {
        "ok": True,
        "reminders": tasks[:15],
        "shopping": shopping[:20],
        "notes": notes,
        "git": {
            "repo": DEFAULT_REPO_DIR.name,
            "branch": git_branch,
            "modified_count": len(git_files),
            "files": git_files[:25],
        },
    }


@router.post("/productivity/reminder")
def api_add_reminder(req: QuickReminderRequest) -> dict[str, Any]:
    if req.list_type == "shopping":
        items = [x.strip() for x in req.title.split(",") if x.strip()]
        res = add_items_to_shopping_list(items)
        return {"ok": bool(res.get("ok")), "message": f"Добавлено в покупки: {', '.join(items)}"}

    due_dt = None
    if req.due_iso:
        try:
            due_dt = dt.datetime.fromisoformat(req.due_iso)
        except Exception:
            due_dt = None
    res = create_apple_reminder(req.title, due_dt)
    return {"ok": bool(res.get("ok")), "message": f"Напоминание «{req.title}» создано."}


@router.post("/dev/git-summary")
def api_dev_git_summary(speak_station: bool = Query(False)) -> dict[str, Any]:
    assistant = _get_assistant()
    summary = git_status_summary(DEFAULT_REPO_DIR, llm=assistant.llm)
    if speak_station:
        announce_on_station_async(summary, assistant.speak, with_chime=False)
    record_call(
        source="web",
        transcript="Сводка Git / Code Review",
        answer=summary,
        route="Git / Dev",
    )
    return {"ok": True, "summary": summary}


@router.post("/dev/run-tests")
def api_dev_run_tests() -> dict[str, Any]:
    res = run_project_tests(DEFAULT_REPO_DIR)
    record_call(
        source="web",
        transcript="Запуск тестов проекта",
        answer=res,
        route="Git / Dev",
    )
    return {"ok": True, "result": res}


@router.get("/logs")
def api_get_logs(
    source: str = Query("stderr", description="stderr | stdout | station"),
    lines: int = Query(120, ge=20, le=500),
    search: str = Query(""),
) -> dict[str, Any]:
    raw_lines: list[str] = []
    if source == "stdout":
        p = Path("/tmp/station-assistant.stdout.log")
        if p.exists():
            raw_lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    elif source == "station":
        try:
            res = subprocess.run(
                [
                    "ssh",
                    "-p",
                    str(STATION_PORT),
                    "-o",
                    "ConnectTimeout=2",
                    "-o",
                    "StrictHostKeyChecking=no",
                    f"root@{STATION_HOST}",
                    f"tail -n {int(lines)} /tmp/touch-assistant.log /tmp/mictap.log 2>/dev/null || dmesg | tail -n 60",
                ],
                capture_output=True,
                text=True,
                timeout=3.5,
            )
            raw_lines = res.stdout.splitlines()[-lines:]
        except Exception as exc:
            raw_lines = [f"Ошибка чтения логов со станции по SSH: {exc}"]
    else:
        p = Path("/tmp/station-assistant.stderr.log")
        if p.exists():
            raw_lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]

    if search.strip():
        q = search.strip().lower()
        raw_lines = [ln for ln in raw_lines if q in ln.lower()]

    return {
        "ok": True,
        "source": source,
        "count": len(raw_lines),
        "lines": raw_lines,
    }
