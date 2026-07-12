#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SECOND_APP="${WECHAT_SECOND_APP_PATH:-$ROOT_DIR/dist/WeChatSecond.app}"
SECOND_EXE="$SECOND_APP/Contents/MacOS/WeChat"
PID=""
if [[ -f "$HOME/Library/Application Support/WeChatSecond/wechat-second.pid" ]]; then
  CANDIDATE=$(cat "$HOME/Library/Application Support/WeChatSecond/wechat-second.pid" 2>/dev/null || true)
  if [[ -n "$CANDIDATE" ]] && ps -p "$CANDIDATE" -o args= 2>/dev/null | grep -Fq "$SECOND_EXE"; then
    PID="$CANDIDATE"
  fi
fi
if [[ -z "$PID" ]]; then
  PID=$(pgrep -f "$(printf '%s' "$SECOND_EXE" | sed 's/[][$.*^|(){}+?\\]/\\&/g')" | head -1 || true)
fi
if [[ -z "$PID" ]]; then
  echo "找不到第二微信进程，请先运行：$ROOT_DIR/scripts/launch_second_wechat.sh" >&2
  exit 1
fi
ARGS=$(ps -p "$PID" -o args=)
if ! printf '%s' "$ARGS" | grep -Fq "$SECOND_EXE"; then
  echo "拒绝返回非第二微信 PID：$PID $ARGS" >&2
  exit 2
fi
printf '%s\n' "$PID"
