# Stage 1: Build React 19 editorial frontend
FROM node:22-alpine AS frontend-builder
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci || npm install
COPY frontend/ ./
RUN npm run build

# Stage 2: Python 3.13 FastAPI + SPA runtime
FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    STATIC_DIR=/app/static

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
      curl \
      ca-certificates \
      fonts-noto-cjk \
      libxml2 \
      libxslt1.1 \
      libjpeg62-turbo \
      libwebp7 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY backend ./backend
RUN pip install --upgrade pip && pip install .

COPY --from=frontend-builder /web/dist /app/static

RUN mkdir -p /data/library

EXPOSE 8000

CMD ["sh", "-c", "alembic -c /app/backend/inkpress/db/alembic.ini upgrade head && uvicorn inkpress.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'"]
