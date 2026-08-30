# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:0.12.7 AS uv
FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="Telemax" \
      org.opencontainers.image.description="Personal MAX ↔ Telegram bridge" \
      org.opencontainers.image.source="https://github.com/aipolukhin/telemax" \
      org.opencontainers.image.licenses="Apache-2.0"

COPY --from=uv /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    HOME=/state/home

WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --no-install-project --no-dev --python 3.12

COPY bridge ./bridge
RUN uv sync --locked --no-dev --no-editable --python 3.12

ENTRYPOINT ["/app/.venv/bin/telemax"]
CMD ["--config", "/state/config.yaml", "run"]
