#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SECOND_HOME="$HOME/Library/Application Support/WeChatSecond"
SIDECAR_DIR="$ROOT_DIR/tools/voice_transcript_sidecar"
SIDECAR_BIN="$SIDECAR_DIR/voice-transcript-sidecar"
OCR_BIN="$ROOT_DIR/tools/voice_transcript_ocr/voice-transcript-ocr"
PID_FILE="$SECOND_HOME/voice-transcript-sidecar.pid"
LOG_FILE="$SECOND_HOME/logs/voice-transcript-sidecar.log"
APP="${WECHAT2_APP:-$HOME/Applications/WeChat2.app}"
EXE="$APP/Contents/MacOS/WeChat"
UI_EXE="$APP/Contents/MacOS/WeChatAppEx.app/Contents/MacOS/WeChatAppEx"
WECHAT_PID="${1:-}"

mkdir -p "$(dirname "$LOG_FILE")"
if [[ ! -x "$SIDECAR_BIN" ]]; then
  echo "找不到语音转文字观察器: $SIDECAR_BIN" >&2
  exit 1
fi
if [[ ! -x "$OCR_BIN" || "$ROOT_DIR/tools/voice_transcript_ocr/main.m" -nt "$OCR_BIN" ]]; then
  "$ROOT_DIR/scripts/build_voice_transcript_ocr.sh"
fi
if [[ -z "$WECHAT_PID" ]]; then
  WECHAT_PID="$("$ROOT_DIR/scripts/find_wechat2_pid.sh" | head -1 || true)"
fi
if [[ -z "$WECHAT_PID" ]]; then
  echo "找不到第二微信 PID，拒绝附加" >&2
  exit 2
fi
ACTUAL_EXE=$(/usr/bin/python3 - "$WECHAT_PID" <<'PY'
import ctypes, os, sys
pid = int(sys.argv[1])
buf = ctypes.create_string_buffer(4096)
ret = ctypes.CDLL('/usr/lib/libproc.dylib').proc_pidpath(pid, buf, 4096)
print(os.path.realpath(buf.value.decode('utf-8', 'ignore')) if ret > 0 else '')
PY
)
if [[ "$ACTUAL_EXE" != "$(/usr/bin/python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$EXE")" ]]; then
  echo "拒绝附加：PID=$WECHAT_PID 不是第二微信" >&2
  exit 3
fi

# The built-in voice transcript is rendered by WeChatAppEx on recent macOS
# clients. It is a direct child of WeChat2, so observing it preserves the same
# instance boundary and never attaches to the primary /Applications copy.
UI_PID="$(ps -axo pid=,ppid=,command= | awk -v parent="$WECHAT_PID" '$2 == parent && $0 ~ /WeChatAppEx\.app\/Contents\/MacOS\/WeChatAppEx( |$)/ { print $1; exit }' || true)"
if [[ -n "$UI_PID" ]]; then
  ACTUAL_UI_EXE=$(/usr/bin/python3 - "$UI_PID" <<'PY'
import ctypes, os, sys
pid = int(sys.argv[1])
buf = ctypes.create_string_buffer(4096)
ret = ctypes.CDLL('/usr/lib/libproc.dylib').proc_pidpath(pid, buf, 4096)
print(os.path.realpath(buf.value.decode('utf-8', 'ignore')) if ret > 0 else '')
PY
)
  if [[ "$ACTUAL_UI_EXE" != "$(/usr/bin/python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$UI_EXE")" ]]; then
    echo "忽略非第二微信 UI 子进程 PID=$UI_PID" >&2
    UI_PID=""
  fi
fi

RENDERER_PIDS=""
if [[ -n "$UI_PID" ]]; then
  while IFS= read -r RENDERER_PID; do
    [[ -n "$RENDERER_PID" ]] || continue
    ACTUAL_RENDERER_EXE=$(/usr/bin/python3 - "$RENDERER_PID" <<'PY'
import ctypes, os, sys
pid = int(sys.argv[1])
buf = ctypes.create_string_buffer(4096)
ret = ctypes.CDLL('/usr/lib/libproc.dylib').proc_pidpath(pid, buf, 4096)
print(os.path.realpath(buf.value.decode('utf-8', 'ignore')) if ret > 0 else '')
PY
)
    if [[ "$ACTUAL_RENDERER_EXE" == "$APP/Contents/"*"WeChatAppEx Helper (Renderer).app/Contents/MacOS/WeChatAppEx Helper (Renderer)" ]]; then
      RENDERER_PIDS+="${RENDERER_PIDS:+,}$RENDERER_PID"
    fi
  done < <(ps -axo pid=,ppid=,command= | awk -v parent="$UI_PID" '$2 == parent && /WeChatAppEx Helper \(Renderer\).*com\.tencent\.xinWeChat\.instance2/ { print $1 }')
fi

OLD="$(cat "$PID_FILE" 2>/dev/null || true)"
if [[ -n "$OLD" ]] && kill -0 "$OLD" 2>/dev/null; then
  CMD="$(ps -p "$OLD" -o command= 2>/dev/null || true)"
  if [[ "$CMD" == *"$SIDECAR_BIN"* ]]; then
    echo "Voice transcript sidecar already running PID=$OLD"
    exit 0
  fi
fi

: > "$LOG_FILE"
WECHAT2_UI_PID="$UI_PID" WECHAT2_RENDERER_PIDS="$RENDERER_PIDS" WECHAT2_OCR_BIN="$OCR_BIN" /usr/bin/python3 - "$SIDECAR_BIN" "$WECHAT_PID" "$LOG_FILE" "$PID_FILE" <<'PY'
import os
import subprocess
import sys

binary, wechat_pid, log_path, pid_path = sys.argv[1:]
log = open(log_path, 'ab', buffering=0)
cmd = [binary, f'-wechat-pid={wechat_pid}']
ui_pid = os.environ.get('WECHAT2_UI_PID', '')
if ui_pid:
    cmd.append(f'-ui-pid={ui_pid}')
renderer_pids = os.environ.get('WECHAT2_RENDERER_PIDS', '')
if renderer_pids:
    cmd.append(f'-renderer-pids={renderer_pids}')
ocr_bin = os.environ.get('WECHAT2_OCR_BIN', '')
if ocr_bin:
    cmd.append(f'-ocr-bin={ocr_bin}')
proc = subprocess.Popen(
    cmd,
    stdout=log,
    stderr=subprocess.STDOUT,
    stdin=subprocess.DEVNULL,
    start_new_session=True,
    close_fds=True,
)
with open(pid_path, 'w') as fh:
    fh.write(str(proc.pid))
PY

for _ in {1..20}; do
  SIDECAR_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "$SIDECAR_PID" ]] && kill -0 "$SIDECAR_PID" 2>/dev/null && grep -q 'voice transcript sidecar attached' "$LOG_FILE" 2>/dev/null; then
    echo "Voice transcript sidecar PID=$SIDECAR_PID attached WeChat2 PID=$WECHAT_PID"
    exit 0
  fi
  sleep 0.25
done
echo "Voice transcript sidecar did not become ready; log: $LOG_FILE" >&2
exit 4
