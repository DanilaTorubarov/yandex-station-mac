"""PCM/WAV без внешних бинарников.

Станция отдаёт сырой S16_LE, 16 кГц, моно — ровно то, что нужно Whisper.
Всё остальное (WAV, стерео, 48 кГц) приводим здесь.
"""

from __future__ import annotations

import io
import wave
from math import gcd

import numpy as np


def pcm16le_to_float32(data: bytes) -> np.ndarray:
    """Сырой поток станции: little-endian int16, моно."""
    if len(data) % 2:
        data = data[:-1]
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0


def _decode(raw: bytes, width: int) -> np.ndarray:
    if width == 1:
        return (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    if width == 2:
        return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if width == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        val = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        val = np.where(val & 0x800000, val - 0x1000000, val)
        return val.astype(np.float32) / 8388608.0
    if width == 4:
        return np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    raise ValueError(f"неподдерживаемая разрядность WAV: {width * 8} бит")


def read_wav(data: bytes) -> tuple[np.ndarray, int]:
    """WAV -> (float32 моно, частота)."""
    with wave.open(io.BytesIO(data), "rb") as wav:
        channels = wav.getnchannels()
        rate = wav.getframerate()
        width = wav.getsampwidth()
        raw = wav.readframes(wav.getnframes())
    samples = _decode(raw, width)
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples.astype(np.float32), rate


def resample(samples: np.ndarray, rate_in: int, rate_out: int) -> np.ndarray:
    if rate_in == rate_out or samples.size == 0:
        return samples
    try:
        from scipy.signal import resample_poly

        step = gcd(rate_in, rate_out)
        return resample_poly(samples, rate_out // step, rate_in // step).astype(np.float32)
    except Exception:
        # Линейная интерполяция: хуже по алиасингу, но без scipy.
        count = int(round(samples.size * rate_out / rate_in))
        if count <= 0:
            return np.zeros(0, dtype=np.float32)
        src = np.arange(samples.size, dtype=np.float64)
        dst = np.linspace(0, samples.size - 1, count)
        return np.interp(dst, src, samples).astype(np.float32)


def to_wav_bytes(samples: np.ndarray, rate: int) -> bytes:
    """float32 [-1..1] -> WAV PCM_16."""
    pcm = np.clip(samples, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm.tobytes())
    return buf.getvalue()


def duration_seconds(samples: np.ndarray, rate: int) -> float:
    return samples.size / float(rate) if rate else 0.0
