#!/usr/bin/env bash
set -euo pipefail
APP="${WECHAT2_APP:-$HOME/Applications/WeChat2.app}"
EXE="$APP/Contents/MacOS/WeChat"
if [[ ! -x "$EXE" ]]; then
  echo "找不到第二微信可执行文件: $EXE" >&2
  exit 1
fi
/usr/bin/python3 - "$EXE" <<'PY'
import ctypes
import os
import subprocess
import sys

exe = os.path.realpath(os.path.expanduser(sys.argv[1]))
libproc = ctypes.CDLL('/usr/lib/libproc.dylib')
PROC_PIDPATHINFO_MAXSIZE = 4096

def pid_path(pid: int) -> str:
    buf = ctypes.create_string_buffer(PROC_PIDPATHINFO_MAXSIZE)
    ret = libproc.proc_pidpath(pid, buf, PROC_PIDPATHINFO_MAXSIZE)
    if ret <= 0:
        return ''
    try:
        return os.path.realpath(buf.value.decode('utf-8', 'ignore'))
    except Exception:
        return ''

# 只在名为 WeChat 的主进程里找，避免匹配 WeChatAppEx/Helper/grep/脚本自身。
try:
    pids = subprocess.run(['/usr/bin/pgrep', '-x', 'WeChat'], check=False, capture_output=True, text=True).stdout.split()
except Exception:
    pids = []

matches = []
for s in pids:
    try:
        pid = int(s)
    except ValueError:
        continue
    path = pid_path(pid)
    if path == exe:
        matches.append(pid)

# 兜底：有些环境 proc_pidpath 可能取不到，才检查 ps args 的起始路径。
if not matches:
    ps = subprocess.run(['/bin/ps', '-axo', 'pid=,command='], check=False, capture_output=True, text=True).stdout.splitlines()
    prefix = exe + ' '
    for line in ps:
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        pid_s, cmd = parts
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        if cmd == exe or cmd.startswith(prefix):
            matches.append(pid)

for pid in sorted(set(matches)):
    print(pid)
PY
