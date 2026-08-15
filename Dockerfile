FROM node:22-alpine AS web-build
WORKDIR /app/web
COPY web/package.json web/package-lock.json* ./
RUN npm install
COPY web/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY api ./api
COPY backtest ./backtest
COPY config ./config
COPY data ./data
COPY execution ./execution
COPY core ./core
COPY strategies ./strategies
COPY tests ./tests
COPY scripts ./scripts
COPY alpha_miner ./alpha_miner
COPY research_reviewer ./research_reviewer
COPY --from=web-build /app/web/dist ./web/dist

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD curl -f http://localhost:8000/health || exit 1
STOPSIGNAL SIGTERM
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--ws", "wsproto"]
