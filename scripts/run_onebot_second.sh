#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ONEBOT_BIN="${ONEBOT_BIN:-$ROOT_DIR/bin/onebot}"
SECOND_HOME="${WECHAT_SECOND_HOME:-$HOME/Library/Application Support/WeChatSecond}"
CONF="${WECHAT_HOOK_CONF:-$ROOT_DIR/config/4_1_11_54_mac.experimental.json}"
RECEIVE_HOST="${ONEBOT_RECEIVE_HOST:-127.0.0.1:58080}"
SEND_URL="${ONEBOT_SEND_URL:-http://127.0.0.1:36060/onebot}"
CONN_TYPE="${ONEBOT_CONN_TYPE:-http}"
LOG_LEVEL="${ONEBOT_LOG_LEVEL:-info}"

if [[ ! -x "$ONEBOT_BIN" ]]; then
  echo "找不到 onebot 可执行文件：$ONEBOT_BIN" >&2
  echo "可执行：$ROOT_DIR/scripts/build_onebot.sh；本机当前未检测到 Go 时，请放入预编译 onebot 到 $ONEBOT_BIN" >&2
  exit 1
fi
if [[ ! -f "$CONF" ]]; then
  echo "找不到 hook 地址配置：$CONF" >&2
  exit 1
fi
PID=$("$ROOT_DIR/scripts/find_second_wechat_pid.sh")
ARGS=$(ps -p "$PID" -o args=)
if ! printf '%s' "$ARGS" | grep -Fq "$ROOT_DIR/dist/WeChatSecond.app/Contents/MacOS/WeChat"; then
  echo "安全检查失败：目标 PID 不是第二微信，拒绝 attach：$PID $ARGS" >&2
  exit 2
fi
IMAGE_PATH=$(find "$SECOND_HOME/Sandbox/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files" -type d -path '*/Img' 2>/dev/null | head -1 || true)
if [[ -z "$IMAGE_PATH" ]]; then
  IMAGE_PATH="$SECOND_HOME/Sandbox/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files"
  mkdir -p "$IMAGE_PATH"
fi
mkdir -p "$ROOT_DIR/logs"
echo "只 attach 第二微信 PID：$PID"
echo "HookConf：$CONF"
echo "ImagePath：$IMAGE_PATH"
echo "HTTP：$RECEIVE_HOST"
(
  cd "$ROOT_DIR/vendor/wechat_chatter/onebot"
  "$ONEBOT_BIN" \
    -wechat_pid="$PID" \
    -wechat_conf="$CONF" \
    -image_path="$IMAGE_PATH" \
    -receive_host="$RECEIVE_HOST" \
    -send_url="$SEND_URL" \
    -conn_type="$CONN_TYPE" \
    -log_level="$LOG_LEVEL"
) 2>&1 | tee "$ROOT_DIR/logs/onebot-second.log"
