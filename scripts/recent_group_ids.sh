#!/usr/bin/env bash
set -euo pipefail
LOG_FILE="${ONEBOT_LOG:-$HOME/Library/Application Support/WeChatAgent/logs/onebot-wechat.log}"
LIMIT="${1:-20}"
if [[ ! -f "$LOG_FILE" ]]; then
  echo "找不到 OneBot 日志: $LOG_FILE" >&2
  exit 1
fi
python3 - "$LOG_FILE" "$LIMIT" <<'PY'
import collections
import json
import re
import sys
from datetime import datetime

log_file = sys.argv[1]
limit = int(sys.argv[2])
ansi = re.compile(r'\x1b\[[0-9;]*m')
rows = []

def parse_msg(line: str):
    line = ansi.sub('', line.strip())
    m = re.search(r'msg=(".*")$', line)
    if not m:
        return None
    try:
        json_string = json.loads(m.group(1))
        return json.loads(json_string)
    except Exception:
        return None

with open(log_file, 'r', errors='ignore') as f:
    for line in f:
        if '发送数据 msg=' not in line:
            continue
        obj = parse_msg(line)
        if not obj or obj.get('post_type') != 'message' or obj.get('message_type') != 'group':
            continue
        msgsource = obj.get('msgsource') or ''
        mc = ''
        mm = re.search(r'<membercount>(\d+)</membercount>', msgsource)
        if mm:
            mc = mm.group(1)
        text = ''
        for part in obj.get('message') or []:
            if isinstance(part, dict) and part.get('type') == 'text':
                text += str((part.get('data') or {}).get('text') or '')
        sender = obj.get('sender') or {}
        rows.append({
            'time': obj.get('time'),
            'group_id': obj.get('group_id') or '',
            'membercount': mc,
            'user_id': obj.get('user_id') or sender.get('user_id') or '',
            'nickname': sender.get('nickname') or sender.get('card') or '',
            'text': text.replace('\n', ' ')[:80],
        })

print('最近真实群消息（用来确认“值班群”的 group_id）：')
if not rows:
    print('  暂无。请在目标群发一句话后重跑本脚本。')
    raise SystemExit(0)
for r in rows[-limit:]:
    print(f"  group_id={r['group_id']} members={r['membercount'] or '-'} sender={r['nickname'] or r['user_id']} text={r['text']}")

summary = collections.Counter(r['group_id'] for r in rows)
print('\n出现次数汇总：')
for gid, count in summary.most_common():
    last = next(r for r in reversed(rows) if r['group_id'] == gid)
    print(f"  {gid}\tcount={count}\tmembers={last['membercount'] or '-'}\tlast_sender={last['nickname'] or last['user_id']}\tlast_text={last['text']}")
PY
