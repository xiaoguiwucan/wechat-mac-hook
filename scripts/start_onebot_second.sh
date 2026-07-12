#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SECOND_HOME="${WECHAT_SECOND_HOME:-$HOME/Library/Application Support/WeChatSecond}"
ONEBOT_DIR="$ROOT_DIR/tools/onebot/onebot"
ONEBOT_BIN="$ONEBOT_DIR/onebot"
CONF="$ROOT_DIR/tools/onebot/wechat_version/4_1_11_53_mac.json"
LOG_DIR="$SECOND_HOME/logs"
LOG_FILE="$LOG_DIR/onebot.log"
RECEIVE_HOST="${ONEBOT_RECEIVE_HOST:-127.0.0.1:58080}"
SEND_URL="${ONEBOT_SEND_URL:-http://127.0.0.1:36060/onebot}"
IMAGE_PATH="${ONEBOT_IMAGE_PATH:-$SECOND_HOME/Sandbox/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files}"

if [[ ! -x "$ONEBOT_BIN" ]]; then
  echo "找不到 onebot：$ONEBOT_BIN" >&2
  exit 1
fi
mkdir -p "$LOG_DIR" "$IMAGE_PATH"
xattr -dr com.apple.quarantine "$ONEBOT_BIN" 2>/dev/null || true
codesign --force --sign - --timestamp=none "$ONEBOT_BIN" >/dev/null 2>&1 || true

PID="$(cat "$SECOND_HOME/wechat-second.pid" 2>/dev/null || true)"
if [[ -z "$PID" ]] || ! kill -0 "$PID" 2>/dev/null; then
  "$ROOT_DIR/scripts/launch_second_wechat.sh"
  sleep 3
  PID="$(cat "$SECOND_HOME/wechat-second.pid")"
fi

echo "Starting OneBot for second WeChat PID=$PID"
echo "HTTP: $RECEIVE_HOST"
echo "Log: $LOG_FILE"
(
  cd "$ONEBOT_DIR"
  nohup "$ONEBOT_BIN" \
    -type=local \
    -wechat_pid="$PID" \
    -wechat_conf="$CONF" \
    -receive_host="$RECEIVE_HOST" \
    -send_url="$SEND_URL" \
    -image_path="$IMAGE_PATH" \
    -conn_type=http \
    -log_level=info >"$LOG_FILE" 2>&1 &
  echo $! > "$SECOND_HOME/onebot.pid"
)
sleep 5
OPID="$(cat "$SECOND_HOME/onebot.pid")"
if ! kill -0 "$OPID" 2>/dev/null; then
  echo "OneBot 启动失败，日志：" >&2
  sed -n '1,200p' "$LOG_FILE" >&2 || true
  exit 1
fi
sed -n '1,120p' "$LOG_FILE" || true
echo "OneBot PID: $OPID"
