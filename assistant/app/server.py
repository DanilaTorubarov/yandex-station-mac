"""HTTP-оркестратор: станция шлёт PCM, получает текст и WAV."""

from __future__ import annotations

import base64
import logging
import subprocess
import tempfile
import time
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from .audio import pcm16le_to_float32, read_wav, resample
from .config import SAMPLE_RATE, Settings
from .dashboard_api import router as dashboard_router
from .journal import record_call
from .notify_tools import (
    MacEventsWatcher,
    announce_on_station_async,
    build_notification_wav,
    play_wav_on_station,
)
from .pipeline import Assistant

log = logging.getLogger(__name__)
STATIC_INDEX_HTML = Path(__file__).resolve().parent / "static" / "index.html"

settings = Settings.load()
assistant: Assistant | None = None
watcher: MacEventsWatcher | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global assistant, watcher
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    assistant = Assistant(settings)
    if settings.llm_provider != "ollama":
        try:
            httpx.post(
                f"{settings.ollama_url}/api/generate",
                json={"model": settings.llm_model, "keep_alive": 0},
                timeout=3.0,
            )
            log.info("выгружена модель %s из памяти Ollama (экономия ~3.6 ГБ RAM)", settings.llm_model)
        except Exception:
            pass
    watcher = MacEventsWatcher(assistant.speak)
    watcher.start()
    log.info(
        "оркестратор: ASR=%s (fallback=%s) LLM=%s TTS=%s",
        settings.asr_provider,
        settings.asr_fallback or "нет",
        getattr(assistant.llm, "name", settings.llm_provider),
        settings.tts_provider,
    )
    yield
    if watcher:
        watcher.stop()


app = FastAPI(title="station-assistant", version="0.1.0", lifespan=lifespan)
app.include_router(dashboard_router)


def _assistant() -> Assistant:
    if assistant is None:  # pragma: no cover - только при кривом старте
        raise HTTPException(status_code=503, detail="оркестратор ещё не готов")
    return assistant


class TextRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


def _voice_payload(result: dict) -> dict:
    audio = result.pop("audio", None)
    if audio:
        result["audio_wav_base64"] = base64.b64encode(audio).decode("ascii")
        result["audio_bytes"] = len(audio)
    return result


def _render_dashboard_html() -> HTMLResponse:
    if not STATIC_INDEX_HTML.exists():
        raise HTTPException(status_code=404, detail="HTML-панель не найдена")
    return HTMLResponse(STATIC_INDEX_HTML.read_text(encoding="utf-8"))


@app.get("/")
def root(request: Request):
    accept = request.headers.get("accept", "")
    if "application/json" in accept and "text/html" not in accept:
        return {
            "service": "station-assistant",
            "endpoints": ["/ui", "/health", "/chat", "/say", "/voice", "/voice/wav", "/reset"],
        }
    return _render_dashboard_html()


