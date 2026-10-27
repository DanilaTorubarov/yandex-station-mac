"""Синтез речи: сверхбыстрый Edge TTS (Светлана/Дмитрий с кэшем и прогретым WebSocket), локальный Piper, Google TTS."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

from .config import Settings

log = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).resolve().parent.parent / ".tts_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Время жизни прогретого WebSocket-соединения с Microsoft Edge (с)
_WARM_WS_TTL = 22.0


class TTSError(RuntimeError):
    pass


# --- Постоянный фоновый Event Loop для мгновенных WebSocket-запросов ----------

_bg_loop: asyncio.AbstractEventLoop | None = None
_bg_loop_lock = threading.Lock()


def _get_bg_loop() -> asyncio.AbstractEventLoop:
    global _bg_loop
    with _bg_loop_lock:
        if _bg_loop is None:
            ready = threading.Event()

            def _runner() -> None:
                global _bg_loop
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                _bg_loop = loop
                ready.set()
                loop.run_forever()

            t = threading.Thread(target=_runner, name="edge-tts-loop", daemon=True)
            t.start()
            ready.wait(timeout=2.0)
        return _bg_loop  # type: ignore[return-value]


def _run_coroutine_sync(coro, timeout: float = 5.5):
    """Запускает корутину в постоянном фоновом event loop с жёстким таймаутом."""
    loop = _get_bg_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return future.result(timeout=timeout)
    except Exception as exc:
        future.cancel()
        raise TTSError(f"Edge TTS таймаут или ошибка ({timeout:.1f}с): {exc}") from exc


class EdgeTTS:
    """Облачный нейросетевой синтез речи через Microsoft Edge TTS с кэшем и прогретым WebSocket.

    Три уровня ускорения:
    1. Двухуровневый кэш (RAM + диск `assistant/.tts_cache`): повторные фразы, уведомления
       и ответы команд отдаются за 0.001 с (1 мс).
    2. Параллельный прогрев WebSocket (`warmup()`): в момент начала распознавания речи (пока идут
       ASR и LLM) параллельно открывается TLS WebSocket к `speech.platform.bing.com` и отправляется
       `Path:speech.config`. К моменту готовности текста ответа соединение уже прогрето, и синтез
       занимает всего ~0.35–0.45 с вместо 1.5–6 с!
    3. Мгновенный обрыв сокета (`transport.abort()`) сразу после получения `Path:turn.end` без
       ожидания ответного WebSocket CLOSE фрейма.
    """

    name = "edge"

    def __init__(
        self,
        voice: str = "ru-RU-SvetlanaNeural",
        rate: str = "+0%",
        pitch: str = "+0Hz",
        timeout: float = 5.5,
    ) -> None:
        self.voice = voice
        self.rate = rate
        self.pitch = pitch
        self.timeout = timeout
        self._mem_cache: dict[str, tuple[np.ndarray, int]] = {}
        self._warm_session = None
        self._warm_ws = None
        self._warm_ts: float = 0.0
        self._warm_lock: asyncio.Lock | None = None

    @property
    def loaded(self) -> bool:
        return True

    def warmup(self) -> None:
        """Неблокирующий прогрев WebSocket-соединения параллельно с работой ASR и LLM."""
        try:
            loop = _get_bg_loop()
            asyncio.run_coroutine_threadsafe(self._ensure_warm_ws(), loop)
        except Exception:
            pass

    def _cache_key(self, text: str) -> str:
        norm = (
            " ".join(text.lower().replace("«", "").replace("»", "").replace('"', "").split())
        )
        raw = f"{self.voice}|{self.rate}|{self.pitch}|{norm}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:24]

    def _get_cached(self, key: str) -> tuple[np.ndarray, int] | None:
        cached = self._mem_cache.get(key)
        if cached is not None:
            return cached
        wav_file = CACHE_DIR / f"{key}.wav"
        if wav_file.exists():
            try:
                from .audio import read_wav

                res = read_wav(wav_file.read_bytes())
                if len(self._mem_cache) < 256:
                    self._mem_cache[key] = res
                return res
            except Exception:
                wav_file.unlink(missing_ok=True)
        return None

    def _save_cache(self, key: str, wav_bytes: bytes, decoded: tuple[np.ndarray, int]) -> None:
        if len(self._mem_cache) >= 256:
            try:
                oldest = next(iter(self._mem_cache))
                self._mem_cache.pop(oldest, None)
            except StopIteration:
                pass
        self._mem_cache[key] = decoded
        try:
            wav_file = CACHE_DIR / f"{key}.wav"
            if not wav_file.exists():
                tmp_file = CACHE_DIR / f"{key}.tmp.{os.getpid()}"
                tmp_file.write_bytes(wav_bytes)
                tmp_file.replace(wav_file)
        except Exception as exc:
            log.debug("Не удалось сохранить TTS-кэш: %s", exc)

    def synth(self, text: str) -> tuple[np.ndarray, int]:
        text = text.strip()
        if not text:
            raise TTSError("пустой текст для синтеза")
        key = self._cache_key(text)
        cached = self._get_cached(key)
        if cached is not None:
            return cached
        return _run_coroutine_sync(self._synth_async(text, key), timeout=self.timeout)

    @staticmethod
    def _abort_ws_and_session(ws, session) -> None:
        try:
            if ws is not None and getattr(ws, "_writer", None) and getattr(ws._writer, "transport", None):
                ws._writer.transport.abort()
        except Exception:
            pass
        try:
            if session is not None and getattr(session, "_connector", None):
                session._connector._close()
        except Exception:
            pass

    async def _open_configured_ws(self):
        import aiohttp
        import edge_tts.communicate as ec

        url = (
            f"{ec.WSS_URL}&ConnectionId={ec.connect_id()}"
            f"&Sec-MS-GEC={ec.DRM.generate_sec_ms_gec()}"
            f"&Sec-MS-GEC-Version={ec.SEC_MS_GEC_VERSION}"
        )
        connector = aiohttp.TCPConnector(family=socket.AF_INET, ssl=ec._SSL_CTX, force_close=True)
        client_timeout = aiohttp.ClientTimeout(total=self.timeout, connect=2.0, sock_read=4.5)
        session = aiohttp.ClientSession(connector=connector, timeout=client_timeout)
        try:
            ws = await session.ws_connect(
                url,
                compress=15,
                headers=ec.DRM.headers_with_muid(ec.WSS_HEADERS),
                ssl=ec._SSL_CTX,
            )
            await ws.send_str(
                f"X-Timestamp:{ec.date_to_string()}\r\n"
                "Content-Type:application/json; charset=utf-8\r\n"
                "Path:speech.config\r\n\r\n"
                '{"context":{"synthesis":{"audio":{"metadataoptions":{'
                '"sentenceBoundaryEnabled":"true","wordBoundaryEnabled":"false"'
                '},"outputFormat":"audio-24khz-48kbitrate-mono-mp3"}}}}\r\n'
            )
            return session, ws
        except Exception:
            self._abort_ws_and_session(None, session)
            raise

    async def _ensure_warm_ws(self) -> None:
        if self._warm_lock is None:
            self._warm_lock = asyncio.Lock()
        async with self._warm_lock:
            now = time.monotonic()
            if (
                self._warm_ws is not None
                and not self._warm_ws.closed
                and (now - self._warm_ts) < _WARM_WS_TTL
            ):
                return
            if self._warm_ws is not None or self._warm_session is not None:
                self._abort_ws_and_session(self._warm_ws, self._warm_session)
                self._warm_ws = None
                self._warm_session = None
            try:
                session, ws = await self._open_configured_ws()
                self._warm_session = session
                self._warm_ws = ws
                self._warm_ts = time.monotonic()
            except Exception as exc:
                log.debug("Прогрев EdgeTTS WS не удался: %s", exc)

    async def _acquire_ws(self):
        if self._warm_lock is None:
            self._warm_lock = asyncio.Lock()
        async with self._warm_lock:
            now = time.monotonic()
            if (
                self._warm_ws is not None
                and not self._warm_ws.closed
                and (now - self._warm_ts) < _WARM_WS_TTL
            ):
                session, ws = self._warm_session, self._warm_ws
                self._warm_session = None
                self._warm_ws = None
                return session, ws
            if self._warm_ws is not None or self._warm_session is not None:
                self._abort_ws_and_session(self._warm_ws, self._warm_session)
                self._warm_session = None
                self._warm_ws = None
        return await self._open_configured_ws()

    async def _download_mp3_once(self, text: str) -> bytes:
        import aiohttp
        import edge_tts.communicate as ec
        from edge_tts.data_classes import TTSConfig

        spoken_text = text.replace("«", "").replace("»", "").replace('"', "")
        clean_text = ec.escape(ec.remove_incompatible_characters(spoken_text))
        tc = TTSConfig(self.voice, self.rate, "+0%", self.pitch, "SentenceBoundary")

        session, ws = await self._acquire_ws()
        mp3_buf = bytearray()
        try:
            await ws.send_str(
                ec.ssml_headers_plus_data(
                    ec.connect_id(),
                    ec.date_to_string(),
                    ec.mkssml(tc, clean_text),
                )
            )
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    if len(msg.data) > 2:
                        hdr_len = int.from_bytes(msg.data[:2], "big")
                        if hdr_len + 2 < len(msg.data):
                            mp3_buf.extend(msg.data[hdr_len + 2 :])
                elif msg.type == aiohttp.WSMsgType.TEXT:
                    if "Path:turn.end" in msg.data:
                        break
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                    break
        finally:
            self._abort_ws_and_session(ws, session)

        if not mp3_buf:
            raise TTSError("пустой поток от ноды Edge TTS")
        return bytes(mp3_buf)

    async def _synth_async(self, text: str, cache_key: str) -> tuple[np.ndarray, int]:
        from .audio import read_wav

        try:
            mp3_bytes = await self._download_mp3_once(text)
        except Exception as exc:
            raise TTSError(f"Edge TTS ошибка генерации: {exc}") from exc

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f_in:
            f_in.write(mp3_bytes)
            in_path = f_in.name
        out_path = in_path + ".wav"

        try:
            cmd = ["/usr/bin/afconvert", "-f", "WAVE", "-d", "LEI16@24000", in_path, out_path]
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            if proc.returncode != 0:
                raise TTSError(f"afconvert error: {proc.stderr.decode('utf-8', errors='ignore')}")
            wav_bytes = Path(out_path).read_bytes()
            decoded = read_wav(wav_bytes)
            self._save_cache(cache_key, wav_bytes, decoded)
            return decoded
        except Exception as exc:
            if isinstance(exc, TTSError):
                raise
            raise TTSError(f"Ошибка конвертации Edge TTS аудио: {exc}") from exc
        finally:
            if os.path.exists(in_path):
                os.unlink(in_path)
            if os.path.exists(out_path):
                os.unlink(out_path)


class PiperTTS:
    """Piper на onnxruntime, полностью локально. Голос грузится при старте для мгновенного fallback (~0.12 с)."""

    name = "piper"

    def __init__(self, voice_path: Path, length_scale: float = 0.95, preload: bool = True) -> None:
        self.voice_path = Path(voice_path)
        self.length_scale = length_scale
        self._voice = None
        if preload and self.voice_path.exists():
            try:
                self._ensure_loaded()
            except Exception as exc:
                log.warning("Не удалось предзагрузить PiperTTS: %s", exc)

    @property
    def loaded(self) -> bool:
        return self._voice is not None

    def _ensure_loaded(self) -> None:
        if self._voice is not None:
            return
        if not self.voice_path.exists():
            raise TTSError(
                f"нет голоса Piper: {self.voice_path}. Запустите assistant/scripts/fetch-voice.sh"
            )
        from piper import PiperVoice

        self._voice = PiperVoice.load(str(self.voice_path))

    def synth(self, text: str) -> tuple[np.ndarray, int]:
        text = text.strip()
        if not text:
            raise TTSError("пустой текст для синтеза")
        self._ensure_loaded()
        from piper import SynthesisConfig

        config = SynthesisConfig(length_scale=self.length_scale, normalize_audio=True)
        chunks = list(self._voice.synthesize(text, config))
        if not chunks:
            raise TTSError("Piper не вернул аудио")

        rate = int(chunks[0].sample_rate)
        pieces: list[np.ndarray] = []
        for chunk in chunks:
            audio = np.asarray(chunk.audio_float_array, dtype=np.float32)
            if audio.ndim > 1:
                audio = audio.reshape(-1, audio.shape[-1]).mean(axis=1)
            pieces.append(audio.reshape(-1))
        return np.concatenate(pieces).astype(np.float32), rate


class GoogleTTS:
    """Быстрый облачный синтез через Google TTS HTTP endpoint (~0.25 с)."""

    name = "google"

    def __init__(self, voice: str = "ru") -> None:
        self.voice = voice
        import httpx

        self.client = httpx.Client(timeout=3.5)

    @property
    def loaded(self) -> bool:
        return True

    def synth(self, text: str) -> tuple[np.ndarray, int]:
        from .audio import read_wav

        text = text.strip()
        if not text:
            raise TTSError("пустой текст для синтеза")
        try:
            resp = self.client.get(
                "https://translate.google.com/translate_tts",
                params={"ie": "UTF-8", "tl": "ru", "client": "tw-ob", "q": text[:200]},
            )
            if resp.status_code >= 400 or not resp.content:
                raise TTSError(f"Google TTS HTTP {resp.status_code}")
        except Exception as exc:
            raise TTSError(f"Google TTS ошибка сети: {exc}") from exc

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f_in:
            f_in.write(resp.content)
            in_path = f_in.name
        out_path = in_path + ".wav"
        try:
            cmd = ["/usr/bin/afconvert", "-f", "WAVE", "-d", "LEI16@24000", in_path, out_path]
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            if proc.returncode != 0:
                raise TTSError("afconvert error on Google TTS")
            return read_wav(Path(out_path).read_bytes())
        finally:
            if os.path.exists(in_path):
                os.unlink(in_path)
            if os.path.exists(out_path):
                os.unlink(out_path)


def build_tts(settings: Settings):
    provider = settings.tts_provider
    if provider == "edge":
        return EdgeTTS(voice=settings.edge_voice)
    if provider == "piper":
        return PiperTTS(settings.piper_voice)
    if provider == "google":
        return GoogleTTS(settings.google_tts_voice)
    raise ValueError(f"неизвестный TTS_PROVIDER: {provider}")
