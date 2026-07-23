#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP="${WECHAT_APP_PATH:-/Applications/WeChat.app}"
SHORT=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$APP/Contents/Info.plist" 2>/dev/null || true)
BUNDLE=$(/usr/libexec/PlistBuddy -c 'Print :WeChatBundleVersion' "$APP/Contents/Info.plist" 2>/dev/null || true)
BUILD=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$APP/Contents/Info.plist" 2>/dev/null || true)
echo "CFBundleShortVersionString=$SHORT"
echo "WeChatBundleVersion=$BUNDLE"
echo "CFBundleVersion=$BUILD"
if [[ "$BUNDLE" == "4.1.11.53" && "$BUILD" == "269109" ]]; then
  echo "SuggestedConf=$ROOT_DIR/tools/onebot/wechat_version/4_1_11_53_mac.json"
else
  echo "SuggestedConf="
  exit 2
fi
