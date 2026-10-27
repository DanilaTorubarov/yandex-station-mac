"""Голосовые уведомления с Mac на Яндекс Станцию (POST /say + авто-мониторинг скачиваний и задач)."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np

from .audio import to_wav_bytes

log = logging.getLogger(__name__)

STATION_HOST = os.environ.get("STATION_IP", "192.168.2.83")
STATION_PORT = os.environ.get("STATION_SSH_PORT", "2222")
_PLAY_LOCK = threading.Lock()


def _generate_notify_chime(rate: int) -> np.ndarray:
    """Короткий мягкий двухнотный сигнал уведомления (G5 -> D6, 0.42 с)."""
    notes_hz = [783.99, 1174.66]
    total_sec = 0.45
    total_samples = int(rate * total_sec)
    out = np.zeros(total_samples, dtype=np.float32)

    for idx, freq in enumerate(notes_hz):
        start_s = idx * 0.13
        start_i = int(start_s * rate)
        dur_s = total_sec - start_s
        n = int(dur_s * rate)
        if n <= 0:
            continue
        t = np.linspace(0.0, dur_s, n, endpoint=False, dtype=np.float32)
        env = np.exp(-6.5 * t) * (1.0 - np.exp(-80.0 * t))
        wave = 0.75 * np.sin(2.0 * np.pi * freq * t) + 0.25 * np.sin(2.0 * np.pi * (freq * 2.0) * t)
        out[start_i : start_i + n] += 0.28 * wave * env

    peak = float(np.max(np.abs(out)))
    if peak > 0:
        out = out / peak * 0.45
    return out.astype(np.float32)


def build_notification_wav(
    text: str,
    tts_speak_fn: Callable[[str], tuple[bytes, int]],
    with_chime: bool = True,
) -> tuple[bytes, int]:
    """Синтезирует речь и при необходимости добавляет в начало мягкий сигнал уведомления."""
    speech_wav, rate = tts_speak_fn(text)
    pcm_int16 = np.frombuffer(speech_wav[44:], dtype="<i2")
    speech_samples = pcm_int16.astype(np.float32) / 32767.0
    if not with_chime:
        return speech_wav, rate

    chime = _generate_notify_chime(rate)
    pause = np.zeros(int(rate * 0.14), dtype=np.float32)
    combined = np.concatenate([chime, pause, speech_samples])
    return to_wav_bytes(combined, rate), rate


def play_wav_on_station(wav_bytes: bytes, fallback_mac: bool = False) -> bool:
    """
    Отправляет WAV на Яндекс Станцию Мини 2 (192.168.2.83:2222), освобождает ALSA от фонового
    AirPlay/mediad при необходимости и воспроизводит через aplay -N (без блокировки).
    """
    with _PLAY_LOCK:
        remote_cmd = (
            "cat > /tmp/mac_notify.wav && "
            "for p in $(ps -o pid,args 2>/dev/null | awk '/aplay.*mac_notify/ && !/awk/ {print $1}'); do kill -15 \"$p\" 2>/dev/null; done; "
            "ALSA_CONFIG_PATH=/etc/alsa-mictap.conf /usr/bin/aplay -N -q -D dmix_spk_plug /tmp/mac_notify.wav; "
            "rm -f /tmp/mac_notify.wav"
        )
        try:
            res = subprocess.run(
                [
                    "ssh",
                    "-p",
                    STATION_PORT,
                    "-o",
                    "ConnectTimeout=3",
                    "-o",
                    "StrictHostKeyChecking=no",
                    f"root@{STATION_HOST}",
                    remote_cmd,
                ],
                input=wav_bytes,
                capture_output=True,
                timeout=15,
            )
            if res.returncode == 0:
                return True
            log.warning(
                "Не удалось проиграть уведомление на колонке (code=%s): %s",
                res.returncode,
                res.stderr.decode("utf-8", errors="ignore").strip(),
            )
        except Exception as exc:
            log.warning("Ошибка SSH при отправке уведомления на колонку: %s", exc)

        if fallback_mac:
            try:
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                    f.write(wav_bytes)
                    tmp_path = f.name
                subprocess.run(["/usr/bin/afplay", tmp_path], check=False, timeout=30)
                Path(tmp_path).unlink(missing_ok=True)
                return True
            except Exception:
                pass
        return False


def announce_on_station_async(
    text: str,
    tts_speak_fn: Callable[[str], tuple[bytes, int]],
    with_chime: bool = True,
) -> None:
    """Асинхронно озвучивает уведомление на колонке, не блокируя вызывающий процесс."""

    def _worker() -> None:
        try:
            wav, _ = build_notification_wav(text, tts_speak_fn, with_chime=with_chime)
            log.info("Голосовое уведомление на колонку: '%s'", text)
            play_wav_on_station(wav, fallback_mac=False)
        except Exception as exc:
            log.warning("Ошибка озвучки уведомления '%s': %s", text, exc)

    threading.Thread(target=_worker, daemon=True).start()


# --- Фоновый мониторинг завершённых скачиваний в ~/Downloads -----------------

_TEMP_DOWNLOAD_SUFFIXES = (".crdownload", ".download", ".part", ".tmp", ".partial")


def _humanize_filename_for_speech(filename: str) -> str:
    stem = Path(filename).stem
    ext = Path(filename).suffix.lstrip(".").lower()
    # Оставляем только читаемые слова/цифры, обрезаем длинные хэши
    clean_stem = re.sub(r"[_\-]+", " ", stem)
    clean_stem = re.sub(r"\b[0-9a-f]{12,}\b", "", clean_stem, flags=re.IGNORECASE)
    clean_stem = " ".join(clean_stem.split())[:45].strip()
    if not clean_stem:
        return f"файл {ext}" if ext else "новый файл"
    return clean_stem


class MacEventsWatcher:
    """
    Лёгкий фоновый наблюдатель (0 МБ лишней памяти):
    следит за папкой ~/Downloads и озвучивает на колонке, когда скачивание файла завершилось.
    """

    def __init__(self, tts_speak_fn: Callable[[str], tuple[bytes, int]]) -> None:
        self.tts_speak_fn = tts_speak_fn
        self.downloads_dir = Path.home() / "Downloads"
        self._stop = threading.Event()
        self._known_files: set[str] = set()
        self._last_announce_ts = 0.0

    def _snapshot_downloads(self) -> set[str]:
        if not self.downloads_dir.is_dir():
            return set()
        res: set[str] = set()
        try:
            for entry in self.downloads_dir.iterdir():
                name = entry.name
                if name.startswith(".") or name.lower().endswith(_TEMP_DOWNLOAD_SUFFIXES):
                    continue
                res.add(name)
        except Exception:
            pass
        return res

    def start(self) -> None:
        self._known_files = self._snapshot_downloads()
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(4.0):
            try:
                current = self._snapshot_downloads()
                new_files = current - self._known_files
                self._known_files = current
                if new_files and (time.time() - self._last_announce_ts > 5.0):
                    # Ждём 1 с и проверяем, что файл не временный и не удалён
                    time.sleep(1.0)
                    still_there = [f for f in new_files if (self.downloads_dir / f).exists()]
                    if still_there:
                        self._last_announce_ts = time.time()
                        if len(still_there) == 1:
                            spoken_name = _humanize_filename_for_speech(still_there[0])
                            msg = f"На компьютере завершилось скачивание файла {spoken_name}."
                        else:
                            msg = f"На компьютере завершилось скачивание {len(still_there)} файлов."
                        announce_on_station_async(msg, self.tts_speak_fn, with_chime=True)
            except Exception as exc:
                log.debug("Ошибка мониторинга Downloads: %s", exc)
