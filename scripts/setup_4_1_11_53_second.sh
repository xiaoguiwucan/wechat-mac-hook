#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SRC_APP="$ROOT_DIR/downloads/WeChat-4.1.11.53/WeChat.app"
DMG="$ROOT_DIR/downloads/WeChatMac-4.1.11.53.dmg"
if [[ ! -d "$SRC_APP" ]]; then
  if [[ ! -f "$DMG" ]]; then
    echo "缺少 $DMG，请先下载 4.1.11.53 dmg" >&2
    exit 1
  fi
  mkdir -p "$ROOT_DIR/downloads/WeChat-4.1.11.53"
  hdiutil attach "$DMG" -nobrowse -noautoopen -readonly -plist > "$ROOT_DIR/build/mount.plist"
  MNT=$(/usr/libexec/PlistBuddy -c 'Print :system-entities:0:mount-point' "$ROOT_DIR/build/mount.plist" 2>/dev/null || true)
  [[ -d "$MNT" ]] || MNT=$(/usr/libexec/PlistBuddy -c 'Print :system-entities:1:mount-point' "$ROOT_DIR/build/mount.plist" 2>/dev/null || true)
  [[ -d "$MNT" ]] || { echo "无法挂载 $DMG" >&2; exit 1; }
  SRC_IN_DMG=$(find "$MNT" -maxdepth 2 -name 'WeChat.app' -type d | head -1)
  ditto "$SRC_IN_DMG" "$SRC_APP"
  hdiutil detach "$MNT" >/dev/null || true
fi
"$ROOT_DIR/scripts/build.sh"
WECHAT_APP_PATH="$SRC_APP" "$ROOT_DIR/scripts/install_second_app.sh"
echo "完成：第二微信 4.1.11.53 已安装到 $ROOT_DIR/dist/WeChatSecond.app"
echo "启动：$ROOT_DIR/scripts/launch_second_wechat.sh"
echo "OneBot：$ROOT_DIR/scripts/start_onebot_second.sh"
