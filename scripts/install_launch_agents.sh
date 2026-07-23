#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ASCII_ROOT="$HOME/Projects/wechat-agent-advanced"
mkdir -p "$HOME/Projects"
ln -sfn "$ROOT" "$ASCII_ROOT"
AGENTS="$HOME/Library/LaunchAgents"
LOGS="$HOME/Library/Application Support/WeChatAgent/logs"
BIN="$HOME/Library/Application Support/WeChatAgent/bin"
mkdir -p "$AGENTS" "$LOGS" "$BIN"

write_plist() {
  local label="$1" runner="$2"
  local wrapper="$BIN/$label"
  if [[ "$runner" == "run_ai_reply_foreground.sh" ]]; then
    cat > "$wrapper" <<'EOF'
#!/bin/zsh
set -euo pipefail
SERVER=$(/usr/bin/find "$HOME/Documents/cursor" -maxdepth 4 -type f -path '*/ai_reply/ai_reply_server.py' -print | /usr/bin/head -n 1)
ROOT="${SERVER%/ai_reply/ai_reply_server.py}"
set -a
source "$ROOT/config/ai_reply.env"
set +a
PYTHON=$(/usr/bin/find "$HOME/.local/share/uv/python" -type f -path '*/bin/python3.*' ! -name '*-config' -perm -111 2>/dev/null | /usr/bin/sort -V | /usr/bin/tail -n 1)
exec "$PYTHON" "$SERVER" --config "$ROOT/config/ai_reply_config.json"
EOF
  else
    cat > "$wrapper" <<'EOF'
#!/bin/zsh
set -euo pipefail
SERVER=$(/usr/bin/find "$HOME/Documents/cursor" -maxdepth 3 -type f -path '*/web_admin/server.py' -print | /usr/bin/head -n 1)
ROOT="${SERVER%/web_admin/server.py}"
set -a
source "$ROOT/config/ai_reply.env"
[[ -f "$ROOT/infrastructure/.env" ]] && source "$ROOT/infrastructure/.env"
set +a
PYTHON=$(/usr/bin/find "$HOME/.local/share/uv/python" -type f -path '*/bin/python3.*' ! -name '*-config' -perm -111 2>/dev/null | /usr/bin/sort -V | /usr/bin/tail -n 1)
exec "$PYTHON" "$SERVER" --port "${WEB_ADMIN_PORT:-8765}"
EOF
  fi
  chmod 700 "$wrapper"
  cat > "$AGENTS/$label.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array>
    <string>$wrapper</string>
  </array>
  <key>WorkingDirectory</key><string>$HOME</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$LOGS/$label.stdout.log</string>
  <key>StandardErrorPath</key><string>$LOGS/$label.stderr.log</string>
</dict></plist>
EOF
  plutil -lint "$AGENTS/$label.plist" >/dev/null
}

write_plist ai.wechat-agent.reply run_ai_reply_foreground.sh
write_plist ai.wechat-agent.web run_web_admin_foreground.sh

for label in ai.wechat-agent.reply ai.wechat-agent.web; do
  launchctl bootout "gui/$UID/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$UID" "$AGENTS/$label.plist"
  launchctl enable "gui/$UID/$label"
done
for _ in {1..40}; do
  if /usr/bin/curl -fsS --max-time 1 http://127.0.0.1:36060/health >/dev/null 2>&1 \
    && /usr/bin/curl -fsS --max-time 1 http://127.0.0.1:8765/api/status >/dev/null 2>&1; then
    echo "launch_agents_installed ai.wechat-agent.reply ai.wechat-agent.web"
    exit 0
  fi
  sleep 0.25
done
for label in ai.wechat-agent.reply ai.wechat-agent.web; do
  launchctl bootout "gui/$UID/$label" 2>/dev/null || true
  /usr/bin/python3 - "$AGENTS/$label.plist" <<'PY'
from pathlib import Path
import sys
Path(sys.argv[1]).unlink(missing_ok=True)
PY
done
echo "LaunchAgent 未启用：macOS 拒绝后台进程读取 Documents。服务已回滚，请先授予相应后台进程文件访问权限。" >&2
exit 3
