#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP="${WECHAT2_APP:-$HOME/Applications/WeChat2.app}"
EXE="$APP/Contents/MacOS/WeChat"
SECOND_HOME="$HOME/Library/Application Support/WeChatSecond"
LOG_DIR="$SECOND_HOME/logs"
LOG_FILE="$LOG_DIR/wechat2-launch.log"
PID_FILE="$SECOND_HOME/wechat2.pid"
EXPECTED_BUNDLE="com.tencent.xinWeChat.instance2"
EXPECTED_BUILD="269109"
mkdir -p "$LOG_DIR"

if [[ ! -x "$EXE" ]]; then
  echo "找不到第二微信: $EXE" >&2
  exit 1
fi
BUNDLE_ID=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$APP/Contents/Info.plist" 2>/dev/null || true)
BUILD=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$APP/Contents/Info.plist" 2>/dev/null || true)
if [[ "$BUNDLE_ID" != "$EXPECTED_BUNDLE" ]]; then
  echo "拒绝启动：$APP 的 Bundle ID 是 $BUNDLE_ID，不是 $EXPECTED_BUNDLE" >&2
  exit 2
fi
if [[ "$BUILD" != "$EXPECTED_BUILD" ]]; then
  echo "拒绝启动：$APP build=$BUILD，不是 wechat_chatter 配套的 $EXPECTED_BUILD / 4.1.11.53" >&2
  exit 3
fi

# 默认绝不杀已登录的第二微信；只有显式 WECHAT2_RESTART=1 才重启第二微信。
EXISTING=$("$ROOT_DIR/scripts/find_wechat2_pid.sh" | head -1 || true)
if [[ -n "$EXISTING" && "${WECHAT2_RESTART:-0}" != "1" ]]; then
  echo "$EXISTING" > "$PID_FILE"
  echo "WeChat2 already running PID=$EXISTING (不重启，不影响登录态)"
  /bin/ps -p "$EXISTING" -o pid,etime,comm,args
  exit 0
fi

if [[ -n "$EXISTING" && "${WECHAT2_RESTART:-0}" == "1" ]]; then
  echo "WECHAT2_RESTART=1，仅结束第二微信 PID=$EXISTING"
  kill "$EXISTING" 2>/dev/null || true
  for _ in {1..20}; do
    if ! kill -0 "$EXISTING" 2>/dev/null; then break; fi
    sleep 0.5
  done
  if kill -0 "$EXISTING" 2>/dev/null; then
    ACTUAL=$(/usr/bin/python3 - "$EXISTING" <<'PY'
import ctypes, os, sys
pid = int(sys.argv[1])
buf = ctypes.create_string_buffer(4096)
ret = ctypes.CDLL('/usr/lib/libproc.dylib').proc_pidpath(pid, buf, 4096)
print(os.path.realpath(buf.value.decode('utf-8', 'ignore')) if ret > 0 else '')
PY
)
    if [[ "$ACTUAL" != "$EXE" ]]; then
      echo "拒绝强制结束：PID=${EXISTING} path=${ACTUAL} 不是第二微信" >&2
      exit 4
    fi
    echo "第二微信未响应 TERM，强制结束已验证 PID=$EXISTING"
    kill -KILL "$EXISTING" 2>/dev/null || true
    for _ in {1..20}; do
      if ! kill -0 "$EXISTING" 2>/dev/null; then break; fi
      sleep 0.25
    done
  fi
  if kill -0 "$EXISTING" 2>/dev/null; then
    echo "第二微信旧进程无法结束，拒绝继续启动" >&2
    exit 5
  fi
fi

: > "$LOG_FILE"
xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true

# 优先用 LaunchServices 启动 app bundle，保持 com.tencent.xinWeChat.instance2 容器语义。
/usr/bin/open -n "$APP" --args --allow_multi_open --multi_open >>"$LOG_FILE" 2>&1 || \
  nohup "$EXE" --allow_multi_open --multi_open >>"$LOG_FILE" 2>&1 &

PID=""
for _ in {1..60}; do
  PID=$("$ROOT_DIR/scripts/find_wechat2_pid.sh" | head -1 || true)
  if [[ -n "$PID" && "$PID" != "${EXISTING:-}" ]]; then break; fi
  PID=""
  sleep 0.5
done
if [[ -z "$PID" ]]; then
  echo "WeChat2 启动失败；日志：$LOG_FILE" >&2
  sed -n '1,200p' "$LOG_FILE" >&2 || true
  exit 4
fi

echo "$PID" > "$PID_FILE"
echo "Started WeChat2 PID=$PID"
/bin/ps -p "$PID" -o pid,etime,comm,args
