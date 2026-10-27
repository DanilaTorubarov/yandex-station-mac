"""Настройки оркестратора: .env + переменные окружения."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_RATE = 16_000


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Минимальный парсер .env: без зависимостей и без перезаписи окружения."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ[key] = value


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    language: str

    asr_provider: str
    asr_fallback: str
    asr_model: str
    groq_api_key: str

    llm_provider: str
    llm_fallback: str
    llm_chain: str
    llm_model: str
    groq_llm_model: str
    groq_vision_model: str
    openrouter_api_key: str
    openrouter_model: str
    openrouter_vision_model: str
    gemini_model: str
    gemini_api_key: str
    ollama_url: str

    tts_provider: str
    tts_fallback: str
    piper_voice: Path
    edge_voice: str
    google_tts_voice: str

    history: int

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv()
        voice = Path(_env("PIPER_VOICE", "voices/ru_RU-irina-medium.onnx"))
        if not voice.is_absolute():
            voice = ROOT / voice
        return cls(
            host=_env("ASSISTANT_HOST", "127.0.0.1"),
            port=_int("ASSISTANT_PORT", 8730),
            language=_env("ASSISTANT_LANGUAGE", "ru"),
            asr_provider=_env("ASR_PROVIDER", "groq").lower(),
            asr_fallback=_env("ASR_FALLBACK", "mlx").lower(),
            asr_model=_env("ASR_MODEL", "mlx-community/whisper-large-v3-turbo"),
            groq_api_key=_env("GROQ_API_KEY", ""),
            llm_provider=_env("LLM_PROVIDER", "chain").lower(),
            llm_fallback=_env("LLM_FALLBACK", "").lower(),
            llm_chain=_env("LLM_CHAIN", "groq,openrouter,gemini,ollama").lower(),
            llm_model=_env("LLM_MODEL", "qwen3.5:4b"),
            groq_llm_model=_env("GROQ_LLM_MODEL", "qwen/qwen3.8-27b"),
            groq_vision_model=_env("GROQ_VISION_MODEL", "qwen/qwen3.8-27b"),
            openrouter_api_key=_env("OPENROUTER_API_KEY", ""),
            openrouter_model=_env(
                "OPENROUTER_MODEL",
                "google/gemma-4-31b-it:free,google/gemma-4-26b-a4b-it:free,nvidia/nemotron-3.5-lightning:free,openrouter/free",
            ),
            openrouter_vision_model=_env(
                "OPENROUTER_VISION_MODEL",
                "nvidia/nemotron-3.5-super-vl:free,google/gemma-4-31b-it:free,openrouter/free",
            ),
            gemini_model=_env("GEMINI_MODEL", "gemini-3.8-flash"),
            gemini_api_key=_env("GEMINI_API_KEY", ""),
            ollama_url=_env("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/"),
            tts_provider=_env("TTS_PROVIDER", "edge").lower(),
            tts_fallback=_env("TTS_FALLBACK", "piper").lower(),
            piper_voice=voice,
            edge_voice=_env("EDGE_VOICE", "ru-RU-SvetlanaNeural"),
            google_tts_voice=_env("GOOGLE_TTS_VOICE", "ru-RU-Wavenet-C"),
            history=_int("ASSISTANT_HISTORY", 6),
        )
