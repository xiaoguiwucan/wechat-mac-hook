CREATE EXTENSION IF NOT EXISTS pgroonga;
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS chat_events (
  event_id TEXT PRIMARY KEY,
  trace_id TEXT NOT NULL DEFAULT '',
  direction TEXT NOT NULL,
  account_id TEXT NOT NULL DEFAULT 'current-wechat',
  group_id TEXT NOT NULL,
  group_name TEXT NOT NULL DEFAULT '',
  user_id TEXT NOT NULL DEFAULT '',
  sender_name TEXT NOT NULL DEFAULT '',
  message_id TEXT NOT NULL DEFAULT '',
  event_time BIGINT NOT NULL,
  text TEXT NOT NULL DEFAULT '',
  raw_message TEXT NOT NULL DEFAULT '',
  segments JSONB NOT NULL DEFAULT '[]'::jsonb,
  raw_event JSONB NOT NULL DEFAULT '{}'::jsonb,
  source TEXT NOT NULL DEFAULT 'onebot_callback',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (account_id, group_id, direction, message_id, event_time)
);

CREATE INDEX IF NOT EXISTS chat_events_group_time_idx
  ON chat_events(group_id, event_time DESC);
CREATE INDEX IF NOT EXISTS chat_events_user_time_idx
  ON chat_events(group_id, user_id, event_time DESC);
CREATE INDEX IF NOT EXISTS chat_events_pgroonga_idx
  ON chat_events USING pgroonga
  ((coalesce(sender_name, '') || ' ' || coalesce(text, '') || ' ' || coalesce(raw_message, '')));

CREATE TABLE IF NOT EXISTS media_objects (
  id BIGSERIAL PRIMARY KEY,
  event_id TEXT NOT NULL REFERENCES chat_events(event_id) ON DELETE CASCADE,
  group_id TEXT NOT NULL,
  media_type TEXT NOT NULL,
  source_file TEXT NOT NULL DEFAULT '',
  source_url TEXT NOT NULL DEFAULT '',
  object_key TEXT NOT NULL DEFAULT '',
  sha256 TEXT NOT NULL DEFAULT '',
  mime_type TEXT NOT NULL DEFAULT '',
  byte_size BIGINT NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'pending',
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  error TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(event_id, media_type, source_file)
);
DROP INDEX IF EXISTS media_objects_sha256_idx;
CREATE INDEX IF NOT EXISTS media_objects_sha256_idx
  ON media_objects(sha256) WHERE sha256 <> '';

CREATE TABLE IF NOT EXISTS memory_embeddings (
  object_type TEXT NOT NULL,
  object_id TEXT NOT NULL,
  group_id TEXT NOT NULL,
  model TEXT NOT NULL,
  -- pgvector HNSW supports halfvec up to 4000 dimensions. Existing 4096-D
  -- local vectors are truncated only in the derived central index; raw source
  -- text remains authoritative and can always rebuild the index.
  embedding halfvec(4000) NOT NULL,
  source_event_ids TEXT[] NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY(object_type, object_id, model)
);
CREATE INDEX IF NOT EXISTS memory_embeddings_hnsw_idx
  ON memory_embeddings USING hnsw (embedding halfvec_cosine_ops);

CREATE TABLE IF NOT EXISTS memory_facts (
  id BIGSERIAL PRIMARY KEY,
  group_id TEXT NOT NULL,
  user_id TEXT NOT NULL DEFAULT '',
  category TEXT NOT NULL,
  value TEXT NOT NULL,
  confidence REAL NOT NULL DEFAULT 0,
  source_event_ids TEXT[] NOT NULL DEFAULT '{}',
  valid_from TIMESTAMPTZ,
  valid_until TIMESTAMPTZ,
  superseded_by BIGINT REFERENCES memory_facts(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS memory_facts_lookup_idx
  ON memory_facts(group_id, user_id, category, updated_at DESC);

CREATE TABLE IF NOT EXISTS memory_summaries (
  scope_type TEXT NOT NULL,
  scope_id TEXT NOT NULL,
  group_id TEXT NOT NULL,
  summary TEXT NOT NULL,
  source_event_ids TEXT[] NOT NULL DEFAULT '{}',
  source_cursor BIGINT NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY(scope_type, scope_id, group_id)
);

CREATE TABLE IF NOT EXISTS ingest_offsets (
  source TEXT PRIMARY KEY,
  last_event_id TEXT NOT NULL DEFAULT '',
  last_event_time BIGINT NOT NULL DEFAULT 0,
  synced_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS graph_sync_jobs (
  id BIGSERIAL PRIMARY KEY,
  event_id TEXT NOT NULL UNIQUE REFERENCES chat_events(event_id) ON DELETE CASCADE,
  group_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '',
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS automation_runs (
  run_id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  source_event_id TEXT NOT NULL DEFAULT '',
  group_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  intent TEXT NOT NULL,
  risk_level TEXT NOT NULL,
  status TEXT NOT NULL,
  hermes_run_id TEXT NOT NULL DEFAULT '',
  result_summary TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS automation_events (
  id BIGSERIAL PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES automation_runs(run_id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS permission_bindings (
  group_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('member', 'admin', 'owner')),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY(group_id, user_id)
);
