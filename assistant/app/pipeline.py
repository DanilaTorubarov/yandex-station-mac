"""Связка ASR -> LLM -> TTS с необязательным fallback на каждом звене."""

from __future__ import annotations

import logging
import re
import time
from collections import deque
from dataclasses import replace

import numpy as np

from .asr import build_asr
from .audio import to_wav_bytes
from .config import Settings
from .dev_tools import try_handle_dev_command
from .display_tools import try_handle_display_command
from .find_phone_tools import try_handle_find_phone_command
from .journal import record_call
from .llm import build_llm
from .mac_tools import copy_to_clipboard, paste_into_app, try_handle_mac_command
from .music_tools import try_handle_music_command
from .notes_calendar_tools import try_handle_calendar_command, try_handle_notes_command
from .realtime_tools import try_handle_realtime_query
from .reminders_tools import try_handle_reminders_command
from .timer_tools import stop_station_audio_and_timers, try_handle_timer_command
from .torrent_tools import try_handle_torrent_command
from .tts import build_tts

log = logging.getLogger(__name__)

_MARKDOWN = re.compile(r"[*_`#>]+")
_WAKE_PREFIX = re.compile(
    r"^[\s,.\-!?:;]*(?:эй[\s,.\-!?:;]+|слушай[\s,.\-!?:;]+|привет[\s,.\-!?:;]+)*"
    r"(?:алис[аеуыой]|аліс[ао]|яндекс)[\s,.\-!?:;]*",
    re.IGNORECASE,
)
_HALLUCINATIONS = re.compile(
    r"^(?:продолжение следует.*|субтитры\b.*|редактор субтитров.*|"
    r"спасибо за просмотр.*|подписывайтесь.*|dima\s*torzok.*)$",
    re.IGNORECASE,
)

_DETAILED_MODE_ON = (
    "включи подробный режим",
    "включи умный режим",
    "режим подробно",
    "умный режим",
    "отвечай подробно",
    "переключись в подробный режим",
    "переключись в умный режим",
)
_DETAILED_MODE_OFF = (
    "включи краткий режим",
    "выключи подробный режим",
    "выключи умный режим",
    "режим кратко",
    "краткий режим",
    "обычный режим",
    "отвечай кратко",
    "переключись в краткий режим",
)
_DETAILED_QUERY_TRIGGERS = (
    "расскажи подробно",
    "объясни подробно",
    "подробно расскажи",
    "подробно объясни",
    "расскажи детальн",
    "объясни детальн",
    "разбери подробно",
    "разбери детальн",
    "подумай хорошо",
    "подумай глубоко",
    "хорошенько подумай",
    "шаг за шагом",
    "по шагам",
    "в умном режиме",
    "максимально подробно",
    "расскажи всё про",
    "расскажи все про",
    "подробнее",
)


_STOP_COMMANDS = {
    "стоп",
    "stop",
    "стоп алиса",
    "алиса стоп",
    "хватит",
    "заткнись",
    "молчи",
    "тихо",
    "остановись",
    "отмена",
    "отставить",
    "отбой",
    "прекрати",
    "выключись",
    "замолчи",
    "останови",
    "замолчи пожалуйста",
    "хорош",
    "стоп игра",
    "пауза",
    "pause",
    "тишина",
    "выруби",
    "заглуши",
}


def is_stop_command(text: str) -> bool:
    """Определяет команды немедленной остановки (стоп, хватит, заткнись, отмена, выключи таймер)."""
    t = (text or "").lower().strip(" .,!?:;\t\n\r")
    if not t:
        return False
    t_clean = _WAKE_PREFIX.sub("", t).strip(" .,!?:;\t\n\r")
    if not t_clean:
        return False
    # Не перехватывать команды звонков в Discord/мессенджерах
    if any(w in t_clean for w in ("дискорд", "discord", "дс")):
        return False
    if t in _STOP_COMMANDS or t_clean in _STOP_COMMANDS:
        return True

    # Быстрый паттерн: выключение таймеров, будильников, музыки и сигналов любой длины фразы
    if re.search(
        r"\b(?:стоп|останови|прекрати|отмени|выключи|выруби|сбрось|заглуши|хватит|убери)\b.*"
        r"\b(?:таймер\w*|даймер\w*|будильник\w*|звонок\w*|сигнал\w*|музык\w*|звук\w*|алерт\w*)\b",
        t_clean,
    ):
        return True

    words = t_clean.split()
    if len(words) <= 4:
        stop_verbs = (
            "стоп",
            "stop",
            "хватит",
            "останови",
            "остановись",
            "прекрати",
            "отмени",
            "выключи",
            "выруби",
            "сбрось",
            "заглуши",
            "заткнись",
            "замолчи",
            "тихо",
            "пауза",
            "pause",
        )
        stop_targets = (
            "таймер",
            "таймеры",
            "даймер",
            "даймеры",
            "будильник",
            "будильники",
            "звонок",
            "сигнал",
            "музыку",
            "звук",
            "речь",
            "ответ",
        )
        if words[0] in stop_verbs:
            if len(words) == 1:
                return True
            if any(target in t_clean for target in stop_targets):
                return True
    return False


