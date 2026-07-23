#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP="${WECHAT_APP:-/Applications/WeChat.app}"
EXE="$APP/Contents/MacOS/WeChat"
AGENT_HOME="$HOME/Library/Application Support/WeChatAgent"
LOG_DIR="$AGENT_HOME/logs"
LOG_FILE="$LOG_DIR/wechat-launch.log"
PID_FILE="$AGENT_HOME/wechat.pid"
EXPECTED_BUNDLE="com.tencent.xinWeChat"
EXPECTED_VERSION="4.1.11.53"
EXPECTED_BUILD="269109"
mkdir -p "$LOG_DIR"
"$ROOT_DIR/scripts/verify_wechat_target.sh" --quiet

# 当前 Hook 只适配固定的 4.1.11.53 / 269109。禁止 Sparkle 在后台替换
# 已注入并重新签名的应用，否则运行中的代码页会被替换并触发
# CODESIGNING / Invalid Page 闪退。
/usr/bin/defaults write com.tencent.xinWeChat SUEnableAutomaticChecks -bool false
/usr/bin/defaults write com.tencent.xinWeChat SUAutomaticallyUpdate -bool false

if [[ ! -x "$EXE" ]]; then
  echo "找不到当前微信: $EXE" >&2
  exit 1
fi
BUNDLE_ID=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$APP/Contents/Info.plist" 2>/dev/null || true)
BUILD=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$APP/Contents/Info.plist" 2>/dev/null || true)
VERSION=$(/usr/libexec/PlistBuddy -c 'Print :WeChatBundleVersion' "$APP/Contents/Info.plist" 2>/dev/null || true)
if [[ "$BUNDLE_ID" != "$EXPECTED_BUNDLE" ]]; then
  echo "拒绝启动：$APP 的 Bundle ID 是 $BUNDLE_ID，不是 $EXPECTED_BUNDLE" >&2
  exit 2
fi
if [[ "$BUILD" != "$EXPECTED_BUILD" || "$VERSION" != "$EXPECTED_VERSION" ]]; then
  echo "拒绝启动：$APP version=$VERSION build=$BUILD，当前仅适配 $EXPECTED_VERSION / $EXPECTED_BUILD" >&2
  exit 3
fi

# 只允许官方安装路径的唯一主进程。发现其他 WeChat App 进程时直接停止。
OTHER=$(/usr/bin/python3 - "$EXE" <<'PY'
import ctypes, os, subprocess, sys
expected = os.path.realpath(sys.argv[1])
libproc = ctypes.CDLL('/usr/lib/libproc.dylib')
rows = []
for raw in subprocess.run(['/usr/bin/pgrep', '-x', 'WeChat'], capture_output=True, text=True).stdout.split():
    try:
        pid = int(raw)
    except ValueError:
        continue
    buf = ctypes.create_string_buffer(4096)
    if libproc.proc_pidpath(pid, buf, 4096) <= 0:
        continue
    path = os.path.realpath(buf.value.decode('utf-8', 'ignore'))
    if path and path != expected:
        rows.append(f'{pid}:{path}')
print('\n'.join(rows))
PY
)
if [[ -n "$OTHER" ]]; then
  echo "检测到非唯一安装路径的 WeChat 进程，拒绝继续：" >&2
  echo "$OTHER" >&2
  exit 4
fi

# 默认绝不杀已登录的当前微信；只有显式 WECHAT_RESTART=1 才重启当前微信。
EXISTING=$("$ROOT_DIR/scripts/find_wechat_pid.sh" | head -1 || true)
if [[ -n "$EXISTING" && "${WECHAT_RESTART:-0}" != "1" ]]; then
  echo "$EXISTING" > "$PID_FILE"
  echo "WeChat already running PID=$EXISTING (不重启，不影响登录态)"
  /bin/ps -p "$EXISTING" -o pid,etime,comm,args
  exit 0
fi

if [[ -n "$EXISTING" && "${WECHAT_RESTART:-0}" == "1" ]]; then
  echo "WECHAT_RESTART=1，仅结束当前微信 PID=$EXISTING"
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
      echo "拒绝强制结束：PID=${EXISTING} path=${ACTUAL} 不是当前微信" >&2
      exit 4
    fi
    echo "当前微信未响应 TERM，强制结束已验证 PID=$EXISTING"
    kill -KILL "$EXISTING" 2>/dev/null || true
    for _ in {1..20}; do
      if ! kill -0 "$EXISTING" 2>/dev/null; then break; fi
      sleep 0.25
    done
  fi
  if kill -0 "$EXISTING" 2>/dev/null; then
    echo "当前微信旧进程无法结束，拒绝继续启动" >&2
    exit 5
  fi
fi

: > "$LOG_FILE"
xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true

# 正常启动已安装微信，不传入任何创建新实例的参数。
/usr/bin/open -a "$APP" >>"$LOG_FILE" 2>&1

PID=""
for _ in {1..60}; do
  PID=$("$ROOT_DIR/scripts/find_wechat_pid.sh" | head -1 || true)
  if [[ -n "$PID" && "$PID" != "${EXISTING:-}" ]]; then break; fi
  PID=""
  sleep 0.5
done
if [[ -z "$PID" ]]; then
  echo "WeChat 启动失败；日志：$LOG_FILE" >&2
  sed -n '1,200p' "$LOG_FILE" >&2 || true
  exit 4
fi

echo "$PID" > "$PID_FILE"
echo "Started WeChat PID=$PID"
/bin/ps -p "$PID" -o pid,etime,comm,args
