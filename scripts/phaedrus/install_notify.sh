#!/usr/bin/env bash
#
# Install (or re-install) the Phaedrus daily nudge as a macOS LaunchAgent.
#
# Writes ~/Library/LaunchAgents/com.phaedrus.notify.plist that runs
# daily_notify.py once a day (default 09:00) via the project venv, then loads
# it with launchctl. Idempotent: unloads any existing agent before loading, so
# you can re-run it after changing the time or the script.
#
# Usage:
#   bash scripts/phaedrus/install_notify.sh            # schedule for 09:00
#   bash scripts/phaedrus/install_notify.sh 21 30      # schedule for 21:30
#   bash scripts/phaedrus/install_notify.sh --uninstall
#
set -euo pipefail

LABEL="com.phaedrus.notify"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"

# --- Resolve paths (this script lives in scripts/phaedrus/) ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
NOTIFY_PY="$SCRIPT_DIR/daily_notify.py"

# Prefer the project venv python; fall back to whatever python3 is on PATH.
VENV_PY="$REPO_ROOT/.venv/bin/python"
if [ -x "$VENV_PY" ]; then
  PYTHON="$VENV_PY"
else
  PYTHON="$(command -v python3 || command -v python)"
fi

# --- Uninstall path ---
if [ "${1:-}" = "--uninstall" ]; then
  if [ -f "$PLIST" ]; then
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Uninstalled $LABEL (removed $PLIST)."
  else
    echo "Nothing to uninstall — $PLIST not present."
  fi
  exit 0
fi

# --- Fire time (default 09:00) ---
HOUR="${1:-9}"
MINUTE="${2:-0}"

if ! [ -f "$NOTIFY_PY" ]; then
  echo "error: cannot find $NOTIFY_PY" >&2
  exit 1
fi

mkdir -p "$(dirname "$PLIST")"

# --- Write the plist ---
cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON}</string>
        <string>${NOTIFY_PY}</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>${HOUR}</integer>
        <key>Minute</key>
        <integer>${MINUTE}</integer>
    </dict>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>${HOME}/.phaedrus/notify.log</string>
    <key>StandardErrorPath</key>
    <string>${HOME}/.phaedrus/notify.log</string>
</dict>
</plist>
PLIST_EOF

# --- Load it (unload first for idempotency) ---
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

printf 'Installed %s\n' "$LABEL"
printf '  python : %s\n' "$PYTHON"
printf '  script : %s\n' "$NOTIFY_PY"
printf '  fires  : daily at %02d:%02d\n' "$HOUR" "$MINUTE"
printf '  plist  : %s\n' "$PLIST"
printf '  log    : %s/.phaedrus/notify.log\n' "$HOME"
echo
echo "To change the time:   bash scripts/phaedrus/install_notify.sh <hour> <minute>"
echo "To uninstall:         bash scripts/phaedrus/install_notify.sh --uninstall"
echo "To test now:          launchctl start $LABEL"
