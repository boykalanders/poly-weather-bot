# Build the virtualenv separately so the compiler toolchain that the CLOB client's
# eth-* dependencies occasionally need does not ship in the final image.
FROM python:3.12-slim AS build

RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN python -m venv /venv \
 && /venv/bin/pip install --no-cache-dir --upgrade pip \
 && /venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt


FROM python:3.12-slim

# ca-certificates for TLS to Polymarket, Open-Meteo and Telegram; tini so the
# container forwards SIGINT and the bot gets its clean shutdown.
RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends ca-certificates tini \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --create-home --home-dir /home/polyweather polyweather

COPY --from=build /venv /venv
WORKDIR /app
COPY . /app

# The leader list is configuration, not state, but /app/data is where the Fly
# volume mounts -- and a mounted volume hides whatever the image had at that
# path. Keep a copy outside the mount and point COPY_WALLETS_FILE at it (see
# fly.toml), otherwise the fallback silently disappears the moment a volume is
# attached and copy trading follows nobody.
RUN mkdir -p /app/seed \
 && cp /app/data/top_traders.json /app/seed/top_traders.json

# data/ is the volume mount point and holds the sqlite database; logs/ is
# ephemeral scratch. Both must be writable by the unprivileged user.
RUN mkdir -p /app/data /app/logs && chown -R polyweather:polyweather /app

USER polyweather
ENV PATH=/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# No EXPOSE and no port: this is a worker. Telegram is long-polled outbound
# (tg/app.py run_polling), so nothing ever connects to us.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "main.py"]
