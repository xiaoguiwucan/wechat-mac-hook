#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP="${1:?用法: scripts/verify_memory_backup.sh BACKUP_DIR}"
cd "$BACKUP"
shasum -a 256 -c SHA256SUMS
shasum -a 256 -c MINIO_SHA256SUMS
sqlite3 edge-memory.sqlite "PRAGMA integrity_check;" | grep -qx ok
if command -v pg_restore >/dev/null 2>&1; then
  pg_restore --list postgres.dump >/dev/null
else
  set -a
  source "$ROOT/infrastructure/.env"
  set +a
  docker compose --env-file "$ROOT/infrastructure/.env" \
    -f "$ROOT/infrastructure/docker-compose.yml" exec -T postgres \
    pg_restore --list < postgres.dump >/dev/null
fi
python3 - minio-manifest.json minio-objects <<'PY'
import json
import sys
from pathlib import Path
manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
root = Path(sys.argv[2])
missing = [row["object"] for row in manifest if not (root / row["object"]).is_file()]
if missing:
    raise SystemExit(f"缺少 {len(missing)} 个 MinIO 对象")
print(f"backup_verified objects={len(manifest)}")
PY
