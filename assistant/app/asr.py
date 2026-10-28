"""Распознавание речи: облачный Groq Whisper (0 МБ RAM, ~0.25 с) с локальным фоллбэком MLX-Whisper."""

from __future__ import annotations

import gc
import logging

import httpx
import numpy as np

from .audio import resample, to_wav_bytes
from .config import SAMPLE_RATE, Settings

log = logging.getLogger(__name__)

_WHISPER_INITIAL_PROMPT = (
    "Алиса, открой Ютуб, Телеграм, Дискорд, Ватсап, Ворд, Гугл Документы, Блокнот. "
    "Поставь таймер на 10 минут, напомни через полчаса выключить духовку, поставь будильник на 8 утра, "
    "сколько осталось на таймере, отмени таймер. "
    "Включи демку, выключи демку, выключи микрофон, включи микрофон, замуть меня, включи вебку. "
    "Сделай звук на компе тише, громкость на 50 процентов, сколько процентов заряда на маке, "
    "скрой все окна, спрячь все окна, сверни все окна, покажи рабочий стол, верни окна, "
    "заблокируй экран компа, усыпи мак, сделай скриншот экрана, открой папку Загрузки, Рабочий стол. "
    "Переведи то, что я скопировал, исправь ошибки в тексте из буфера обмена и вставь обратно, "
    "кратко перескажи скопированный текст. "
    "Сколько времени, какая сейчас погода, какой курс доллара и евро, сколько стоит биткоин, какие сегодня новости. "
    "Открой чат с Мишей в Телеграме и напиши, что я выхожу. "
    "Включи на компе видео про космос, поставь на паузу. "
    "Закрой вкладку, следующая вкладка, открой историю, перескажи открытую страницу, открой сайт a.solw.me, solw.me. "
    "Посмотри на экран, объясни ошибку, реши задачу, переведи то что открыто."
)


class ASRError(RuntimeError):
    pass


def _prepare_audio(samples: np.ndarray, rate: int) -> np.ndarray | None:
    if samples.size == 0:
        return None
    audio = resample(samples.astype(np.float32), rate, SAMPLE_RATE)
    if audio.size < SAMPLE_RATE // 10:  # короче 100 мс — не речь
        return None
    peak = float(np.max(np.abs(audio)))
    if peak < 0.001:  # в vqe_0 тишина <= 0.00025 (int16 <= 8), речь >= 0.003
        return None
    if peak < 0.35:
        audio = np.clip(audio * (0.4 / peak), -1.0, 1.0)
    return audio


class GroqASR:
    """Whisper large-v3-turbo через Groq LPU (0 МБ RAM на маке, скорость ~0.25 с)."""

    name = "groq"
    url = "https://api.groq.com/openai/v1/audio/transcriptions"

    def __init__(self, api_key: str, language: str, model: str = "whisper-large-v3-turbo") -> None:
        if not api_key:
            raise ASRError("ASR_PROVIDER=groq, но GROQ_API_KEY не задан")
        self.api_key = api_key
        self.language = language
        self.model = model
        self.client = httpx.Client(
            timeout=httpx.Timeout(12.0, connect=2.5),
            headers={"Authorization": f"Bearer {self.api_key}"},
        )

    @property
    def loaded(self) -> bool:
        return True

    def transcribe(self, samples: np.ndarray, rate: int) -> str:
        audio = _prepare_audio(samples, rate)
        if audio is None:
            return ""
        wav = to_wav_bytes(audio, SAMPLE_RATE)
        try:
            response = self.client.post(
                self.url,
                files={"file": ("audio.wav", wav, "audio/wav")},
                data={
                    "model": self.model,
                    "language": self.language,
                    "prompt": _WHISPER_INITIAL_PROMPT[:220],
                    "response_format": "json",
                    "temperature": "0",
                },
            )
        except httpx.HTTPError as exc:
            raise ASRError(f"Groq ASR сеть: {exc}") from exc
        if response.status_code >= 400:
            raise ASRError(f"Groq ASR {response.status_code}: {response.text[:300]}")
        payload = response.json()
        return " ".join((payload.get("text") or "").split())


class WhisperASR:
    """Локальный MLX-Whisper (фоллбэк на случай отсутствия интернета или лимитов Groq)."""

    name = "mlx"

    def __init__(self, model: str, language: str) -> None:
        self.model = model
        self.language = language
        self._loaded = False

    @property
    def loaded(self) -> bool:
        return self._loaded

    def transcribe(self, samples: np.ndarray, rate: int) -> str:
        audio = _prepare_audio(samples, rate)
        if audio is None:
            return ""
        try:
            import mlx.core as mx
            import mlx_whisper
        except ImportError as exc:
            raise ASRError("mlx-whisper не установлен") from exc

        result = mlx_whisper.transcribe(
            audio,
            path_or_hf_repo=self.model,
            language=self.language,
            task="transcribe",
            temperature=0.0,
            condition_on_previous_text=False,
            initial_prompt=_WHISPER_INITIAL_PROMPT,
            verbose=False,
        )
        self._loaded = True
        text = (result.get("text") or "").strip()
        # Освобождаем кэш Metal GPU после офлайн-распознавания, чтобы не держать лишнюю память
        try:
            mx.clear_cache()
        except Exception:
            pass
        gc.collect()
        return " ".join(text.split())


def build_asr(settings: Settings):
    provider = settings.asr_provider
    if provider == "groq":
        return GroqASR(settings.groq_api_key, settings.language)
    if provider == "mlx":
        return WhisperASR(settings.asr_model, settings.language)
    raise ValueError(f"неизвестный ASR_PROVIDER: {provider}")
