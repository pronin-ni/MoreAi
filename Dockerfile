FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    gnupg \
    curl \
    jq \
    libglib2.0-0 \
    libnss3 \
    libnspr4 \
    libdbus-1-3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    && rm -rf /var/lib/apt/lists/*

# Install OpenCode CLI for agent model support
RUN ARCH=$(dpkg --print-architecture 2>/dev/null || echo "amd64") \
    && case "$ARCH" in \
        amd64) OC_ARCH="x64" ;; \
        arm64) OC_ARCH="arm64" ;; \
        *) OC_ARCH="x64" ;; \
    esac \
    && echo "Installing opencode for $OC_ARCH..." \
    && curl -fsSL "https://github.com/anomalyco/opencode/releases/latest/download/opencode-linux-${OC_ARCH}.tar.gz" -o /tmp/opencode.tar.gz \
    && tar -xzf /tmp/opencode.tar.gz -C /usr/local/bin opencode \
    && rm /tmp/opencode.tar.gz \
    && opencode version || true

# Install Kilocode CLI for agent model support
RUN ARCH=$(dpkg --print-architecture 2>/dev/null || echo "amd64") \
    && case "$ARCH" in \
        amd64) KC_ARCH="x64" ;; \
        arm64) KC_ARCH="arm64" ;; \
        *) KC_ARCH="x64" ;; \
    esac \
    && echo "Installing kilocode for $KC_ARCH..." \
    && curl -fsSL "https://github.com/kilocode/kilocode/releases/latest/download/kilocode-linux-${KC_ARCH}.tar.gz" -o /tmp/kilocode.tar.gz \
    && tar -xzf /tmp/kilocode.tar.gz -C /usr/local/bin kilocode \
    && rm /tmp/kilocode.tar.gz \
    && kilocode version || true

COPY pyproject.toml .
COPY app/ ./app/
RUN pip install uv && uv sync --all-extras

RUN .venv/bin/python -m playwright install --with-deps chromium

ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD [".venv/bin/python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
