#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SECOND_HOME="$HOME/Library/Application Support/WeChatSecond"
PID_FILE="$SECOND_HOME/web-admin.pid"
LOG_FILE="$SECOND_HOME/logs/web-admin.log"
PORT="${WECHAT_SECOND_ADMIN_PORT:-8765}"
mkdir -p "$SECOND_HOME/logs"

OLD="$(cat "$PID_FILE" 2>/dev/null || true)"
if [[ -n "$OLD" ]] && kill -0 "$OLD" 2>/dev/null; then
  CMD="$(ps -p "$OLD" -o command= 2>/dev/null || true)"
  if [[ "$CMD" == *"web_admin/server.py"* ]]; then
    echo "Web admin already running PID=$OLD http://127.0.0.1:$PORT"
    exit 0
  fi
fi

/usr/bin/python3 - "$ROOT_DIR" "$PID_FILE" "$LOG_FILE" "$PORT" <<'PY'
import os, subprocess, sys
root, pid_file, log_file, port = sys.argv[1:]
log = open(log_file, 'ab', buffering=0)
proc = subprocess.Popen(
    [sys.executable, os.path.join(root, 'web_admin', 'server.py'), '--port', port],
    cwd=root, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
    start_new_session=True, close_fds=True,
)
with open(pid_file, 'w') as f:
    f.write(str(proc.pid))
PY

PID="$(cat "$PID_FILE")"
for _ in {1..30}; do
  if curl -fsS "http://127.0.0.1:$PORT/api/status" >/dev/null 2>&1; then
    echo "Web admin PID=$PID http://127.0.0.1:$PORT"
    exit 0
  fi
  sleep 0.2
done
echo "Web admin failed to start" >&2
tail -n 80 "$LOG_FILE" >&2 || true
exit 1
