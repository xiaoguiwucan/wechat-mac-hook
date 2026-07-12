#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP="${WECHAT_SECOND_APP_PATH:-$ROOT_DIR/dist/WeChatSecond.app}"
[[ -d "$APP" ]] || APP="${WECHAT_APP_PATH:-/Applications/WeChat.app}"
SHORT=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$APP/Contents/Info.plist" 2>/dev/null || true)
BUNDLE=$(/usr/libexec/PlistBuddy -c 'Print :WeChatBundleVersion' "$APP/Contents/Info.plist" 2>/dev/null || true)
BUILD=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$APP/Contents/Info.plist" 2>/dev/null || true)
echo "CFBundleShortVersionString=$SHORT"
echo "WeChatBundleVersion=$BUNDLE"
echo "CFBundleVersion=$BUILD"
case "$BUNDLE" in
  4.1.11.54) echo "SuggestedConf=$ROOT_DIR/config/4_1_11_54_mac.experimental.json" ;;
  4.1.11.53) echo "SuggestedConf=$ROOT_DIR/vendor/wechat_chatter/wechat_version/4_1_11_53_mac.json" ;;
  *) echo "SuggestedConf=" ;;
esac
