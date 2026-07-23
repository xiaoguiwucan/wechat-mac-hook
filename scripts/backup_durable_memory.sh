#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
set -a
source config/ai_reply.env
source infrastructure/.env
set +a

STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="${1:-$ROOT/backups/$STAMP}"
mkdir -p "$DEST"

EDGE_DB="${WECHAT_EDGE_DB:-$HOME/Library/Application Support/WeChatAgent/memory/wechat-memory.sqlite3}"
sqlite3 "$EDGE_DB" ".backup '$DEST/edge-memory.sqlite'"
docker compose --env-file infrastructure/.env -f infrastructure/docker-compose.yml \
  exec -T postgres pg_dump -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -Fc \
  > "$DEST/postgres.dump"

PYTHONPATH=tools/runtime/python python3 - "$DEST" <<'PY'
import json
import os
import sys
from pathlib import Path
from minio import Minio

dest = Path(sys.argv[1])
client = Minio(
    os.environ["WECHAT_MINIO_ENDPOINT"],
    access_key=os.environ["WECHAT_MINIO_ACCESS_KEY"],
    secret_key=os.environ["WECHAT_MINIO_SECRET_KEY"],
    secure=os.getenv("WECHAT_MINIO_SECURE", "0") == "1",
)
bucket = os.environ["WECHAT_MINIO_BUCKET"]
manifest = [
    {"object": obj.object_name, "size": obj.size, "etag": obj.etag}
    for obj in client.list_objects(bucket, recursive=True)
]
(dest / "minio-objects").mkdir(parents=True, exist_ok=True)
for item in manifest:
    target = dest / "minio-objects" / item["object"]
    target.parent.mkdir(parents=True, exist_ok=True)
    client.fget_object(bucket, item["object"], str(target))
(dest / "minio-manifest.json").write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
PY

(cd "$DEST" && find minio-objects -type f -exec shasum -a 256 {} + | sort) \
  > "$DEST/MINIO_SHA256SUMS"
shasum -a 256 "$DEST/edge-memory.sqlite" "$DEST/postgres.dump" \
  "$DEST/minio-manifest.json" "$DEST/MINIO_SHA256SUMS" > "$DEST/SHA256SUMS"
chmod -R go-rwx "$DEST"
echo "$DEST"
