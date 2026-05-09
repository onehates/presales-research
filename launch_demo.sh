#!/usr/bin/env bash
# Launch demo: starts chat server and opens the most recent brief.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BRIEFS_DIR="$SCRIPT_DIR/briefs"

# Find most recent HTML brief
BRIEF=$(ls -t "$BRIEFS_DIR"/*.html 2>/dev/null | head -1)
if [ -z "$BRIEF" ]; then
  echo "No HTML briefs found in $BRIEFS_DIR"
  exit 1
fi

echo "Starting chat server on http://localhost:8000 ..."
python3 "$SCRIPT_DIR/chat_server.py" &
CHAT_PID=$!
trap "kill $CHAT_PID 2>/dev/null" EXIT

sleep 1

echo "Opening brief: $(basename "$BRIEF")"
if command -v xdg-open &>/dev/null; then
  xdg-open "$BRIEF"
elif command -v open &>/dev/null; then
  open "$BRIEF"
else
  echo "Open in browser: file://$BRIEF"
fi

echo "Chat server running (PID $CHAT_PID). Press Ctrl+C to stop."
wait $CHAT_PID
