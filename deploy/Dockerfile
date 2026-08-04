# Container image for the paper/live trading bot and its monitoring endpoint.
#
# It runs the paper trader by default (safe: fake money). Point it at live trading only
# after backtests, walk-forward validation, and a satisfying paper run -- and only by
# overriding the command AND setting the live safety flags/secrets (see README).
FROM python:3.11-slim

# Keep Python lean and log-friendly in containers.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install deps first so Docker layer-caching skips reinstalling on code-only changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Persisted paper state / audit logs live here; mount a volume to keep them across restarts.
RUN mkdir -p /app/data /app/logs
VOLUME ["/app/data", "/app/logs"]

# Monitoring endpoint (health + Prometheus metrics).
EXPOSE 8000

# Container-level liveness probe hits the bot's own /healthz.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz').status==200 else 1)" || exit 1

# Default: live-poll paper trading with monitoring on. Override `command` to change mode.
CMD ["python", "scripts/run_paper_trader.py", "live"]
