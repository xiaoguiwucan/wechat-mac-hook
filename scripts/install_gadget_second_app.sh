#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP="${WECHAT_SECOND_APP_PATH:-$ROOT_DIR/dist/WeChatSecond.app}"
GADGET_SRC="${FRIDA_GADGET_PATH:-$ROOT_DIR/downloads/frida-gadget-17.8.0-macos-universal.dylib}"
ENTITLEMENTS="$ROOT_DIR/frida-second.entitlements"

if [[ ! -d "$APP" ]]; then
  echo "找不到第二微信：$APP，请先运行 scripts/install_second_app.sh" >&2
  exit 1
fi
if [[ ! -f "$GADGET_SRC" ]]; then
  echo "找不到 FridaGadget：$GADGET_SRC" >&2
  exit 1
fi
EXE_NAME=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$APP/Contents/Info.plist")
EXE="$APP/Contents/MacOS/$EXE_NAME"
mkdir -p "$APP/Contents/Frameworks"
cp -f "$GADGET_SRC" "$APP/Contents/Frameworks/FridaGadget.dylib"
chmod +x "$APP/Contents/Frameworks/FridaGadget.dylib"
cat > "$APP/Contents/Frameworks/FridaGadget.config" <<'JSON'
{
  "interaction": {
    "type": "listen",
    "address": "127.0.0.1",
    "port": 27042,
    "on_load": "resume"
  }
}
JSON
python3 "$ROOT_DIR/scripts/inject_load_dylib.py" "$EXE" '@executable_path/../Frameworks/FridaGadget.dylib'
cat > "$ENTITLEMENTS" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "https://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>com.apple.security.cs.disable-library-validation</key><true/>
    <key>com.apple.security.cs.allow-jit</key><true/>
    <key>com.apple.security.cs.allow-unsigned-executable-memory</key><true/>
    <key>com.apple.security.cs.allow-dyld-environment-variables</key><true/>
    <key>get-task-allow</key><true/>
</dict>
</plist>
PLIST
xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true
find "$APP/Contents" \( -name '*.framework' -o -name '*.dylib' -o -name '*.bundle' \) -print0 | xargs -0 -I{} codesign -f -s - --timestamp=none "{}" >/dev/null 2>&1 || true
codesign --force --deep --sign - --timestamp=none --entitlements "$ENTITLEMENTS" "$APP" >/dev/null

echo "Frida Gadget 已注入第二微信：$APP"
otool -l "$EXE" | grep -A4 'FridaGadget' || true
