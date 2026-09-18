# Single image used by both the API and the OCR worker so that the OCR code path
# in production is byte-identical to the one exercised by tests.
FROM python:3.12-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# tesseract-ocr-eng / -hin: OCR language data (English, Hindi)
# tesseract-ocr-script-deva: Devanagari script model, better on mixed-script labels
# fonts-noto-core: ships NotoSansDevanagari + NotoSerifDevanagari for PDF rendering
# libgl1 / libglib2.0-0: OpenCV runtime dependencies
# libjpeg / libwebp / zlib: Pillow image codecs
#
# The upgrade step is not cosmetic. A base image tag is rebuilt on its own schedule, so
# between rebuilds it carries whatever security updates Debian has published since. A
# trivy scan of this image found three HIGH severity issues in libpcre2, all of them
# already fixed in the Debian archive and none of them reachable without this step.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-hin \
        tesseract-ocr-script-deva \
        fonts-noto-core \
        libgl1 \
        libglib2.0-0 \
        libjpeg62-turbo \
        libwebp7 \
        zlib1g \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies come from the generated lock file so that image builds are
# reproducible. requirements.txt declares the direct pins it was resolved from.
COPY requirements.lock.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt

COPY pyproject.toml alembic.ini ./
COPY app ./app
COPY migrations ./migrations
COPY scripts ./scripts
# The tests ship with the image so a deployed build can verify itself, and so CI
# runs exactly the code that was built rather than a checkout beside it.
COPY tests ./tests

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin maanak \
    && mkdir -p /app/var \
    && chown -R maanak:maanak /app

USER maanak

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=25s --retries=6 \
    CMD curl -fsS http://127.0.0.1:8000/health/live || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
