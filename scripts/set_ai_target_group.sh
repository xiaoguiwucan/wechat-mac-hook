#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="${AI_REPLY_CONFIG:-$ROOT_DIR/config/ai_reply_config.json}"
if [[ $# -lt 1 ]]; then
  cat >&2 <<EOF
用法: $0 <group_id> [群名]
示例: $0 1234567890@chatroom 值班群

提示：不知道 group_id 时先运行：
  $ROOT_DIR/scripts/recent_group_ids.sh
EOF
  exit 1
fi
GROUP_ID="$1"
GROUP_NAME="${2:-值班群}"
python3 - "$CONFIG" "$GROUP_ID" "$GROUP_NAME" <<'PY'
import json
import sys
from pathlib import Path
path = Path(sys.argv[1])
gid = sys.argv[2]
name = sys.argv[3]
raw = json.loads(path.read_text(encoding='utf-8'))
raw['target_groups'] = [{'name': name, 'id': gid}]
path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(f'已设置 AI 回复目标群：{name} => {gid}')
PY
# 只重启 AI 回复服务，不重启/不杀任何微信进程。
if [[ -f "$HOME/Library/Application Support/WeChatAgent/ai-reply.pid" ]]; then
  "$ROOT_DIR/scripts/stop_ai_reply.sh" || true
fi
"$ROOT_DIR/scripts/start_ai_reply.sh"
