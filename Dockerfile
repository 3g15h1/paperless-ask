FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOST=0.0.0.0 PORT=8000 DATA_DIR=/data

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY paperless_ask ./paperless_ask
RUN pip install . && useradd --system --uid 10001 --no-create-home paperless-ask \
    && mkdir /data && chown paperless-ask /data
VOLUME /data

USER paperless-ask
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8000\")}/healthz', timeout=4)" || exit 1

CMD ["paperless-ask"]
