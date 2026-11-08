FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data ENV_FILE=/data/.env BEHIND_PROXY=1 PORT=5000

# Fonts for the news cards and flyers (Arial-compatible, so the text fits as designed), Noto for Hindi and Gujarati, fribidi for their text shaping
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-liberation fonts-dejavu-core fonts-noto-core libfribidi0 ffmpeg tzdata curl postgresql-client \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY run.py ./
COPY technews ./technews
RUN useradd --system --uid 10001 --home /app app && mkdir /data && chown app /data
USER app
VOLUME /data
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fsS http://127.0.0.1:5000/health || exit 1

# One worker: the newsroom scheduler runs inside the app, and two workers would post twice.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "8", "--timeout", "180", "--access-logfile", "-", "run:app"]
