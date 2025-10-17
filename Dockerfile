FROM python:3.13-slim

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install system libs for pylibsrtp optional
RUN apt-get update && apt-get install -y --no-install-recommends \
    libssl3 libssl-dev libsrtp2-1 libsrtp2-dev \
    && rm -rf /var/lib/apt/lists/*

# Install uv (static single-file)
RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml README.md /app/
COPY audiohooksipbridge /app/bridge

RUN uv pip install .

# Non-root user
RUN useradd -ms /bin/bash appuser
USER appuser

EXPOSE 8080 8081
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

ENV HTTP_PORT=8080 WS_PORT=8081

CMD ["uv", "run", "-m", "bridge.app"]
