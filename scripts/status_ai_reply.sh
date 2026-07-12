#!/usr/bin/env bash
set -euo pipefail
SECOND_HOME="$HOME/Library/Application Support/WeChatSecond"
PID_FILE="$SECOND_HOME/ai-reply.pid"
LOG_FILE="$SECOND_HOME/logs/ai-reply.log"
PID=$(cat "$PID_FILE" 2>/dev/null || true)
echo "AI reply PID=${PID:-not running}"
if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
  ps -p "$PID" -o pid,ppid,pgid,etime,comm,args
else
  echo "AI reply not running"
fi
echo "Port 36060:"
lsof -nP -iTCP:36060 -sTCP:LISTEN || true
echo "Health:"
curl -sS http://127.0.0.1:36060/health 2>/dev/null || true
echo
echo "Recent log:"
tail -n 80 "$LOG_FILE" 2>/dev/null || true