def is_detailed_query(text: str) -> bool:
    """Проверяет, просит ли пользователь развёрнутый ответ или глубокое рассуждение в конкретном вопросе."""
    t_low = (text or "").lower()
    if any(p in t_low for p in _DETAILED_QUERY_TRIGGERS):
        return True
    if re.search(r"\b(подробн\w+|детальн\w+|глубок\w+)\b", t_low):
        return True
    return False


def clean_for_speech(text: str) -> str:
    """Убираем markdown и лишние пробелы: текст пойдёт в TTS."""
    text = _MARKDOWN.sub("", text)
    text = re.sub(r"^\s*[-•]\s*", "", text, flags=re.MULTILINE)
    return " ".join(text.split())


def clean_transcript(text: str) -> str:
    """Убираем обращение «Алиса» в начале фразы и галлюцинации Whisper на тишине."""
    text = " ".join((text or "").split()).strip()
    if not text:
        return ""
    if _HALLUCINATIONS.match(text):
        return ""
    stripped = _WAKE_PREFIX.sub("", text).strip(" ,.-!?:;")
    if _HALLUCINATIONS.match(stripped):
        return ""
    return stripped


class Assistant:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.asr = build_asr(settings)
        self.asr_fallback = self._build_fallback(settings, "asr", build_asr)
        self.llm = build_llm(settings)
        self.llm_fallback = self._build_fallback(settings, "llm", build_llm)
        self.tts = build_tts(settings)
        self.tts_fallback = self._build_fallback(settings, "tts", build_tts)
        self.history: deque[dict[str, str]] = deque(maxlen=max(0, settings.history))
        self.detailed_mode: bool = False
        self.last_route: str = "LLM"

    @staticmethod
    def _build_fallback(settings: Settings, stage: str, factory):
        provider = getattr(settings, f"{stage}_fallback")
        if not provider:
            return None
        try:
            return factory(replace(settings, **{f"{stage}_provider": provider}))
        except Exception as exc:
            log.warning("fallback %s=%s не собран: %s", stage, provider, exc)
            return None

    def _call(self, stage: str, primary, fallback, method: str, *args, **kwargs):
        started = time.perf_counter()
        try:
            result = getattr(primary, method)(*args, **kwargs)
        except Exception as exc:
            log.warning("%s (%s) упал: %s", stage, getattr(primary, "name", "?"), exc)
            if fallback is None:
                raise
            result = getattr(fallback, method)(*args, **kwargs)
            primary = fallback
        log.debug("%s: %.2f с (%s)", stage, time.perf_counter() - started, primary.name)
        return result

    # --- звенья по отдельности -------------------------------------------

    def transcribe(self, samples: np.ndarray, rate: int) -> str:
        raw = self._call("ASR", self.asr, self.asr_fallback, "transcribe", samples, rate)
        cleaned = clean_transcript(raw)
        if raw and not cleaned:
            log.info("отфильтрована пустая/служебная фраза ASR: '%s'", raw)
        return cleaned

    def _try_switch_response_mode(self, text: str) -> str | None:
        t_low = (text or "").strip().lower()
        if len(t_low.split()) <= 6:
            if any(p in t_low for p in _DETAILED_MODE_OFF):
                self.detailed_mode = False
                log.info("Переключён режим ответа -> КРАТКИЙ")
                return "Включила краткий режим. Теперь буду отвечать быстро и по делу."
            if any(p in t_low for p in _DETAILED_MODE_ON):
                self.detailed_mode = True
                log.info("Переключён режим ответа -> ПОДРОБНЫЙ (УМНЫЙ)")
                return "Включила подробный умный режим. Теперь буду разбирать вопросы глубоко и по шагам."
        return None

    def _last_assistant_reply(self) -> str:
        for item in reversed(self.history):
            if item.get("role") == "assistant" and item.get("content"):
                return item["content"]
        return ""

    def _try_handle_history_clipboard_command(self, text: str) -> str | None:
        """
        Обрабатывает команды работы с предыдущим ответом ассистента:
        - «добавь этот текст в буфер обмена» / «скопируй это в буфер обмена» / «скопируй последний ответ»
        - «вставь этот текст» / «вставь свой ответ»
        """
        t_low = (text or "").strip().lower()
        last_reply = self._last_assistant_reply()
        if not last_reply:
            return None

        if re.search(
            r"\b(?:добавь|скопир\w+|положи|сохрани|запиши)\b.*"
            r"\b(?:этот\s+текст|это|свой\s+ответ|твой\s+ответ|последний\s+ответ|его|текст)\b.*"
            r"(?:\bв\s+буфер\w*|\bбуфер\w*|\bпироб\w*)",
            t_low,
        ) or (
            any(w in t_low for w in ("этот текст", "свой ответ", "твой ответ", "последний ответ"))
            and any(w in t_low for w in ("буфер", "буффер", "скопир", "пироб"))
        ):
            copy_to_clipboard(last_reply)
            log.info("Скопирован предыдущий ответ ассистента в буфер обмена: '%s'", last_reply)
            return f"Скопировала в буфер обмена: {last_reply}"

        if re.search(
            r"\b(?:вставь|напечатай|закинь|скинь)\b.*"
            r"\b(?:этот\s+текст|это|свой\s+ответ|твой\s+ответ|последний\s+ответ|его)\b.*"
            r"(?:туда|в\s+чат|в\s+окно|в\s+поле)?",
            t_low,
        ):
            copy_to_clipboard(last_reply)
            ok, used_app = paste_into_app()
            if ok:
                return f"Вставила текст в {used_app}."
            return "Скопировала текст в буфер обмена, можете вставить через Command V."

        return None

    def answer(self, text: str) -> str:
        if is_stop_command(text):
            log.info("Команда остановки: '%s'", text)
            stop_station_audio_and_timers()
            self.last_route = "Стоп"
            return "Остановила."

        mode_reply = self._try_switch_response_mode(text)
        if mode_reply:
            self.last_route = "Режим ответа"
            return mode_reply

        hist_clip_reply = self._try_handle_history_clipboard_command(text)
        if hist_clip_reply:
            self.last_route = "Буфер обмена"
            return hist_clip_reply

        try:
            disp_reply = try_handle_display_command(text)
            if disp_reply:
                reply = clean_for_speech(disp_reply)
                self.history.append({"role": "user", "content": text})
                self.history.append({"role": "assistant", "content": reply})
                self.last_route = "Дисплей колонки"
                return reply
        except Exception as exc:
            log.warning("Ошибка выполнения команды дисплея: %s", exc)

        try:
            phone_reply = try_handle_find_phone_command(text)
            if phone_reply:
                reply = clean_for_speech(phone_reply)
                self.history.append({"role": "user", "content": text})
                self.history.append({"role": "assistant", "content": reply})
                self.last_route = "Локатор iPhone"
                return reply
        except Exception as exc:
            log.warning("Ошибка выполнения команды поиска телефона: %s", exc)

        try:
            rem_reply = try_handle_reminders_command(text, self.llm)
            if rem_reply:
                reply = clean_for_speech(rem_reply)
                self.history.append({"role": "user", "content": text})
                self.history.append({"role": "assistant", "content": reply})
                self.last_route = "Напоминания"
                return reply
        except Exception as exc:
            log.warning("Ошибка выполнения команды Напоминаний: %s", exc)

        try:
            tmr_reply = try_handle_timer_command(text, self.speak)
            if tmr_reply:
                reply = clean_for_speech(tmr_reply)
                self.history.append({"role": "user", "content": text})
                self.history.append({"role": "assistant", "content": reply})
                self.last_route = "Таймеры"
                return reply
        except Exception as exc:
            log.warning("Ошибка выполнения команды таймера/напоминания: %s", exc)

        try:
            cal_reply = try_handle_calendar_command(text, self.llm)
            if cal_reply:
                reply = clean_for_speech(cal_reply)
                self.history.append({"role": "user", "content": text})
                self.history.append({"role": "assistant", "content": reply})
                self.last_route = "Календарь"
                return reply
        except Exception as exc:
            log.warning("Ошибка выполнения команды Календаря: %s", exc)

        try:
            note_reply = try_handle_notes_command(text, self.llm)
            if note_reply:
                reply = clean_for_speech(note_reply)
                self.history.append({"role": "user", "content": text})
                self.history.append({"role": "assistant", "content": reply})
                self.last_route = "Заметки"
                return reply
        except Exception as exc:
            log.warning("Ошибка выполнения команды Заметок: %s", exc)

        try:
            torr_reply = try_handle_torrent_command(text, self.llm)
            if torr_reply:
                reply = clean_for_speech(torr_reply)
                self.history.append({"role": "user", "content": text})
                self.history.append({"role": "assistant", "content": reply})
                self.last_route = "Торренты"
                return reply
        except Exception as exc:
            log.warning("Ошибка выполнения команды торрентов: %s", exc)

        try:
            dev_reply = try_handle_dev_command(text, self.llm)
            if dev_reply:
                reply = clean_for_speech(dev_reply)
                self.history.append({"role": "user", "content": text})
                self.history.append({"role": "assistant", "content": reply})
                self.last_route = "Git / Dev"
                return reply
        except Exception as exc:
            log.warning("Ошибка выполнения команды разработки/терминала: %s", exc)

        try:
            mus_reply = try_handle_music_command(text)
            if mus_reply:
                reply = clean_for_speech(mus_reply)
                self.history.append({"role": "user", "content": text})
                self.history.append({"role": "assistant", "content": reply})
                self.last_route = "Apple Music"
                return reply
        except Exception as exc:
            log.warning("Ошибка выполнения команды музыки: %s", exc)

        try:
            mac_reply = try_handle_mac_command(text, self.llm)
            if mac_reply:
                reply = clean_for_speech(mac_reply)
                self.history.append({"role": "user", "content": text})
                self.history.append({"role": "assistant", "content": reply})
                self.last_route = "Mac OS"
                return reply
        except Exception as exc:
            log.warning("Ошибка выполнения команды Mac: %s", exc)

        try:
            rt_reply = try_handle_realtime_query(text, self.llm)
            if rt_reply:
                reply = clean_for_speech(rt_reply)
                self.history.append({"role": "user", "content": text})
                self.history.append({"role": "assistant", "content": reply})
                self.last_route = "Погода / Время"
                return reply
        except Exception as exc:
            log.warning("Ошибка получения данных реального времени: %s", exc)

        use_detailed = self.detailed_mode or is_detailed_query(text)
        if use_detailed:
            log.info("Запрос в режиме ПОДРОБНО/УМНЫЙ: '%s'", text)
        messages = [*self.history, {"role": "user", "content": text}]
        reply = clean_for_speech(
            self._call("LLM", self.llm, self.llm_fallback, "chat", messages, detailed=use_detailed)
        )
        if not reply:
            raise RuntimeError("пустой ответ модели")
        if re.search(r"\b(?:в\s+буфер\w*|буфер\w*\s+обмен\w*|скопир\w+|копир\w+|пироб\w*)\b", text.lower()):
            copy_to_clipboard(reply)
        self.history.append({"role": "user", "content": text})
        self.history.append({"role": "assistant", "content": reply})
        self.last_route = "LLM (Умный)" if use_detailed else "LLM"
        return reply

    def speak(self, text: str) -> tuple[bytes, int]:
        samples, rate = self._call("TTS", self.tts, self.tts_fallback, "synth", text)
        return to_wav_bytes(samples, rate), rate

    # --- полный цикл ------------------------------------------------------

    def voice(self, samples: np.ndarray, rate: int) -> dict:
        started = time.perf_counter()
        if hasattr(self.tts, "warmup"):
            self.tts.warmup()
        transcript = self.transcribe(samples, rate)
        asr_done = time.perf_counter()
        if not transcript:
            return {
                "transcript": "",
                "answer": "",
                "audio": None,
                "sample_rate": 0,
                "seconds": {"asr": asr_done - started, "total": asr_done - started},
            }

        if is_stop_command(transcript):
            log.info("Голосовая команда остановки: '%s' -> глушим звук и таймеры", transcript)
            stop_station_audio_and_timers()
            secs = {"asr": asr_done - started, "total": time.perf_counter() - started}
            record_call(
                source="voice",
                transcript=transcript,
                answer="[Звук и таймеры остановлены]",
                route="Стоп",
                seconds=secs,
            )
            return {
                "transcript": transcript,
                "answer": "",
                "audio": None,
                "sample_rate": 0,
                "seconds": secs,
            }

        reply = self.answer(transcript)
        llm_done = time.perf_counter()
        wav, tts_rate = self.speak(reply)
        done = time.perf_counter()
        secs = {
            "asr": asr_done - started,
            "llm": llm_done - asr_done,
            "tts": done - llm_done,
            "total": done - started,
        }
        record_call(
            source="voice",
            transcript=transcript,
            answer=reply,
            route=self.last_route,
            seconds=secs,
        )
        return {
            "transcript": transcript,
            "answer": reply,
            "audio": wav,
            "sample_rate": tts_rate,
            "seconds": secs,
        }

    def reset(self) -> None:
        self.history.clear()

    def health(self) -> dict:
        return {
            "asr": {
                "primary": getattr(self.asr, "name", "?"),
                "model": getattr(self.asr, "model", ""),
                "fallback": getattr(self.asr_fallback, "name", None),
                "loaded": getattr(self.asr, "loaded", None),
            },
            "llm": {
                "primary": getattr(self.llm, "name", "?"),
                "model": getattr(self.llm, "model", ""),
                "fallback": getattr(self.llm_fallback, "name", None),
            },
            "tts": {
                "primary": getattr(self.tts, "name", "?"),
                "voice": str(getattr(self.tts, "voice", "") or getattr(self.tts, "voice_path", "")),
                "fallback": getattr(self.tts_fallback, "name", None),
                "loaded": getattr(self.tts, "loaded", None),
            },
            "history": len(self.history),
        }
