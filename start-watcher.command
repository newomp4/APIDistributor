#!/bin/bash
set -e

cd "$(dirname "$0")/watcher"

if [ ! -d ".venv" ]; then
  echo "First run: creating Python virtual environment..."
  python3 -m venv .venv
fi

source .venv/bin/activate

if [ ! -f ".venv/.deps_installed" ] || [ requirements.txt -nt .venv/.deps_installed ]; then
  echo "Installing dependencies..."
  pip install --quiet --upgrade pip
  pip install --quiet -r requirements.txt
  touch .venv/.deps_installed
fi

echo ""
echo "═══════════════════════════════════════════════════"
echo "  APIDistributor folder-watcher running."
echo "  Drop videos into channels/<name>/inbox/"
echo "  Press Ctrl+C in this window to stop."
echo "═══════════════════════════════════════════════════"
echo ""

exec python3 watcher.py
