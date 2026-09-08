#!/usr/bin/env bash
# Install the Polymarket weather bot as a launchd agent on macOS.
#
#   bash deploy/install-macos.sh
#
# Idempotent: safe to re-run to upgrade an existing install.
#
# Unlike the Ubuntu installer this deliberately runs as *you*, not root, and
# from the checkout you already have rather than /opt. macOS has no equivalent
# of a system service account that owns a home directory, and a LaunchAgent
# runs as the logged-in user anyway -- adding sudo would buy nothing except a
# .env owned by the wrong account.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL=com.polyweather.bot
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [[ $EUID -eq 0 ]]; then
  echo "Do NOT run this with sudo -- a LaunchAgent runs as you." >&2
  exit 1
fi

echo "==> Checking Python"
PYBIN=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1 && \
     "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'; then
    PYBIN="$(command -v "$candidate")"
    break
  fi
done
if [[ -z "$PYBIN" ]]; then
  echo "This bot needs Python 3.10 or newer." >&2
  echo "The python3 Apple ships is 3.9. Install a newer one:  brew install python@3.12" >&2
  exit 1
fi
echo "    using $PYBIN ($("$PYBIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])'))"

echo "==> Building virtualenv"
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  "$PYBIN" -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

mkdir -p "$APP_DIR/data" "$APP_DIR/logs" "$HOME/Library/LaunchAgents"

if [[ ! -f "$APP_DIR/.env" ]]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo "    created $APP_DIR/.env from the example -- EDIT IT BEFORE STARTING"
fi
# The .env can hold a private key: owner-read only.
chmod 600 "$APP_DIR/.env"

echo "==> Writing $PLIST"
# Note there is no EnvironmentVariables block: the bot reads .env itself, and
# duplicating it here would let a stale copy silently outrank the file.
cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>

  <key>ProgramArguments</key>
  <array>
    <string>$APP_DIR/.venv/bin/python</string>
    <string>$APP_DIR/main.py</string>
  </array>

  <key>WorkingDirectory</key>
  <string>$APP_DIR</string>

  <key>RunAtLoad</key>
  <true/>

  <!-- Restart on crash. ThrottleInterval is launchd's crash-loop brake: it
       refuses to respawn more than once per interval, so a bot that cannot
       start backs off instead of thrashing against the exchange. -->
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>
  <key>ThrottleInterval</key>
  <integer>30</integer>

  <key>StandardOutPath</key>
  <string>$APP_DIR/logs/launchd.out.log</string>
  <key>StandardErrorPath</key>
  <string>$APP_DIR/logs/launchd.err.log</string>

  <key>ProcessType</key>
  <string>Background</string>
</dict>
</plist>
PLIST_EOF

plutil -lint "$PLIST" >/dev/null

echo "==> Loading the agent"
# bootout first so a re-run picks up an edited plist. It fails when nothing is
# loaded yet, which is the normal first-install case.
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST"

cat <<EOF

Done.

  1. Edit the config:      nano $APP_DIR/.env
     (at minimum TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

  2. Sanity-check a poll:  $APP_DIR/.venv/bin/python $APP_DIR/main.py --scan

  3. Apply .env edits:     launchctl kickstart -k gui/$UID/$LABEL
     Check it is running:  launchctl print gui/$UID/$LABEL | head -20
     Follow the logs:      tail -f $APP_DIR/logs/bot.log
     Stop it:              launchctl bootout gui/$UID/$LABEL

A LaunchAgent only runs while you are logged in, and stops when the Mac sleeps.
For unattended running keep the machine awake -- see the README.

The bot starts in PAPER mode and will not place a real order until you both set
TRADING_MODE=live and send /arm in Telegram.
EOF
