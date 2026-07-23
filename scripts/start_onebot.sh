#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
AGENT_HOME="$HOME/Library/Application Support/WeChatAgent"
ONEBOT_DIR="$ROOT_DIR/tools/onebot/onebot"
ONEBOT_BIN="$ONEBOT_DIR/onebot"
CONF="$ROOT_DIR/tools/onebot/wechat_version/4_1_11_53_mac.json"
LOG_DIR="$AGENT_HOME/logs"
LOG_FILE="$LOG_DIR/onebot-wechat.log"
PID_FILE="$AGENT_HOME/onebot-wechat.pid"
RECEIVE_HOST="${ONEBOT_RECEIVE_HOST:-127.0.0.1:58080}"
SEND_URL="${ONEBOT_SEND_URL:-http://127.0.0.1:36060/onebot}"
IMAGE_PATH="${ONEBOT_IMAGE_PATH:-$HOME/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files}"
SELF_ID="${ONEBOT_SELF_ID:-}"
MEDIA_PROBE="${ONEBOT_MEDIA_PROBE:-false}"
APP="${WECHAT_APP:-/Applications/WeChat.app}"
EXE="$APP/Contents/MacOS/WeChat"
GADGET="$APP/Contents/Frameworks/FridaGadget.dylib"
GADGET_LOAD="@executable_path/../Frameworks/FridaGadget.dylib"
GADGET_ADDR="${FRIDA_GADGET_ADDR:-127.0.0.1:27042}"
TOOL_BIN="$ROOT_DIR/tools/bin"
mkdir -p "$LOG_DIR" "$IMAGE_PATH" "$TOOL_BIN"

# record/voice 发送需要 ffmpeg 转 SILK。优先使用系统 PATH；没有时使用 Python
# imageio-ffmpeg 提供的本地二进制，并注入到 OneBot 进程 PATH。
if ! command -v ffmpeg >/dev/null 2>&1 && [[ ! -x "$TOOL_BIN/ffmpeg" ]]; then
  FFMPEG_BIN=$(/usr/bin/python3 - <<'PY' 2>/dev/null || true
try:
    import imageio_ffmpeg
    print(imageio_ffmpeg.get_ffmpeg_exe())
except Exception:
    pass
PY
)
  if [[ -n "${FFMPEG_BIN:-}" && -x "$FFMPEG_BIN" ]]; then
    ln -sf "$FFMPEG_BIN" "$TOOL_BIN/ffmpeg"
  fi
fi
export PATH="$TOOL_BIN:/opt/homebrew/bin:/usr/local/bin:$PATH"

if [[ ! -x "$ONEBOT_BIN" ]]; then
  echo "找不到 OneBot: $ONEBOT_BIN" >&2
  exit 1
fi
if [[ ! -f "$CONF" ]]; then
  echo "找不到配置: $CONF" >&2
  exit 1
fi

if [[ ! -f "$GADGET" ]] || ! otool -L "$EXE" | grep -Fq "$GADGET_LOAD"; then
  echo "当前唯一微信尚未安装 Frida Gadget；SIP 开启时不再使用 PID attach。" >&2
  echo "先执行: sudo \"$ROOT_DIR/scripts/install_frida_gadget.sh\"" >&2
  echo "然后执行: WECHAT_RESTART=1 \"$ROOT_DIR/scripts/launch_wechat.sh\"" >&2
  exit 10
fi

# 先执行唯一实例、Bundle ID 和版本校验；已登录微信会直接复用。
"$ROOT_DIR/scripts/launch_wechat.sh"
PID=$("$ROOT_DIR/scripts/find_wechat_pid.sh" | head -1 || true)
if [[ -z "$PID" ]]; then
  echo "找不到当前微信 PID，拒绝附加" >&2
  exit 2
fi

# 从当前微信进程正在使用的 xwechat_files 目录确定 self_id，
# 避免多账号历史目录导致绑定错账号。
if [[ -z "$SELF_ID" ]]; then
  SELF_ID=$(/usr/sbin/lsof -p "$PID" 2>/dev/null \
    | sed -nE 's#.*xwechat_files/(wxid_[^/_[:space:]]+)(_[^/[:space:]]+)?/.*#\1#p' \
    | sort -u | head -1 || true)
