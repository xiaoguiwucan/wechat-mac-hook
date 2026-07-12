#!/usr/bin/env bash
set -euo pipefail

APP_PATH="/Applications/WeChat.app"
GADGET_PATH="$APP_PATH/Contents/Frameworks/FridaGadget.dylib"

# =========================
# Resolve script directory (do not depend on current working directory)
# =========================
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# Support running from:
# 1) Project root:        SCRIPT_DIR = project root
# 2) Project/frida-gadget: SCRIPT_DIR = project root/frida-gadget
if [[ "$(basename "$SCRIPT_DIR")" == "frida-gadget" ]]; then
  PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
else
  PROJECT_DIR="$SCRIPT_DIR"
fi

ENTITLEMENTS="$PROJECT_DIR/frida-gadget/temp.entitlements"

# Ensure entitlements file exists
if [[ ! -f "$ENTITLEMENTS" ]]; then
  echo "[-] Entitlements file not found: $ENTITLEMENTS" >&2
  echo "    Expected location: <project-root>/frida-gadget/temp.entitlements" >&2
  exit 1
fi

echo "[+] Using entitlements: $ENTITLEMENTS"
echo "[+] Signing gadget..."
codesign -f -s - --timestamp=none "$GADGET_PATH"

echo "[+] Deep-signing all embedded frameworks and dylibs..."
find "$APP_PATH/Contents" \( -name "*.framework" -o -name "*.dylib" -o -name "*.bundle" \) -print0 \
  | xargs -0 codesign -f -s - --timestamp=none

echo "[+] Signing main app and injecting entitlements..."
codesign -f -s - --timestamp=none --entitlements "$ENTITLEMENTS" --force "$APP_PATH"

echo "[+] Done!"
