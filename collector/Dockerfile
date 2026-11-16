# sharedmob – Sammler + Statusseite. Raspberry Pi 4/5 mit 64-bit-OS (arm64) oder x86.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    CONFIG_PATH=/app/config.toml \
    TZ=Europe/Zurich

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY sharedmob ./sharedmob
COPY config.toml ./config.toml

RUN useradd --uid 1000 --create-home sm && mkdir -p /data && chown sm /data
COPY entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
VOLUME ["/data"]
EXPOSE 8080
CMD ["python", "-m", "sharedmob.web"]
