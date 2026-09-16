# Production image for the Lumen relay. Railway supplies the public $PORT;
# client TLS/WSS terminates at Railway's public HTTPS endpoint.
FROM golang:1.26-bookworm AS psiphon-core-builder
WORKDIR /src
COPY third_party/psiphon-tunnel-core/ ./
RUN CGO_ENABLED=1 GOOS=linux GOARCH=amd64 \
    go build -mod=vendor -trimpath -ldflags="-s -w" \
    -o /out/psiphon-tunnel-core ./ConsoleClient

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data \
    PORT=8080 \
    PSIPHON_CONSOLE_CLIENT_PATH=/usr/local/bin/psiphon-tunnel-core

WORKDIR /app

# tini forwards Railway's termination signal to Python. gosu lets the
# entrypoint make the mounted /data volume writable before running the relay
# without root privileges.
RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates gosu tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 lumen \
    && useradd --uid 10001 --gid lumen --create-home --home-dir /app lumen \
    && install -d --owner=lumen --group=lumen --mode=0700 /data

COPY --from=psiphon-core-builder /out/psiphon-tunnel-core /usr/local/bin/psiphon-tunnel-core
RUN chmod 0755 /usr/local/bin/psiphon-tunnel-core

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY --chown=lumen:lumen . /app
RUN chmod 0755 /app/docker-entrypoint.sh

EXPOSE 8080

ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker-entrypoint.sh"]
CMD ["python", "main.py"]