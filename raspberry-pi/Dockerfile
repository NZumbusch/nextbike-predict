# Läuft auf Raspberry Pi (arm64/armv7) und x86. Python + SQLite, keine weiteren Dienste.
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
COPY nextbike ./nextbike
COPY config.toml ./config.toml

# Läuft als unprivilegierter User "nb" (entrypoint.sh korrigiert vorher die Rechte von /data)
RUN useradd --uid 1000 --create-home nb && mkdir -p /data && chown nb /data
COPY entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
VOLUME ["/data"]
EXPOSE 8080
CMD ["python", "-m", "nextbike.web"]
