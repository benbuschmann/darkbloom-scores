FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BIND_HOST=0.0.0.0 \
    PORT=8788 \
    SCORES_DB=/data/scores.sqlite3 \
    POLL_INTERVAL_SECONDS=60

WORKDIR /app
COPY fetch_manager.py LICENSE THIRD_PARTY.md ./
RUN python fetch_manager.py \
    && mkdir /data \
    && chown 10001:10001 /data
COPY server.py index.html ./

USER 10001:10001
EXPOSE 8788
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8788') + '/healthz', timeout=3).read()"

ENTRYPOINT ["python", "server.py"]
CMD ["serve"]
