FROM python:3.13-slim

# ffmpeg/ffprobe are required by the local media analysis and render workers.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md requirements-dev.lock ./
COPY index.html app.js styles.css ./
COPY login.html auth.js auth.css ./
COPY share.html share.js share.css ./
COPY web ./web
COPY third_party ./third_party
COPY app ./app
COPY media_gateway ./media_gateway
COPY timeline_engine ./timeline_engine
ARG INSTALL_EXTRAS=""
RUN if [ -n "$INSTALL_EXTRAS" ]; then \
      python -m pip install --no-cache-dir -c requirements-dev.lock ".[${INSTALL_EXTRAS}]"; \
    else \
      python -m pip install --no-cache-dir -c requirements-dev.lock .; \
    fi

# The storage volume is mounted here by Compose.  A non-root process keeps
# generated media and the API surface isolated in production-like runs.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/data/storage \
    && chown -R app:app /app
USER app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
