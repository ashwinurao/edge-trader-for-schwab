# Edge Trader on a home server (see "Running on a home server" in the README).
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    COMPANION_HOST=0.0.0.0 EDGE_TRADER_SERVER=1 SCHWAB_TOKENS_DB=/app/data/tokens.db

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY companion companion
COPY assets assets
RUN mkdir -p data && chown -R 99:100 /app

# Unraid's nobody:users; override with `user:` in compose for other hosts.
USER 99:100
EXPOSE 8770
CMD ["python", "-m", "companion"]
