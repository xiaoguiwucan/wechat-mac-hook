#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SECOND_HOME="$HOME/Library/Application Support/WeChatSecond"
WE2=$("$ROOT_DIR/scripts/find_wechat2_pid.sh" | head -1 || true)
OPID=$(cat "$SECOND_HOME/onebot-wechat2.pid" 2>/dev/null || true)
echo "WeChat2 PID=${WE2:-not running}"
if [[ -n "$WE2" ]]; then ps -p "$WE2" -o pid,ppid,pgid,etime,comm,args; fi
echo "OneBot PID=${OPID:-not running}"
if [[ -n "$OPID" ]] && kill -0 "$OPID" 2>/dev/null; then ps -p "$OPID" -o pid,ppid,pgid,etime,comm,args; else echo "OneBot not running"; fi
echo "Port 58080:"
lsof -nP -iTCP:58080 -sTCP:LISTEN || true
echo "Recent OneBot log:"
tail -n 40 "$SECOND_HOME/logs/onebot-wechat2.log" 2>/dev/null | sed -E 's/\x1b\[[0-9;]*m//g' || true
