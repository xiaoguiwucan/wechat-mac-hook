#!/bin/zsh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
set -a
[[ -f "$ROOT/config/ai_reply.env" ]] && source "$ROOT/config/ai_reply.env"
set +a
PYTHON="${WECHAT_AGENT_PYTHON:-$(find "$HOME/.local/share/uv/python" -type f -path '*/bin/python3.*' ! -name '*-config' -perm -111 2>/dev/null | sort -V | tail -n 1)}"
exec "$PYTHON" "$ROOT/ai_reply/ai_reply_server.py" \
  --config "$ROOT/config/ai_reply_config.json"
