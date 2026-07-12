#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT_DIR/src/WeChatSecondHook.m"
OUT_DIR="$ROOT_DIR/build"
OUT="$OUT_DIR/WeChatSecondHook.dylib"
mkdir -p "$OUT_DIR"
ARCH_FLAGS=()
case "$(uname -m)" in
  arm64) ARCH_FLAGS=(-arch arm64) ;;
  x86_64) ARCH_FLAGS=(-arch x86_64) ;;
  *) ARCH_FLAGS=() ;;
esac
clang "${ARCH_FLAGS[@]}" -dynamiclib -fobjc-arc -O2 \
  -framework Foundation \
  -Wno-deprecated-declarations \
  -o "$OUT" "$SRC"
codesign --force --sign - "$OUT" >/dev/null
printf 'Built: %s\n' "$OUT"
