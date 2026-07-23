#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MANIFEST="$ROOT_DIR/config/wechat_target.json"
GADGET_CONFIG="$ROOT_DIR/frida-gadget/FridaGadget.config"
LOAD_PATH="@executable_path/../Frameworks/FridaGadget.dylib"

IFS=$'\t' read -r APP EXPECTED_BUNDLE EXPECTED_VERSION EXPECTED_BUILD EXPECTED_MAIN_SHA GADGET_VERSION EXPECTED_GADGET_SHA < <(
  /usr/bin/python3 - "$MANIFEST" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
keys = (
    "app", "bundle_id", "version", "build", "main_sha256",
    "frida_gadget_version", "frida_gadget_sha256",
)
print("\t".join(str(m[key]) for key in keys))
PY
)
EXE="$APP/Contents/MacOS/WeChat"
FRAMEWORKS="$APP/Contents/Frameworks"
GADGET_DST="$FRAMEWORKS/FridaGadget.dylib"
CONFIG_DST="$FRAMEWORKS/FridaGadget.config"
CONFIG_RESOURCE="$APP/Contents/Resources/FridaGadget.config"
GADGET_SRC="$ROOT_DIR/downloads/frida-gadget-${GADGET_VERSION}-macos-universal.dylib"
GADGET_XZ="$GADGET_SRC.xz"

if [[ "$APP" != "/Applications/WeChat.app" ]]; then
  echo "拒绝修改非唯一安装目标: $APP" >&2
  exit 2
fi
if [[ "$EUID" -ne 0 ]]; then
  echo "需要管理员权限写入 $APP，请执行：sudo $0" >&2
  exit 77
fi
[[ -x "$EXE" ]] || { echo "当前微信不完整: $APP" >&2; exit 3; }

RUNNING=$(/usr/bin/pgrep -x WeChat 2>/dev/null || true)
if [[ -n "$RUNNING" ]]; then
  echo "微信正在运行（PID: ${RUNNING//$'\n'/,}），拒绝在运行中修改签名；请先正常退出微信。" >&2
  exit 8
fi

BUNDLE=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$APP/Contents/Info.plist")
VERSION=$(/usr/libexec/PlistBuddy -c 'Print :WeChatBundleVersion' "$APP/Contents/Info.plist")
BUILD=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$APP/Contents/Info.plist")
[[ "$BUNDLE" == "$EXPECTED_BUNDLE" && "$VERSION" == "$EXPECTED_VERSION" && "$BUILD" == "$EXPECTED_BUILD" ]] || {
  echo "目标版本不匹配: bundle=$BUNDLE version=$VERSION build=$BUILD" >&2
  exit 4
}

if [[ ! -f "$GADGET_SRC" && -f "$GADGET_XZ" ]]; then
  /usr/bin/python3 - "$GADGET_XZ" "$GADGET_SRC" <<'PY'
import lzma, shutil, sys
with lzma.open(sys.argv[1], "rb") as source, open(sys.argv[2], "wb") as target:
    shutil.copyfileobj(source, target)
PY
fi
[[ -f "$GADGET_SRC" ]] || {
  echo "缺少 Frida Gadget $GADGET_VERSION：$GADGET_SRC" >&2
  echo "下载地址: https://github.com/frida/frida/releases/download/$GADGET_VERSION/frida-gadget-$GADGET_VERSION-macos-universal.dylib.xz" >&2
  exit 5
}
ACTUAL_GADGET_SHA=$(shasum -a 256 "$GADGET_SRC" | awk '{print $1}')
[[ "$ACTUAL_GADGET_SHA" == "$EXPECTED_GADGET_SHA" ]] || {
  echo "Frida Gadget 摘要不匹配: $ACTUAL_GADGET_SHA" >&2
  exit 6
}

if ! otool -L "$EXE" | grep -Fq "$LOAD_PATH"; then
  MAIN_SHA=$(shasum -a 256 "$EXE" | awk '{print $1}')
  [[ "$MAIN_SHA" == "$EXPECTED_MAIN_SHA" ]] || {
    echo "微信主程序不是已锁定的官方原件，拒绝首次注入: $MAIN_SHA" >&2
    exit 7
  }

  CALLER_USER="${SUDO_USER:-$(id -un)}"
  CALLER_HOME=$(/usr/bin/dscl . -read "/Users/$CALLER_USER" NFSHomeDirectory 2>/dev/null | awk '{print $2}')
  [[ -n "$CALLER_HOME" ]] || CALLER_HOME="/Users/$CALLER_USER"
  BACKUP_DIR="$CALLER_HOME/Library/Application Support/WeChatAgent/migration-backups"
  BACKUP="$BACKUP_DIR/WeChat-${EXPECTED_VERSION}-${EXPECTED_BUILD}-official.zip"
  mkdir -p "$BACKUP_DIR"
  if [[ ! -f "$BACKUP" ]]; then
    echo "备份官方微信到: $BACKUP"
    /usr/bin/ditto -c -k --sequesterRsrc --keepParent "$APP" "$BACKUP"
    chown "$CALLER_USER":staff "$BACKUP" 2>/dev/null || true
  fi

  mkdir -p "$FRAMEWORKS"
  cp -f "$GADGET_SRC" "$GADGET_DST"
  chmod +x "$GADGET_DST"
  /usr/bin/python3 "$ROOT_DIR/scripts/inject_load_dylib.py" "$EXE" "$LOAD_PATH"
else
  echo "Frida Gadget load command 已存在，刷新 Gadget 与签名"
  mkdir -p "$FRAMEWORKS"
  cp -f "$GADGET_SRC" "$GADGET_DST"
  chmod +x "$GADGET_DST"
fi

# Frameworks 顶层的普通 JSON 会被 codesign 当成未签名嵌套代码。把真实配置
# 纳入 Resources 资源封印，并在 Gadget 旁放相对符号链接；Frida 仍按约定路径读取。
rm -f "$CONFIG_DST"
cp -f "$GADGET_CONFIG" "$CONFIG_RESOURCE"
ln -s "../Resources/FridaGadget.config" "$CONFIG_DST"

xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true
xattr -dr com.apple.provenance "$APP" 2>/dev/null || true
codesign -f -s - --timestamp=none "$GADGET_DST"
/usr/bin/python3 - "$APP/Contents" <<'PY'
import pathlib
import subprocess
import sys

root = pathlib.Path(sys.argv[1])
suffixes = (".framework", ".dylib", ".bundle", ".xpc", ".appex", ".app")
code = [
    path
    for path in root.rglob("*")
    if (path.is_file() and path.name.endswith(".dylib"))
    or (path.is_dir() and path.name.endswith(suffixes))
]
for path in sorted(code, key=lambda item: len(item.parts), reverse=True):
    subprocess.run(
        ["codesign", "-f", "-s", "-", "--timestamp=none", str(path)],
        check=True,
    )
PY
# Gadget 模式不做 task_for_pid attach。macOS 26 会拒绝没有 provisioning
# profile 却携带 get-task-allow / allow-jit 等受限 entitlement 的临时签名 App；
# 使用非 hardened 的纯 ad-hoc 签名即可让 Gadget 执行 JIT。
codesign -f -s - --timestamp=none --force "$APP"
xattr -dr com.apple.provenance "$APP" 2>/dev/null || true
codesign --verify --deep --strict "$APP"
codesign --verify --strict "$APP"
codesign --verify --strict "$GADGET_DST"

echo "Frida Gadget 已安装到唯一微信: $APP"
echo "Gadget: $GADGET_DST ($GADGET_VERSION)"
echo "监听: 127.0.0.1:27042（微信重启后生效）"
