#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MANIFEST="$ROOT_DIR/config/wechat_target.json"
QUIET="${1:-}"

IFS=$'\t' read -r DEFAULT_APP EXPECTED_BUNDLE EXPECTED_TEAM EXPECTED_VERSION EXPECTED_BUILD EXPECTED_MAIN_SHA EXPECTED_MAIN_GADGET_UNSIGNED_SHA EXPECTED_CORE_SHA EXPECTED_CORE_GADGET_SHA EXPECTED_MODULE_SIZE GADGET_VERSION EXPECTED_GADGET_SIGNED_SHA CONF_REL < <(/usr/bin/python3 - "$MANIFEST" <<'PY'
import json, sys
m=json.load(open(sys.argv[1]))
keys=(
    'app','bundle_id','team_id','version','build','main_sha256',
    'main_gadget_unsigned_sha256','core_arm64_sha256',
    'core_arm64_gadget_sha256','core_module_size',
    'frida_gadget_version','frida_gadget_signed_sha256','onebot_conf',
)
print('\t'.join(str(m[key]) for key in keys))
PY
)
APP="${WECHAT_APP:-$DEFAULT_APP}"
CONF="$ROOT_DIR/$CONF_REL"
PLIST="$APP/Contents/Info.plist"
EXE="$APP/Contents/MacOS/WeChat"
CORE="$APP/Contents/Resources/wechat.dylib"
GADGET="$APP/Contents/Frameworks/FridaGadget.dylib"
GADGET_LOAD="@executable_path/../Frameworks/FridaGadget.dylib"

[[ -x "$EXE" && -f "$CORE" ]] || { echo "目标微信文件不完整: $APP" >&2; exit 1; }
BUNDLE=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$PLIST")
VERSION=$(/usr/libexec/PlistBuddy -c 'Print :WeChatBundleVersion' "$PLIST")
BUILD=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$PLIST")
[[ "$BUNDLE" == "$EXPECTED_BUNDLE" && "$VERSION" == "$EXPECTED_VERSION" && "$BUILD" == "$EXPECTED_BUILD" ]] || {
  echo "目标不匹配: bundle=$BUNDLE version=$VERSION build=$BUILD" >&2; exit 2;
}
TEAM=$(codesign -dv --verbose=4 "$APP" 2>&1 | sed -n 's/^TeamIdentifier=//p' | head -1)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
SIGNATURE_MODE="official"
if otool -L "$EXE" | grep -Fq "$GADGET_LOAD"; then
  SIGNATURE_MODE="frida-gadget"
  [[ -f "$GADGET" ]] || { echo "Frida Gadget load command 存在但 dylib 缺失" >&2; exit 3; }
  codesign --verify --deep --strict "$APP" >/dev/null 2>&1 || {
    echo "本地 Gadget 微信签名校验失败" >&2
    exit 4
  }
  codesign --verify --strict "$GADGET" >/dev/null 2>&1 || {
    echo "Frida Gadget 签名校验失败" >&2
    exit 4
  }
  ENTITLEMENTS=$(codesign -d --entitlements :- "$APP" 2>/dev/null || true)
  if [[ -n "$ENTITLEMENTS" ]]; then
    /usr/bin/python3 -c 'import plistlib,sys; p=plistlib.loads(sys.stdin.buffer.read()); forbidden={\"get-task-allow\",\"com.apple.security.cs.allow-jit\",\"com.apple.security.cs.allow-unsigned-executable-memory\"}; raise SystemExit(1 if forbidden.intersection(p) else 0)' <<< "$ENTITLEMENTS" || {
      echo "本地 Gadget 微信携带 macOS 26 无描述文件时禁止的受限 entitlement" >&2
      exit 5
    }
  fi
  GADGET_SHA=$(shasum -a 256 "$GADGET" | awk '{print $1}')
  [[ "$GADGET_SHA" == "$EXPECTED_GADGET_SIGNED_SHA" ]] || {
    echo "Frida Gadget $GADGET_VERSION 摘要不匹配: $GADGET_SHA" >&2
    exit 6
  }
  cp "$EXE" "$TMP/WeChat"
  codesign --remove-signature "$TMP/WeChat" >/dev/null 2>&1
  MAIN_SHA=$(shasum -a 256 "$TMP/WeChat" | awk '{print $1}')
  [[ "$MAIN_SHA" == "$EXPECTED_MAIN_GADGET_UNSIGNED_SHA" ]] || {
    echo "Gadget 微信主程序规范化摘要不匹配: $MAIN_SHA" >&2
    exit 7
  }
else
  [[ "$TEAM" == "$EXPECTED_TEAM" ]] || { echo "签名团队不匹配: $TEAM" >&2; exit 3; }
  codesign --verify --deep --strict "$APP" >/dev/null 2>&1 || {
    echo "微信官方签名完整性校验失败" >&2
    exit 4
  }
  MAIN_SHA=$(shasum -a 256 "$EXE" | awk '{print $1}')
  [[ "$MAIN_SHA" == "$EXPECTED_MAIN_SHA" ]] || { echo "微信主程序摘要不匹配" >&2; exit 5; }
fi

lipo "$CORE" -thin arm64 -output "$TMP/wechat-arm64.dylib"
if [[ "$SIGNATURE_MODE" == "frida-gadget" ]]; then
  CORE_SHA=$(shasum -a 256 "$TMP/wechat-arm64.dylib" | awk '{print $1}')
  [[ "$CORE_SHA" == "$EXPECTED_CORE_GADGET_SHA" ]] || {
    echo "wechat.dylib ARM64 Gadget 摘要不匹配" >&2
    exit 8
  }
else
  CORE_SHA=$(shasum -a 256 "$TMP/wechat-arm64.dylib" | awk '{print $1}')
  [[ "$CORE_SHA" == "$EXPECTED_CORE_SHA" ]] || {
    echo "wechat.dylib ARM64 摘要不匹配" >&2
    exit 8
  }
fi
/usr/bin/python3 - "$CONF" "$TMP/wechat-arm64.dylib" "$EXPECTED_MODULE_SIZE" <<'PY'
import json, pathlib, sys
conf=json.load(open(sys.argv[1]))
binary=pathlib.Path(sys.argv[2]).read_bytes()
module_size=int(sys.argv[3])
required=('sendFuncAddr','req2bufEnterAddr','req2bufExitAddr','buf2RespAddr','blrX8Addr','autoBufferWriteFunc','uploadImageAddr')
for key in required:
    raw=conf.get(key)
    if not isinstance(raw,str): raise SystemExit(f'缺少地址配置: {key}')
    offset=int(raw,0)
    if offset < 0 or offset + 4 > min(len(binary),module_size): raise SystemExit(f'地址越界: {key}={raw}')
    word=binary[offset:offset+4]
    if word in (b'\x00'*4,b'\xff'*4): raise SystemExit(f'地址不是有效 ARM64 指令: {key}={raw}')
PY
if [[ "$QUIET" != "--quiet" ]]; then
  echo "target_ok app=$APP bundle=$BUNDLE version=$VERSION build=$BUILD signature_mode=$SIGNATURE_MODE team=${TEAM:-adhoc}"
  echo "signature_ok main_sha256=$MAIN_SHA"
  echo "core_ok arm64_sha256=$CORE_SHA module_size=$EXPECTED_MODULE_SIZE conf=$CONF"
fi
