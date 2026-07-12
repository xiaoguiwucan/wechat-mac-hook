#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
"$ROOT_DIR/scripts/run_wechat2_onebot.sh"
"$ROOT_DIR/scripts/start_ai_reply.sh"
echo "WeChat2 + OneBot + AI reply ready."
