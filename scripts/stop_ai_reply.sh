#!/usr/bin/env bash
set -euo pipefail
AGENT_HOME="$HOME/Library/Application Support/WeChatAgent"
PID_FILE="$AGENT_HOME/ai-reply.pid"
PID=$(cat "$PID_FILE" 2>/dev/null || true)
if [[ -z "$PID" ]]; then
  echo "AI reply server not running (no pid file)"
  exit 0
fi
if kill -0 "$PID" 2>/dev/null; then
  CMD=$(ps -p "$PID" -o command= 2>/dev/null || true)
  if [[ "$CMD" == *"ai_reply_server.py"* ]]; then
    kill "$PID" 2>/dev/null || true
    echo "Stopped AI reply server PID=$PID"
  else
    echo "PID file points to non-AI process: PID=$PID $CMD" >&2
    exit 1
  fi
else
  echo "AI reply server already stopped"
fi
rm -f "$PID_FILE"
