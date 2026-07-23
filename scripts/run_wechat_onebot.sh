#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
AGENT_HOME="$HOME/Library/Application Support/WeChatAgent"
"$ROOT_DIR/scripts/launch_wechat.sh"
"$ROOT_DIR/scripts/start_onebot.sh"
LOG="$AGENT_HOME/logs/onebot-wechat.log"
for _ in {1..60}; do
  if grep -q '捕获到 StartTask' "$LOG" 2>/dev/null; then
    echo "StartTask captured; WeChat OneBot ready."
    exit 0
  fi
  sleep 1
done
echo "OneBot is attached, but StartTask has not been captured yet. Send/receive one normal message in WeChat, then retry API." >&2
exit 0
