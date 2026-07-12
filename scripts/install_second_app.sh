#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SRC_APP="${WECHAT_APP_PATH:-/Applications/WeChat.app}"
DST_APP="${WECHAT_SECOND_APP_PATH:-$ROOT_DIR/dist/WeChatSecond.app}"
HOOK="$ROOT_DIR/build/WeChatSecondHook.dylib"

if [[ ! -d "$SRC_APP" ]]; then
  echo "找不到微信：$SRC_APP" >&2
  exit 1
fi
if [[ ! -f "$HOOK" ]]; then
  "$ROOT_DIR/scripts/build.sh"
fi
if [[ "$SRC_APP" == "$DST_APP" ]]; then
  echo "拒绝覆盖主微信：SRC_APP 与 DST_APP 相同" >&2
  exit 1
fi

rm -rf "$DST_APP"
mkdir -p "$(dirname "$DST_APP")"
echo "Copying $SRC_APP -> $DST_APP"
ditto "$SRC_APP" "$DST_APP"

mkdir -p "$DST_APP/Contents/Frameworks"
cp -f "$HOOK" "$DST_APP/Contents/Frameworks/WeChatSecondHook.dylib"

EXE_NAME=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$DST_APP/Contents/Info.plist")
python3 "$ROOT_DIR/scripts/inject_load_dylib.py" "$DST_APP/Contents/MacOS/$EXE_NAME" '@executable_path/../Frameworks/WeChatSecondHook.dylib'

/usr/libexec/PlistBuddy -c 'Set :CFBundleName WeChatSecond' "$DST_APP/Contents/Info.plist" 2>/dev/null || true
/usr/libexec/PlistBuddy -c 'Set :CFBundleDisplayName WeChatSecond' "$DST_APP/Contents/Info.plist" 2>/dev/null || \
  /usr/libexec/PlistBuddy -c 'Add :CFBundleDisplayName string WeChatSecond' "$DST_APP/Contents/Info.plist" 2>/dev/null || true
# 保留原 Bundle ID，不通过 Info.plist 做数据隔离；隔离由 hook 完成。避免改过多内部依赖。

xattr -dr com.apple.quarantine "$DST_APP" 2>/dev/null || true
# 对副本 ad-hoc 重签，去掉 hardened runtime 对 DYLD_INSERT_LIBRARIES 的限制。主微信不改。
codesign --force --deep --sign - "$DST_APP" >/dev/null

echo "Installed second app: $DST_APP"
echo "主微信未修改：$SRC_APP"
