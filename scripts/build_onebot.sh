#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SOURCE_DIR="$ROOT_DIR/vendor/wechat_chatter/onebot"
RUNTIME_DIR="$ROOT_DIR/tools/onebot/onebot"
OUT="$RUNTIME_DIR/onebot"
FRIDA_VERSION="${FRIDA_VERSION:-17.8.0}"
ARCH="${ONEBOT_ARCH:-$(uname -m)}"
DEVKIT_DIR="$ROOT_DIR/vendor/wechat_chatter/frida-devkit"

GO_BIN="${GO_BIN:-$(command -v go || true)}"
if [[ -z "$GO_BIN" && -x "$HOME/.local/go1.25/bin/go" ]]; then
  GO_BIN="$HOME/.local/go1.25/bin/go"
fi
if [[ -z "$GO_BIN" ]]; then
  GO_BIN=$(find "$HOME/go/pkg/mod/golang.org" -type f -path '*/bin/go' -perm -111 2>/dev/null | sort -V | tail -1 || true)
fi
if [[ -z "$GO_BIN" || ! -x "$GO_BIN" ]]; then
  echo "未找到 Go 1.25 运行时" >&2
  exit 1
fi

if [[ ! -f "$DEVKIT_DIR/libfrida-core.a" || ! -f "$DEVKIT_DIR/frida-core.h" ]]; then
  NAME="frida-core-devkit-${FRIDA_VERSION}-macos-${ARCH}"
  URL="https://github.com/frida/frida/releases/download/${FRIDA_VERSION}/${NAME}.tar.xz"
  mkdir -p "$DEVKIT_DIR"
  TMP=$(mktemp -d)
  trap 'rm -rf "$TMP"' EXIT
  echo "Downloading $NAME"
  curl -fL "$URL" -o "$TMP/devkit.tar.xz"
  tar -xf "$TMP/devkit.tar.xz" -C "$DEVKIT_DIR"
fi

mkdir -p "$RUNTIME_DIR"
cp "$SOURCE_DIR/script.js" "$RUNTIME_DIR/script.js"
(
  cd "$SOURCE_DIR"
  CGO_ENABLED=1 GOOS=darwin GOARCH="$ARCH" \
    CGO_CFLAGS="-I$DEVKIT_DIR" \
    CGO_LDFLAGS="-L$DEVKIT_DIR -lfrida-core" \
    "$GO_BIN" build -trimpath -o "$OUT" .
)
codesign --force --sign - --timestamp=none "$OUT" >/dev/null
echo "Built: $OUT"
