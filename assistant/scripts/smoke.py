#!/usr/bin/env python3
"""Проверка локального конвейера без HTTP: WAV -> ASR -> LLM -> TTS -> WAV."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.audio import read_wav, to_wav_bytes  # noqa: E402
from app.config import Settings  # noqa: E402

PHRASE = "Привет! Сколько будет два плюс два? И какая сегодня погода?"


def synth_test_wav(phrase: str) -> Path:
    aiff = Path(tempfile.mkstemp(suffix=".aiff")[1])
    wav = Path(tempfile.mkstemp(suffix=".wav")[1])
    subprocess.run(["say", "-v", "Milena", "-o", str(aiff), phrase], check=True)
    subprocess.run(
        ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(aiff), str(wav)],
        check=True,
    )
    aiff.unlink(missing_ok=True)
    return wav


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", type=Path, help="готовый WAV; иначе синтезируем через say")
    parser.add_argument("--text", help="пропустить ASR и проверить только LLM+TTS")
    parser.add_argument("--play", action="store_true", help="проиграть ответ через afplay")
    parser.add_argument("--save", type=Path, default=Path("smoke-reply.wav"))
    args = parser.parse_args()

    settings = Settings.load()
    print(f"ASR={settings.asr_provider}  LLM={settings.llm_provider}/{settings.llm_model}")
    print(f"TTS={settings.tts_provider}  voice={settings.piper_voice.name}")

    from app.pipeline import Assistant

    assistant = Assistant(settings)

    if args.text:
        transcript = args.text
        print(f"\n[1/3] ASR пропущен, текст: {transcript!r}")
    else:
        wav_path = args.audio or synth_test_wav(PHRASE)
        if args.audio is None:
            print(f"\n[0/3] тестовая фраза: {PHRASE!r}")
        samples, rate = read_wav(wav_path.read_bytes())
        print(f"      файл: {wav_path} ({samples.size / rate:.2f} с, {rate} Гц)")

        started = time.perf_counter()
        transcript = assistant.transcribe(samples, rate)
        asr_time = time.perf_counter() - started
        print(f"\n[1/3] ASR за {asr_time:.2f} с: {transcript!r}")
        if not transcript:
            print("ОШИБКА: пустой транскрипт", file=sys.stderr)
            return 1

    started = time.perf_counter()
    reply = assistant.answer(transcript)
    llm_time = time.perf_counter() - started
    print(f"\n[2/3] LLM за {llm_time:.2f} с: {reply!r}")

    started = time.perf_counter()
    audio, tts_rate = assistant.speak(reply)
    tts_time = time.perf_counter() - started
    args.save.write_bytes(audio)
    seconds = (len(audio) - 44) / 2 / tts_rate
    print(f"\n[3/3] TTS за {tts_time:.2f} с: {args.save} ({seconds:.2f} с, {tts_rate} Гц)")

    if args.play:
        subprocess.run(["afplay", str(args.save)], check=False)

    print("\nИтого без ASR: %.2f с" % (llm_time + tts_time))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