@app.get("/ui", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_ui() -> HTMLResponse:
    return _render_dashboard_html()


@app.get("/health")
def health() -> dict:
    service = _assistant()
    ollama_ok = None
    if settings.llm_provider == "ollama":
        try:
            response = httpx.get(f"{settings.ollama_url}/api/tags", timeout=2.0)
            ollama_ok = response.status_code == 200
        except httpx.HTTPError:
            ollama_ok = False
    return {
        "ok": True,
        "settings": {
            "language": settings.language,
            "llm_model": settings.llm_model,
            "ollama": settings.ollama_url,
            "ollama_reachable": ollama_ok,
        },
        "pipeline": service.health(),
    }


@app.post("/chat")
def chat(request: TextRequest) -> dict:
    service = _assistant()
    t0 = time.perf_counter()
    answer = service.answer(request.text)
    dt_sec = time.perf_counter() - t0
    record_call(
        source="web",
        transcript=request.text,
        answer=answer,
        route=service.last_route,
        seconds={"llm": dt_sec, "total": dt_sec},
    )
    return {"answer": answer}


@app.post("/say")
def say(
    request: TextRequest,
    station: bool = Query(True, description="Озвучить сразу на Яндекс Станции Мини 2"),
    chime: bool = Query(True, description="Добавить мягкий сигнал уведомления перед речью"),
    wait: bool = Query(False, description="Ждать окончания воспроизведения на колонке"),
    play: bool = Query(False, description="Дополнительно проиграть на динамиках Mac"),
) -> Response:
    """
    Озвучивает произвольный текст на Яндекс Станции Мини 2 (по умолчанию station=True)
    и возвращает WAV. Для фоновых хуков (Antigravity, сборки, рендер) работает неблокирующе.
    """
    service = _assistant()
    record_call(
        source="say",
        transcript="[Внешнее уведомление /say]",
        answer=request.text,
        route="Уведомление",
    )
    if station and not wait and not play:
        announce_on_station_async(request.text, service.speak, with_chime=chime)
        return JSONResponse({"ok": True, "queued": True, "text": request.text})

    wav, rate = build_notification_wav(request.text, service.speak, with_chime=chime)
    if station:
        play_wav_on_station(wav, fallback_mac=True)
    if play:
        _play_local(wav)
    return Response(
        content=wav,
        media_type="audio/wav",
        headers={"X-Sample-Rate": str(rate)},
    )


@app.get("/say")
def say_get(
    text: str = Query(..., min_length=1, max_length=4000),
    chime: bool = Query(True),
) -> JSONResponse:
    """Удобный GET-эндпоинт для быстрого вызова из браузера или curl: /say?text=..."""
    service = _assistant()
    announce_on_station_async(text, service.speak, with_chime=chime)
    return JSONResponse({"ok": True, "queued": True, "text": text})


def _play_local(wav: bytes) -> None:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
        handle.write(wav)
        path = handle.name
    try:
        subprocess.run(["afplay", path], check=False, timeout=120)
    finally:
        Path(path).unlink(missing_ok=True)


@app.post("/voice")
async def voice(
    request: Request,
    rate: int = Query(SAMPLE_RATE, ge=8000, le=48000),
) -> JSONResponse:
    """Сырой S16_LE моно (как со станции). Возвращает текст + WAV в base64."""
    body = await request.body()
    if len(body) < 2:
        raise HTTPException(status_code=400, detail="пустое аудио")
    samples = pcm16le_to_float32(body)
    if rate != SAMPLE_RATE:
        samples = resample(samples, rate, SAMPLE_RATE)
    result = await run_in_threadpool(_assistant().voice, samples, SAMPLE_RATE)
    return JSONResponse(_voice_payload(result))


@app.post("/voice/wav")
async def voice_wav(request: Request) -> JSONResponse:
    """То же, но на входе WAV (удобно для curl и тестов)."""
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="пустое аудио")
    try:
        samples, rate = read_wav(body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"не разобрал WAV: {exc}") from exc
    result = await run_in_threadpool(_assistant().voice, samples, rate)
    return JSONResponse(_voice_payload(result))


@app.post("/voice/reply")
async def voice_reply(request: Request) -> Response:
    """Принимает WAV или сырой S16_LE PCM, возвращает готовый WAV в теле ответа."""
    body = await request.body()
    if not body or len(body) < 4:
        raise HTTPException(status_code=400, detail="пустое аудио")
    try:
        if body[:4] == b"RIFF":
            samples, rate = read_wav(body)
        else:
            samples = pcm16le_to_float32(body)
            rate = SAMPLE_RATE
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"ошибка декодирования аудио: {exc}") from exc

    result = await run_in_threadpool(_assistant().voice, samples, rate)
    wav = result.get("audio")
    if not wav:
        return Response(status_code=204)

    transcript = result.get("transcript", "")
    answer = result.get("answer", "")
    log.info("ассистент: вопрос='%s' ответ='%s' (%.2f с)", transcript, answer, result["seconds"]["total"])

    return Response(
        content=wav,
        media_type="audio/wav",
        headers={
            "X-Transcript": urllib.parse.quote(transcript),
            "X-Answer": urllib.parse.quote(answer),
            "X-Time-Total": f"{result['seconds']['total']:.2f}",
        },
    )


@app.post("/reset")
def reset() -> dict:
    _assistant().reset()
    return {"ok": True}


@app.post("/music/play")
def music_play() -> dict:
    from .music_tools import play_apple_music
    return play_apple_music()


@app.post("/music/pause")
def music_pause() -> dict:
    from .music_tools import pause_apple_music
    return pause_apple_music()


@app.post("/music/next")
def music_next() -> dict:
    from .music_tools import next_apple_music
    return next_apple_music()


@app.post("/music/volume")
def music_volume(level: int = Query(..., ge=0, le=10)) -> dict:
    from .music_tools import set_apple_music_airplay_volume
    return set_apple_music_airplay_volume(level)
