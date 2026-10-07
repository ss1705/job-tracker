#!/bin/bash
# Run the tracker in the background at login (macOS launch agent). Auto-sync then runs on its own.
#   scripts/install_launchd.sh            install + start
#   scripts/install_launchd.sh uninstall  stop + remove
set -euo pipefail
LABEL="com.jobtracker.server"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if [ "${1:-}" = "uninstall" ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Removed $LABEL"
  exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/data"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$ROOT/.venv/bin/uvicorn</string><string>jobtracker.api:app</string>
    <string>--host</string><string>127.0.0.1</string><string>--port</string><string>8000</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$ROOT/data/server.log</string>
  <key>StandardErrorPath</key><string>$ROOT/data/server.log</string>
</dict></plist>
PLIST

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed. Tracker runs at http://127.0.0.1:8000 (logs: $ROOT/data/server.log)"
echo "Stop any manually started uvicorn first, or port 8000 will be taken."
