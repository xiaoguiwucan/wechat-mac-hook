#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SECOND_HOME="$HOME/Library/Application Support/WeChatSecond"
"$ROOT_DIR/scripts/launch_wechat2_4_1_11_53.sh"
"$ROOT_DIR/scripts/start_onebot_wechat2.sh"
LOG="$SECOND_HOME/logs/onebot-wechat2.log"
for _ in {1..60}; do
  if grep -q '捕获到 StartTask' "$LOG" 2>/dev/null; then
    echo "StartTask captured; WeChat2 OneBot ready."
    exit 0
  fi
  sleep 1
done
echo "OneBot is attached, but StartTask has not been captured yet. Send/receive one normal message in WeChat2, then retry API." >&2
exit 0
