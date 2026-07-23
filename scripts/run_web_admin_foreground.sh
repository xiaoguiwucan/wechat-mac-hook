#!/bin/zsh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
set -a
[[ -f "$ROOT/config/ai_reply.env" ]] && source "$ROOT/config/ai_reply.env"
[[ -f "$ROOT/infrastructure/.env" ]] && source "$ROOT/infrastructure/.env"
set +a
PYTHON="${WECHAT_AGENT_PYTHON:-$(find "$HOME/.local/share/uv/python" -type f -path '*/bin/python3.*' ! -name '*-config' -perm -111 2>/dev/null | sort -V | tail -n 1)}"
exec "$PYTHON" "$ROOT/web_admin/server.py" --port "${WEB_ADMIN_PORT:-8765}"
