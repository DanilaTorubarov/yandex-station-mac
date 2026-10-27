# Локальный ассистент для станции

Оркестратор, который принимает звук со станции и отдаёт ответ голосом. Сейчас всё
считается локально на Mac:

```
PCM 16 кГц ──▶ mlx-whisper (large-v3-turbo) ──▶ Ollama (qwen3.5:4b) ──▶ Piper (ru_RU-irina)
                                                                              │
                                                                              ▼
                                                                         WAV / aplay
```

Облачные провайдеры (Groq ASR, Gemini LLM, Google TTS) уже предусмотрены в конфиге как
fallback, но включаются позже — см. `docs/ASSISTANT.md`, раздел «Облачный вариант».

## Быстрый старт

```sh
sh scripts/setup.sh     # Python 3.12, зависимости, голос Piper (~63 МБ)
sh scripts/smoke.sh     # WAV -> ASR -> LLM -> TTS, без HTTP
sh scripts/run.sh       # http://127.0.0.1:8730
```

Первый запуск скачивает модель Whisper (~1.6 ГБ) и грузит её в память — это самая долгая
операция. Ollama должна быть запущена, модель `qwen3.5:4b` — скачана (`ollama pull`).

## HTTP API

| Метод | Путь | Что делает |
|---|---|---|
| GET | `/health` | провайдеры, модели, доступность Ollama |
| POST | `/chat` | `{"text": "..."}` -> текстовый ответ |
| POST | `/say` | `{"text": "..."}` -> WAV; `?play=1` ещё и проиграть на Mac |
| POST | `/voice` | тело: сырой S16_LE моно -> JSON с транскриптом, ответом и WAV в base64 |
| POST | `/voice/wav` | тело: WAV -> то же самое |
| POST | `/reset` | очистить историю диалога |

Примеры:

```sh
curl -s localhost:8730/health | jq
curl -s -X POST localhost:8730/chat -H 'content-type: application/json' \
     -d '{"text":"Который час?"}' | jq -r .answer
curl -s -X POST localhost:8730/say -H 'content-type: application/json' \
     -d '{"text":"Проверка связи"}' -o say.wav && afplay say.wav
curl -s -X POST localhost:8730/voice/wav --data-binary @test.wav | jq '{transcript, answer, seconds}'
```

## Настройки

Все параметры — в `.env` (см. `.env.example`). Главное:

- `ASR_PROVIDER`, `LLM_PROVIDER`, `TTS_PROVIDER` — `mlx` / `ollama` / `piper` по умолчанию;
- `*_FALLBACK` — что подставлять при ошибке (`groq`, `gemini`, `google`);
- `LLM_MODEL` — `qwen3.5:4b` для скорости, `qwen3.5:9b` для качества;
- `PIPER_VOICE` — путь к `.onnx`; другие русские голоса: `ru_RU-dmitri-medium`,
  `ru_RU-denis-medium`, `ru_RU-ruslan-medium` (скачать: `PIPER_VOICE_NAME=... sh scripts/fetch-voice.sh`).

## Со станции

Станция пока не подключена: для неё нужен mic-tap (см. `docs/ASSISTANT.md`, этап 0).
Когда он появится, поток будет приходить сюда на `/voice`, а ответ — WAV, который станция
проиграет через `aplay`.

## Ограничения

- Mac должен быть включён; на питании помогает `caffeinate -s`.
- Первый запрос после простоя грузит Whisper (~секунды) и Ollama-модель.
- `afplay`/`aplay` — один владелец ALSA/звука за раз: если Алиса говорит, ответ подождёт.
