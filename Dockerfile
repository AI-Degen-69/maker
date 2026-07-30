# Maker sim: a single Python stage. No Node build -- unlike the taker, this
# dashboard is self-contained HTML, so the image is smaller and builds faster.
#
# DEPLOY IN A NON-US REGION. Binance returns HTTP 451 to US IPs; the preflight
# in deploy/run_service.py fails loudly rather than letting the healthcheck go
# green on a bot that can never see the market.
FROM python:3.12-slim
WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY strategy/ /app/strategy/
COPY server/   /app/server/
COPY deploy/   /app/deploy/

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
ENV PORT=8788

# Persistent storage is REQUIRED: set MAKER_DB=/data/maker.db and mount a
# volume at /data, or every redeploy silently discards the run.

EXPOSE 8788
CMD ["python", "deploy/run_service.py"]
