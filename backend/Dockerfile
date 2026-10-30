FROM ghcr.io/astral-sh/uv:0.13.6 AS uv
FROM python:3.12.7-slim-bookworm
ARG INSTALL_VOICE=false
COPY --from=uv /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1
WORKDIR /srv
COPY pyproject.toml uv.lock ./
RUN if [ "$INSTALL_VOICE" = "true" ]; then \
      apt-get update && apt-get install -y --no-install-recommends libgomp1 libstdc++6 \
      && rm -rf /var/lib/apt/lists/* \
      && uv sync --frozen --no-dev --no-install-project --extra voice; \
    else uv sync --frozen --no-dev --no-install-project; fi
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
RUN if [ "$INSTALL_VOICE" = "true" ]; then uv sync --frozen --no-dev --extra voice; \
    else uv sync --frozen --no-dev; fi \
    && useradd --uid 10001 --create-home appuser
USER appuser
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["/srv/.venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health/live', timeout=3)"]
CMD ["/srv/.venv/bin/uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--loop", "app.loop:create_loop"]
