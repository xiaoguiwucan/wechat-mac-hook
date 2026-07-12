#!/usr/bin/env bash
set -euo pipefail
GROUP_ID="${1:-1234567890@chatroom}"
TEXT="${2:-测试AI回调，不应发群；无API Key时只记录日志}"
curl -sS -X POST 'http://127.0.0.1:36060/onebot' \
  -H 'Content-Type: application/json' \
  -d "{\"post_type\":\"message\",\"message_type\":\"group\",\"group_id\":\"$GROUP_ID\",\"self_id\":\"wxid_uyhenr8zit8y12\",\"user_id\":\"$GROUP_ID\",\"sender\":{\"user_id\":\"$GROUP_ID\",\"nickname\":\"测试者\"},\"time\":$(date +%s),\"message_id\":\"test-$(date +%s)\",\"message\":[{\"type\":\"text\",\"data\":{\"text\":\"$TEXT\"}}],\"raw_message\":\"测试者:\\\n$TEXT\"}"
echo
