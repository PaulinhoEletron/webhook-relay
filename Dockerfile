FROM ghcr.io/astral-sh/uv:0.11.16@sha256:440fd6477af86a2f1b38080c539f1672cd22acb1b1a47e321dba5158ab08864d AS uv
FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c AS base
COPY --from=uv /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MALLOC_ARENA_MAX=2 UV_CACHE_DIR=/tmp/uv-cache \
    UV_COMPILE_BYTECODE=1
WORKDIR /service
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
ENV PATH="/service/.venv/bin:$PATH"
COPY app ./app
COPY receiver ./receiver
COPY migrations ./migrations
COPY alembic.ini ./
COPY examples ./examples
# Bytecode is compiled at build time: the root filesystem is read-only at runtime.
RUN python -m compileall -q /usr/local/lib/python3.12 app receiver migrations examples
USER 10001:10001
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--no-proxy-headers", "--timeout-keep-alive", "5", "--limit-concurrency", "64"]

FROM base AS test
USER root
RUN uv sync --frozen --no-install-project
COPY tests ./tests
USER 10001:10001
CMD ["pytest", "-q", "-p", "no:cacheprovider"]

FROM base AS runtime
ARG VERSION=0.1.0
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.title="webhook-relay" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}"
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health/ready', timeout=4)"]
