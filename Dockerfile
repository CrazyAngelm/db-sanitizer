# syntax=docker/dockerfile:1
# Pinned base images: Python 3.12, uv, and Greenmask 0.2.22.
FROM python@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b AS python-runtime
FROM ghcr.io/astral-sh/uv@sha256:ff07b86af50d4d9391d9daf4ff89ce427bc544f9aae87057e69a1cc0aa369946 AS uv-runtime

# Greenmask supplies the pinned data-plane binary and PostgreSQL 16 client tools.
FROM greenmask/greenmask@sha256:dad5506baf096965a6e2b6a82f00cc7a361102efdf4c6b4b491ae3c098475254 AS runtime

USER root

COPY --from=python-runtime /usr/local /usr/local
COPY --from=uv-runtime /uv /uvx /bin/

ENV PATH="/app/.venv/bin:/usr/local/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    LANGGRAPH_STRICT_MSGPACK=true \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN python --version && greenmask --version && uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY config ./config
COPY demo ./demo
COPY templates/run-report.schema.json ./templates/run-report.schema.json
RUN uv sync --frozen --no-dev && mkdir -p /app/.runs && chown -R greenmask:greenmask /app

USER greenmask
ENTRYPOINT ["db-sanitizer"]
