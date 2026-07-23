#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
AGENT_HOME="$HOME/Library/Application Support/WeChatAgent"
PID_FILE="$AGENT_HOME/onebot-wechat.pid"
ONEBOT_BIN="$ROOT_DIR/tools/onebot/onebot/onebot"
PORT="${ONEBOT_RECEIVE_HOST:-127.0.0.1:58080}"
PORT="${PORT##*:}"
STOPPED=0
"$ROOT_DIR/scripts/stop_voice_transcript_sidecar.sh" || true
PID="$(cat "$PID_FILE" 2>/dev/null || true)"
if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
  CMD="$(ps -p "$PID" -o command= 2>/dev/null || true)"
  if [[ "$CMD" == *"$ONEBOT_BIN"* || "$CMD" == *"tools/onebot/onebot/onebot"* ]]; then
    kill "$PID" 2>/dev/null || true
    echo "Stopped OneBot PID=$PID"
    STOPPED=1
  else
    echo "PID file points to non-OneBot process: PID=$PID $CMD" >&2
    exit 1
  fi
fi
# 清理同端口上的旧 onebot，仅限 onebot 自身；不碰微信。
PORT_PIDS="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
if [[ -n "$PORT_PIDS" ]]; then
  while read -r PPID_; do
    [[ -z "$PPID_" ]] && continue
    CMD="$(ps -p "$PPID_" -o command= 2>/dev/null || true)"
    if [[ "$CMD" == *"$ONEBOT_BIN"* || "$CMD" == *"tools/onebot/onebot/onebot"* ]]; then
      kill "$PPID_" 2>/dev/null || true
      echo "Stopped OneBot port process PID=$PPID_"
      STOPPED=1
    fi
  done <<< "$PORT_PIDS"
fi
rm -f "$PID_FILE"
if [[ "$STOPPED" == "0" ]]; then
  echo "OneBot not running"
fi
