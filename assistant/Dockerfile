FROM python:3.12-slim

WORKDIR /app

RUN apt-get update -qq && \
    apt-get install -y --no-install-recommends \
        curl \
        ffmpeg \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock* /app/

RUN pip install --no-cache-dir \
    fastapi \
    "uvicorn[standard]" \
    httpx \
    numpy \
    faster-whisper \
    piper-tts

COPY app/ /app/app/
COPY voices/ /app/voices/

EXPOSE 8730

CMD ["uvicorn", "app.server:app", "--host", "0.0.0.0", "--port", "8730"]