fi
# macOS 26 的 ad-hoc Gadget 会在 OneBot 连接前暂停微信初始化，因此 lsof
# 可能只能看到 all_users。此时接受最近 24 小时内、且比第二候选至少新
# 两分钟的唯一 db_storage 账号目录，避免启动等待与 self_id 发现形成死锁。
if [[ -z "$SELF_ID" ]]; then
  SELF_ID=$(/usr/bin/python3 - "$IMAGE_PATH" <<'PY'
import pathlib
import re
import sys
import time

root = pathlib.Path(sys.argv[1])
now = time.time()
candidates = []
for account_dir in root.glob("wxid_*"):
    if not account_dir.is_dir():
        continue
    newest = 0.0
    storage = account_dir / "db_storage"
    if storage.is_dir():
        for path in storage.rglob("*"):
            try:
                if path.is_file():
                    newest = max(newest, path.stat().st_mtime)
            except OSError:
                pass
    if newest:
        candidates.append((newest, account_dir.name))
candidates.sort(reverse=True)
if candidates and now - candidates[0][0] <= 86400 and (
    len(candidates) == 1 or candidates[0][0] - candidates[1][0] > 120
):
    name = candidates[0][1]
    match = re.fullmatch(r"(wxid_.+?)(?:_[0-9a-fA-F]{4})?", name)
    if match:
        print(match.group(1))
PY
)
fi
if [[ -z "$SELF_ID" ]] && command -v sqlite3 >/dev/null 2>&1; then
  SELF_ID=$(sqlite3 "$AGENT_HOME/memory/wechat-memory.sqlite3" \
    "SELECT json_extract(raw_json,'$.self_id') FROM messages WHERE COALESCE(json_extract(raw_json,'$.self_id'),'') LIKE 'wxid_%' AND json_extract(raw_json,'$.self_id') NOT IN ('wxid_self','wxid_test_bot') ORDER BY event_time DESC, rowid DESC LIMIT 1;" \
    2>/dev/null || true)
fi
if [[ -z "$SELF_ID" || "$SELF_ID" != wxid_* ]]; then
  echo "无法从当前微信进程确定 self_id，拒绝启动发送链路" >&2
  exit 9
fi

