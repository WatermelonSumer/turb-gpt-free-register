# syntax=docker/dockerfile:1

FROM python:3.12-slim-bookworm AS python-wheels

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

# Build wheels separately so compilers do not remain in the runtime image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN python -m pip install --upgrade pip \
    && python -m pip wheel --wheel-dir=/wheels \
        -r requirements.txt


FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=Asia/Shanghai

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        tini \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
COPY --from=python-wheels /wheels /wheels

RUN python -m pip install --no-index --find-links=/wheels \
        -r requirements.txt \
    && rm -rf /wheels

# Install the system libraries used by local Playwright/CloakBrowser modes.
# Browser Use and Roxy modes connect to their own remote/host browser service.
RUN python -m playwright install-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# Runtime credentials and account data are intentionally excluded by .dockerignore
# and supplied through the bind mount in docker-compose.yml.
COPY . .

EXPOSE 5000

ENTRYPOINT ["tini", "--"]
CMD ["python", "web.py", "--host", "0.0.0.0", "--port", "5000"]
