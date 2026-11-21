FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data ZEBA_ENV_FILE=/data/.env ZEBA_NO_VENV=1 HOME=/home/zeba

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tini ca-certificates fonts-dejavu-core fonts-beng \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt && (pip install cryptg || true) && (pip install tgcrypto-pyrofork || true) && (pip install g4f || true)

COPY . .
RUN useradd -m -u 1000 zeba && mkdir -p /data && chown -R zeba:zeba /data /app && chmod +x /app/deploy/entrypoint.sh
VOLUME ["/data"]
EXPOSE 10000

# Starts as root only to fix volume permissions, then runs as user zeba (see deploy/entrypoint.sh)
ENTRYPOINT ["tini", "--", "/app/deploy/entrypoint.sh"]
CMD ["python", "run.py"]
