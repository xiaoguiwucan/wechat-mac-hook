#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ONEBOT_DIR="$ROOT_DIR/vendor/wechat_chatter/onebot"
OUT="$ROOT_DIR/bin/onebot"
GO_BIN="${GO_BIN:-$(command -v go || true)}"
if [[ -z "$GO_BIN" ]]; then
  echo "未找到 Go。请安装 Go 后重试，或把预编译 onebot 放到：$OUT" >&2
  exit 1
fi
(
  cd "$ONEBOT_DIR"
  GOOS=darwin GOARCH=arm64 "$GO_BIN" build -o "$OUT" .
)
echo "Built: $OUT"
