#!/usr/bin/env bash
set -euo pipefail
TARGET="${1:-}"
TEXT="${2:-第二微信 hook 测试}"
HOST="${ONEBOT_RECEIVE_HOST:-127.0.0.1:58080}"
if [[ -z "$TARGET" ]]; then
  echo "用法：$0 wxid_xxx '测试内容'" >&2
  exit 1
fi
curl -sS -X POST "http://$HOST/send_private_msg" \
  -H 'Content-Type: application/json' \
  -d "{\"user_id\":\"$TARGET\",\"message\":[{\"type\":\"text\",\"data\":{\"text\":$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$TEXT")}}]}" \
  | tee /dev/stderr
