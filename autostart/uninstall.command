#!/bin/bash
# Removes the launchd agent so the watcher no longer auto-starts on login.
set -e

PLIST_PATH="$HOME/Library/LaunchAgents/com.apidistributor.watcher.plist"

if [ -f "$PLIST_PATH" ]; then
  launchctl unload "$PLIST_PATH" 2>/dev/null || true
  rm -f "$PLIST_PATH"
  echo "Auto-start removed. Launch the watcher manually with start-watcher.command."
else
  echo "Auto-start was not installed (no plist at $PLIST_PATH)."
fi
