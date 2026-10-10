FROM python:3.13-slim

# ffmpeg merges video+audio and makes MP3s; node lets yt-dlp solve YouTube's JS challenges.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg nodejs \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=5050

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY static ./static

RUN useradd --create-home clipdrop
USER clipdrop

EXPOSE 5050

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/')"

# One worker on purpose: download jobs and progress live in process memory.
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT} --workers 1 --threads 16 --timeout 0 app:app"]
