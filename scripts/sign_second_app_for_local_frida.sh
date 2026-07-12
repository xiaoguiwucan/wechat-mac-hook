#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP="${WECHAT_SECOND_APP_PATH:-$ROOT_DIR/dist/WeChatSecond.app}"
ENTITLEMENTS="$ROOT_DIR/frida-second.entitlements"
if [[ ! -d "$APP" ]]; then echo "找不到第二微信：$APP" >&2; exit 1; fi
cat > "$ENTITLEMENTS" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "https://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>com.apple.application-identifier</key><string>5A4RE8SF68.com.tencent.xinWeChat</string>
    <key>com.apple.security.app-sandbox</key><true/>
    <key>com.apple.security.application-groups</key>
    <array><string>5A4RE8SF68.com.tencent.xinWeChat</string></array>
    <key>com.apple.security.cs.allow-jit</key><true/>
    <key>com.apple.security.cs.disable-library-validation</key><true/>
    <key>com.apple.security.cs.allow-unsigned-executable-memory</key><true/>
    <key>com.apple.security.device.audio-input</key><true/>
    <key>com.apple.security.device.camera</key><true/>
    <key>com.apple.security.device.usb</key><true/>
    <key>com.apple.security.files.bookmarks.app-scope</key><true/>
    <key>com.apple.security.files.downloads.read-write</key><true/>
    <key>com.apple.security.files.user-selected.read-write</key><true/>
    <key>com.apple.security.network.client</key><true/>
    <key>com.apple.security.network.server</key><true/>
    <key>com.apple.security.personal-information.location</key><true/>
    <key>com.apple.security.personal-information.photos-library</key><true/>
    <key>com.apple.security.print</key><true/>
    <key>com.apple.security.temporary-exception.mach-lookup.global-name</key>
    <array><string>com.tencent.xinWeChat-spks</string><string>com.tencent.xinWeChat-spki</string></array>
    <key>com.apple.security.temporary-exception.sbpl</key>
    <array><string>(allow network-outbound (literal &quot;/private/var/run/usbmuxd&quot;))</string></array>
    <key>get-task-allow</key><true/>
</dict>
</plist>
PLIST
xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true
codesign --force --deep --sign - --timestamp=none --entitlements "$ENTITLEMENTS" "$APP" >/dev/null
codesign -d --entitlements :- "$APP" 2>/dev/null | grep -E 'app-sandbox|application-groups|get-task-allow|allow-jit|disable-library' || true
echo "第二微信已为本地 Frida attach 重签：$APP"
