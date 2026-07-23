#!/usr/bin/env bash
set -euo pipefail
PID_FILE="$HOME/Library/Application Support/WeChatAgent/web-admin.pid"
PID="$(cat "$PID_FILE" 2>/dev/null || true)"
if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
  CMD="$(ps -p "$PID" -o command= 2>/dev/null || true)"
  if [[ "$CMD" == *"web_admin/server.py"* ]]; then
    kill "$PID"
    echo "Stopped Web admin PID=$PID"
  else
    echo "PID file does not point to Web admin" >&2
    exit 1
  fi
fi
rm -f "$PID_FILE"
