BEGIN;

CREATE TABLE IF NOT EXISTS dataset_requests (
  id uuid PRIMARY KEY,
  query text NOT NULL CHECK (length(btrim(query)) > 0),
  filters jsonb NOT NULL DEFAULT '{}'::jsonb,
  uploads jsonb NOT NULL DEFAULT '[]'::jsonb,
  request_payload jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS dataset_requests_created_at_idx
  ON dataset_requests (created_at DESC);

CREATE TABLE IF NOT EXISTS dataset_request_files (
  id uuid PRIMARY KEY,
  request_id uuid NOT NULL REFERENCES dataset_requests(id) ON DELETE CASCADE,
  position integer NOT NULL CHECK (position >= 0),
  file_name text NOT NULL,
  media_type text NOT NULL,
  byte_size bigint NOT NULL CHECK (byte_size >= 0),
  last_modified_ms bigint,
  content bytea NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (request_id, position)
);

CREATE INDEX IF NOT EXISTS dataset_request_files_request_id_idx
  ON dataset_request_files (request_id);

COMMIT;
