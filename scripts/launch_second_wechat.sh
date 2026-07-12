#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DST_APP="${WECHAT_SECOND_APP_PATH:-$ROOT_DIR/dist/WeChatSecond.app}"
HOOK="$ROOT_DIR/build/WeChatSecondHook.dylib"
SECOND_HOME="${WECHAT_SECOND_HOME:-$HOME/Library/Application Support/WeChatSecond}"
LOG_DIR="$SECOND_HOME/logs"
LOG_FILE="$LOG_DIR/launch.log"

if [[ ! -f "$HOOK" ]]; then
  "$ROOT_DIR/scripts/build.sh"
fi
if [[ ! -d "$DST_APP" ]]; then
  "$ROOT_DIR/scripts/install_second_app.sh"
fi

EXE_NAME=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$DST_APP/Contents/Info.plist")
EXE="$DST_APP/Contents/MacOS/$EXE_NAME"
if [[ ! -x "$EXE" ]]; then
  echo "找不到可执行文件：$EXE" >&2
  exit 1
fi
mkdir -p "$LOG_DIR"

echo "Launching second WeChat only..."
echo "App: $DST_APP"
echo "Hook: $HOOK"
echo "Data: $SECOND_HOME"
echo "Log: $LOG_FILE"

WECHAT_REAL_HOME="$HOME" \
WECHAT_SECOND_INSTANCE=1 \
WECHAT_SECOND_HOME="$SECOND_HOME" \
nohup "$EXE" --wechat-second-instance --allow_multi_open --multi_open >"$LOG_FILE" 2>&1 &
PID=$!
echo "$PID" > "$SECOND_HOME/wechat-second.pid"
echo "Started PID: $PID"
sleep 2
if ! kill -0 "$PID" 2>/dev/null; then
  echo "第二微信启动后已退出，日志如下：" >&2
  sed -n '1,160p' "$LOG_FILE" >&2 || true
  exit 1
fi
HOOK_OK=0
for _ in 1 2 3 4 5; do
  if grep -q '\[WeChatSecondHook\] enabled' "$LOG_FILE" 2>/dev/null; then
    HOOK_OK=1
    break
  fi
  sleep 1
done
if [[ "$HOOK_OK" == "1" ]]; then
  echo "Hook 已生效。"
else
  echo "进程已启动，但暂未在日志中看到 hook 标记；请查看：$LOG_FILE" >&2
fi
