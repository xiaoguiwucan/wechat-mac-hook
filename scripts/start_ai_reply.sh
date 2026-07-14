#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SECOND_HOME="$HOME/Library/Application Support/WeChatSecond"
PID_FILE="$SECOND_HOME/ai-reply.pid"
LOG_DIR="$SECOND_HOME/logs"
LOG_FILE="$LOG_DIR/ai-reply.log"
STDOUT_LOG="$LOG_DIR/ai-reply.stdout.log"
CONFIG="${AI_REPLY_CONFIG:-$ROOT_DIR/config/ai_reply_config.json}"
ENV_FILE="${AI_REPLY_ENV_FILE:-$ROOT_DIR/config/ai_reply.env}"
PYTHON_BIN="${WECHAT_SECOND_PYTHON:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  PYTHON_BIN=$(find "$HOME/.local/share/uv/python" -type f -path '*/bin/python3.*' ! -name '*-config' -perm -111 2>/dev/null | sort -V | tail -n 1 || true)
fi
if [[ -z "$PYTHON_BIN" ]]; then
  PYTHON_BIN=$(command -v python3)
fi
mkdir -p "$LOG_DIR"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi
PORT="${AI_REPLY_LISTEN_PORT:-36060}"

if [[ ! -f "$CONFIG" ]]; then
  echo "找不到 AI 配置: $CONFIG" >&2
  exit 1
fi

# 如果已运行，直接复用。
OLD=$(cat "$PID_FILE" 2>/dev/null || true)
if [[ -n "$OLD" ]] && kill -0 "$OLD" 2>/dev/null; then
  CMD=$(ps -p "$OLD" -o command= 2>/dev/null || true)
  if [[ "$CMD" == *"ai_reply_server.py"* ]]; then
    echo "AI reply server already running PID=$OLD"
    exit 0
  fi
fi

# 端口被占用时，只允许杀旧 ai_reply_server；其它程序直接报错。
PORT_PIDS=$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)
if [[ -n "$PORT_PIDS" ]]; then
  while read -r PPID_; do
    [[ -z "$PPID_" ]] && continue
    CMD=$(ps -p "$PPID_" -o command= 2>/dev/null || true)
    if [[ "$CMD" == *"ai_reply_server.py"* ]]; then
      kill "$PPID_" 2>/dev/null || true
    else
      echo "端口 $PORT 被非 AI 服务占用：PID=$PPID_ $CMD" >&2
      exit 2
    fi
  done <<< "$PORT_PIDS"
  sleep 1
fi

# 先检查配置。
"$PYTHON_BIN" "$ROOT_DIR/ai_reply/ai_reply_server.py" --config "$CONFIG" --check >/dev/null

# daemon 化启动，避免终端/Codex 回收后台进程。
/usr/bin/python3 - "$ROOT_DIR" "$CONFIG" "$PID_FILE" "$STDOUT_LOG" "$PYTHON_BIN" <<'PY'
import os
import subprocess
import sys
root, config, pid_file, log_file, python_bin = sys.argv[1:]
args = [python_bin, os.path.join(root, 'ai_reply', 'ai_reply_server.py'), '--config', config]
log = open(log_file, 'ab', buffering=0)
proc = subprocess.Popen(args, cwd=root, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
with open(pid_file, 'w') as f:
    f.write(str(proc.pid))
PY

PID=$(cat "$PID_FILE")
for _ in {1..40}; do
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "AI reply server exited; log:" >&2
    tail -n 120 "$LOG_FILE" >&2 || true
    exit 3
  fi
  if lsof -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done

echo "AI reply server PID=$PID listening on 127.0.0.1:$PORT"
curl -sS "http://127.0.0.1:$PORT/health" || true
echo
