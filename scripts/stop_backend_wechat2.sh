#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
"$ROOT_DIR/scripts/stop_ai_reply.sh" || true
"$ROOT_DIR/scripts/stop_onebot_wechat2.sh" || true
echo "Stopped backend services (AI + OneBot). WeChat2 is left running."
