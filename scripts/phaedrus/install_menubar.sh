#!/usr/bin/env bash
#
# Install the Phaedrus menu-bar app as a macOS LaunchAgent so it starts at login.
#
# Idempotent: re-running overwrites the plist and reloads it cleanly
# (unload-before-load). Safe to run any number of times.
#
# Usage:
#   bash scripts/phaedrus/install_menubar.sh           # install / reinstall
#   bash scripts/phaedrus/install_menubar.sh --uninstall
#
set -euo pipefail

LABEL="com.phaedrus.menubar"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"

# Resolve the repo root from this script's own location (scripts/phaedrus/..).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

VENV_PYTHON="${REPO_ROOT}/.venv/bin/python"
APP="${SCRIPT_DIR}/menubar_app.py"
LOG_DIR="${HOME}/.phaedrus"
AGENT_LOG="${LOG_DIR}/menubar.out.log"
AGENT_ERR="${LOG_DIR}/menubar.err.log"

unload_if_loaded() {
  # bootout (modern) first, fall back to legacy unload; ignore "not loaded".
  launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
  launchctl unload "${PLIST}" 2>/dev/null || true
}

if [[ "${1:-}" == "--uninstall" ]]; then
  unload_if_loaded
  rm -f "${PLIST}"
  echo "Phaedrus menu-bar LaunchAgent removed (${PLIST})."
  exit 0
fi

# --- Pre-flight checks ---
if [[ ! -x "${VENV_PYTHON}" ]]; then
  echo "ERROR: venv python not found or not executable at:" >&2
  echo "  ${VENV_PYTHON}" >&2
  echo "Create the venv first, then re-run." >&2
  exit 1
fi
if [[ ! -f "${APP}" ]]; then
  echo "ERROR: menubar_app.py not found at ${APP}" >&2
  exit 1
fi

mkdir -p "${HOME}/Library/LaunchAgents" "${LOG_DIR}"

# --- Write the plist (overwrite each run) ---
cat > "${PLIST}" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${VENV_PYTHON}</string>
        <string>${APP}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${REPO_ROOT}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>ProcessType</key>
    <string>Interactive</string>
    <key>StandardOutPath</key>
    <string>${AGENT_LOG}</string>
    <key>StandardErrorPath</key>
    <string>${AGENT_ERR}</string>
</dict>
</plist>
PLIST_EOF

echo "Wrote ${PLIST}"

# --- Reload cleanly (unload-before-load) ---
unload_if_loaded

# Prefer modern bootstrap; fall back to legacy load -w.
if launchctl bootstrap "gui/$(id -u)" "${PLIST}" 2>/dev/null; then
  launchctl enable "gui/$(id -u)/${LABEL}" 2>/dev/null || true
  echo "Loaded via launchctl bootstrap."
else
  launchctl load -w "${PLIST}"
  echo "Loaded via launchctl load -w."
fi

echo
echo "Done. The Phaedrus menu-bar app will now launch at login."
echo "It also started just now — look for '${LABEL}'-driven '◆' in the menu bar."
echo "To remove: bash scripts/phaedrus/install_menubar.sh --uninstall"
