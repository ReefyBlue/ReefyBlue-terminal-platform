# One image for all five services (sim, gateway, chaos, ecs, soak). Build context: repo root.
#   docker build -f deploy/Dockerfile -t terminal-platform .
# The QNAP TS-473A is x86-64: linux/amd64 only.

# --- builder: resolve the locked workspace into /app/.venv ------------------------------------
FROM --platform=linux/amd64 ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.12
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY packages/ packages/
# Workspace members are installed editable: the venv points at /app/packages/*/src, which the
# runtime stage copies to the same path.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --all-packages

# --- runtime -----------------------------------------------------------------------------------
FROM --platform=linux/amd64 python:3.12-slim AS runtime
RUN groupadd --system --gid 1000 app \
 && useradd --system --uid 1000 --gid app --home-dir /app --shell /usr/sbin/nologin app \
 && mkdir -p /data/soak \
 && chown app:app /data/soak
WORKDIR /app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app pyproject.toml uv.lock terminal.yaml ./
COPY --chown=app:app packages/ packages/
COPY --chown=app:app deploy/scenarios/ deploy/scenarios/
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1
USER app
# Each service sets its own command in docker-compose.yml.
CMD ["python", "-m", "terminal_ecs_service.serve", "--layout", "/app/terminal.yaml"]
