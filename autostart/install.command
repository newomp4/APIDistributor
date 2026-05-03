#!/bin/bash
# Installs a launchd agent so the watcher auto-starts on login.
# Double-click this file in Finder, or run `bash install.command` in Terminal.
set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LAUNCHER="$PROJECT_DIR/start-watcher.command"
PLIST_PATH="$HOME/Library/LaunchAgents/com.apidistributor.watcher.plist"
LOG_OUT="$HOME/Library/Logs/apidistributor.out.log"
LOG_ERR="$HOME/Library/Logs/apidistributor.err.log"

if [ ! -x "$LAUNCHER" ]; then
  echo "ERROR: $LAUNCHER not found or not executable."
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"

cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.apidistributor.watcher</string>
  <key>ProgramArguments</key>
  <array>
    <string>$LAUNCHER</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$PROJECT_DIR</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>
  <key>StandardOutPath</key>
  <string>$LOG_OUT</string>
  <key>StandardErrorPath</key>
  <string>$LOG_ERR</string>
</dict>
</plist>
EOF

# Unload first if already loaded (safe; ignores errors)
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"

echo ""
echo "═══════════════════════════════════════════════════"
echo "  Auto-start installed."
echo ""
echo "  The watcher will now launch automatically every"
echo "  time you log in. UI: http://localhost:5050"
echo ""
echo "  Logs:   $LOG_OUT"
echo "          $LOG_ERR"
echo ""
echo "  To disable: double-click autostart/uninstall.command"
echo "═══════════════════════════════════════════════════"
