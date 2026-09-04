#!/usr/bin/env bash
# Install the Polymarket weather bot on a fresh Ubuntu VPS (22.04 / 24.04).
#
#   sudo bash deploy/install-ubuntu.sh
#
# Idempotent: safe to re-run to upgrade an existing install.
set -euo pipefail

APP_USER=polyweather
APP_DIR=/opt/polyweather
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo." >&2
  exit 1
fi

echo "==> Installing system packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-dev build-essential git ca-certificates

PYVER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
echo "    python3 = $PYVER"
python3 - <<'PY'
import sys
if sys.version_info < (3, 10):
    sys.exit("This bot needs Python 3.10 or newer.")
PY

echo "==> Creating service user $APP_USER"
id -u "$APP_USER" &>/dev/null || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"

echo "==> Syncing code to $APP_DIR"
mkdir -p "$APP_DIR"
if [[ "$SRC_DIR" != "$APP_DIR" ]]; then
  # Never clobber a live .env or the sqlite database.
  tar -C "$SRC_DIR" \
      --exclude=.venv --exclude=.git --exclude=data --exclude=logs \
      --exclude=.env --exclude='__pycache__' --exclude='*.pyc' \
      -cf - . | tar -C "$APP_DIR" -xf -
fi
mkdir -p "$APP_DIR/data" "$APP_DIR/logs"

# data/ is excluded above so the live sqlite database survives an upgrade, but
# the leader file is configuration, not state -- without it copy_trader loads
# zero wallets and silently never trades. Copy it explicitly.
if [[ -f "$SRC_DIR/data/top_traders.json" && "$SRC_DIR" != "$APP_DIR" ]]; then
  cp "$SRC_DIR/data/top_traders.json" "$APP_DIR/data/top_traders.json"
  echo "    installed data/top_traders.json ($(python3 -c "import json,sys; print(len(json.load(open('$APP_DIR/data/top_traders.json'))['traders']))") leader wallets)"
elif [[ ! -f "$APP_DIR/data/top_traders.json" ]]; then
  echo "    WARNING: no data/top_traders.json -- copy trading will be idle"
fi

echo "==> Building virtualenv"
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

if [[ ! -f "$APP_DIR/.env" ]]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo "    created $APP_DIR/.env from the example -- EDIT IT BEFORE STARTING"
fi

echo "==> Setting ownership and permissions"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
# The .env holds a private key: owner-read only.
chmod 600 "$APP_DIR/.env"
chmod 750 "$APP_DIR"

echo "==> Installing systemd unit"
cp "$APP_DIR/deploy/polyweather.service" /etc/systemd/system/polyweather.service
systemctl daemon-reload
systemctl enable polyweather >/dev/null

cat <<EOF

Done.

  1. Edit the config:      sudo nano $APP_DIR/.env
     (at minimum TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

  2. Sanity-check a scan:  sudo -u $APP_USER $APP_DIR/.venv/bin/python $APP_DIR/main.py --scan

  3. Start the service:    sudo systemctl start polyweather
     Follow the logs:      sudo journalctl -u polyweather -f
     Restart after edits:  sudo systemctl restart polyweather

The bot starts in PAPER mode and will not place a real order until you both set
TRADING_MODE=live and send /arm in Telegram.
EOF
