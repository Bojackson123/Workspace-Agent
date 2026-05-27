# ---- Builder Stage ----
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Install dependencies first (cached layer unless pyproject.toml changes)
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-install-project --no-dev

# Copy application source and install the project itself
COPY . .
RUN uv sync --frozen --no-dev


# ---- Runtime Stage ----
FROM python:3.12-slim-bookworm AS runtime

# Security: run as non-root
RUN groupadd --system appgroup && \
    useradd --system --gid appgroup --no-create-home appuser

WORKDIR /app

# Copy only the virtual environment and source from builder
COPY --from=builder --chown=appuser:appgroup /app /app

# Place the venv on PATH so `python` resolves to the venv's interpreter
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER appuser

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