ACTUAL_EXE=$(/usr/bin/python3 - "$PID" <<'PY'
import ctypes, os, sys
pid=int(sys.argv[1])
buf=ctypes.create_string_buffer(4096)
ret=ctypes.CDLL('/usr/lib/libproc.dylib').proc_pidpath(pid, buf, 4096)
print(os.path.realpath(buf.value.decode('utf-8','ignore')) if ret > 0 else '')
PY
)
if [[ "$ACTUAL_EXE" != "$(/usr/bin/python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$EXE")" ]]; then
  echo "拒绝附加：PID=${PID} path=${ACTUAL_EXE}，不是当前微信 ${EXE}" >&2
  exit 3
fi

GADGET_PORT="${GADGET_ADDR##*:}"
GADGET_READY=0
for _ in {1..30}; do
  if /usr/sbin/lsof -a -p "$PID" -iTCP:"$GADGET_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    GADGET_READY=1
    break
  fi
  sleep 0.5
done
if [[ "$GADGET_READY" != "1" ]]; then
  echo "当前微信未监听 Frida Gadget: $GADGET_ADDR；需要重启唯一微信使注入生效。" >&2
  echo "执行: WECHAT_RESTART=1 \"$ROOT_DIR/scripts/launch_wechat.sh\"" >&2
  exit 11
fi

# 停旧 OneBot：只杀本脚本启动的 onebot，绝不杀微信。
OLD=$(cat "$PID_FILE" 2>/dev/null || true)
if [[ -n "$OLD" ]] && kill -0 "$OLD" 2>/dev/null; then
  CMD=$(/bin/ps -p "$OLD" -o command= 2>/dev/null || true)
  if [[ "$CMD" == *"$ONEBOT_BIN"* ]]; then
    kill "$OLD" 2>/dev/null || true
    sleep 1
  fi
fi

# 如果端口被旧 onebot 占用，杀旧 onebot；如果是别的程序，直接报错，避免误杀。
PORT="${RECEIVE_HOST##*:}"
PORT_PIDS=$(/usr/sbin/lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)
if [[ -n "$PORT_PIDS" ]]; then
  while read -r PPID_; do
    [[ -z "$PPID_" ]] && continue
    CMD=$(/bin/ps -p "$PPID_" -o command= 2>/dev/null || true)
    if [[ "$CMD" == *"$ONEBOT_BIN"* ]]; then
      kill "$PPID_" 2>/dev/null || true
    else
      echo "端口 $PORT 被非 OneBot 进程占用：PID=$PPID_ $CMD" >&2
      exit 4
    fi
  done <<< "$PORT_PIDS"
  sleep 1
fi

xattr -dr com.apple.quarantine "$ONEBOT_BIN" 2>/dev/null || true
codesign --force --sign - --timestamp=none "$ONEBOT_BIN" >/dev/null 2>&1 || true

: > "$LOG_FILE"
# 用 setsid/start_new_session 真正脱离当前执行 shell，避免 Codex/终端回收后台进程时把 OneBot 一起杀掉。
/usr/bin/python3 - "$ONEBOT_DIR" "$ONEBOT_BIN" "$PID_FILE" "$LOG_FILE" "$PID" "$CONF" "$RECEIVE_HOST" "$SEND_URL" "$IMAGE_PATH" "$SELF_ID" "$MEDIA_PROBE" "$GADGET_ADDR" <<'PY'
import os
import subprocess
import sys

onebot_dir, onebot_bin, pid_file, log_file, wechat_pid, conf, receive_host, send_url, image_path, self_id, media_probe, gadget_addr = sys.argv[1:]
args = [
    onebot_bin,
    '-type=gadget',
    f'-gadget_addr={gadget_addr}',
    f'-wechat_pid={wechat_pid}',
    f'-wechat_conf={conf}',
    f'-receive_host={receive_host}',
    f'-send_url={send_url}',
    f'-image_path={image_path}',
    f'-self_id={self_id}',
    '-conn_type=http',
    '-log_level=info',
    f'-media_probe={media_probe}',
]
log = open(log_file, 'ab', buffering=0)
proc = subprocess.Popen(
    args,
    cwd=onebot_dir,
    stdin=subprocess.DEVNULL,
    stdout=log,
    stderr=subprocess.STDOUT,
    start_new_session=True,
    close_fds=True,
)
with open(pid_file, 'w') as f:
    f.write(str(proc.pid))
PY

OPID=$(cat "$PID_FILE")
READY=0
for _ in {1..80}; do
  if ! kill -0 "$OPID" 2>/dev/null; then
    echo "OneBot 已退出；日志：" >&2
    sed -n '1,260p' "$LOG_FILE" >&2 || true
    exit 5
  fi
  # “Frida 已就绪”只表示脚本已加载；必须等到动态消息与接收 Hook。
  # UploadMedia 属于独立发送通道，未就绪时保持 OneBot 在线并由状态页
  # 标记为降级，不能因此杀掉已经正常工作的消息接收链路。
  if /usr/sbin/lsof -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 \
    && grep -q 'Dynamic Text Message Setup Complete' "$LOG_FILE" 2>/dev/null \
    && grep -q 'Receiver buf2resp hook attached' "$LOG_FILE" 2>/dev/null; then
    READY=1
    break
  fi
  sleep 0.5
done

if ! kill -0 "$PID" 2>/dev/null; then
  echo "当前微信在附加过程中退出；日志：" >&2
  sed -n '1,260p' "$LOG_FILE" >&2 || true
  exit 6
fi
if ! /usr/sbin/lsof -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "OneBot attach 超时，HTTP 端口 $PORT 未监听；当前微信保持运行" >&2
  sed -n '1,260p' "$LOG_FILE" >&2 || true
  kill "$OPID" 2>/dev/null || true
  rm -f "$PID_FILE"
  exit 7
fi
if [[ "$READY" != "1" ]]; then
  echo "OneBot 初始化超时：接收 Hook 未就绪；拒绝误报启动成功" >&2
  sed -n '1,260p' "$LOG_FILE" >&2 || true
  kill "$OPID" 2>/dev/null || true
  rm -f "$PID_FILE"
  exit 8
fi

echo "OneBot PID=$OPID attached WeChat PID=$PID"
echo "HTTP: http://$RECEIVE_HOST"
sed -n '1,220p' "$LOG_FILE" || true

# UI 语音转文字观察器已停用：当前方案改为直接抓取语音原始文件并走 ASR。
# 如需临时恢复旧 UI 识别方案，可手动设置 ENABLE_UI_VOICE_TRANSCRIPT=1。
if [[ "${ENABLE_UI_VOICE_TRANSCRIPT:-0}" == "1" ]]; then
  "$ROOT_DIR/scripts/start_voice_transcript_sidecar.sh" "$PID" || true
fi
