-- p(doom) telemetry tables in Lakebase Postgres. Same columns as databricks/sql/01_tables.sql
-- (Unity Catalog); the types map as STRING->text, BIGINT->bigint, INT->integer, DOUBLE->double
-- precision, TIMESTAMP->timestamptz, VARIANT->jsonb. Applied to the pdoom database, schema pdoom.

CREATE SCHEMA IF NOT EXISTS pdoom;

CREATE TABLE IF NOT EXISTS pdoom.doom_sessions (
  session_id text PRIMARY KEY,
  player_type text NOT NULL,
  mode text,
  engine text,
  level text,
  seed bigint,
  model_version text,
  started_at timestamptz,
  ended_at timestamptz,
  outcome text,
  duration_s double precision,
  kills integer
);
COMMENT ON TABLE pdoom.doom_sessions IS 'One row per attempt at a level, for every player type';
COMMENT ON COLUMN pdoom.doom_sessions.mode IS 'paused or realtime. Never compare the two';
COMMENT ON COLUMN pdoom.doom_sessions.engine IS 'wasm is the real Doom build. mock is the test arena; exclude it from results';
COMMENT ON COLUMN pdoom.doom_sessions.outcome IS 'cleared, died, timeout, aborted (Stop pressed) or abandoned (page closed). The last two are not attempts';

CREATE TABLE IF NOT EXISTS pdoom.doom_events (
  session_id text NOT NULL REFERENCES pdoom.doom_sessions (session_id) ON DELETE CASCADE,
  tic integer NOT NULL,
  ts timestamptz,
  health integer,
  armor integer,
  weapon text,
  ammo integer,
  x double precision,
  y double precision,
  angle double precision,
  action text,
  PRIMARY KEY (session_id, tic)
);
COMMENT ON TABLE pdoom.doom_events IS 'Sampled game state over time, one row per sampled tic';

CREATE TABLE IF NOT EXISTS pdoom.doom_decisions (
  session_id text NOT NULL REFERENCES pdoom.doom_sessions (session_id) ON DELETE CASCADE,
  tic integer NOT NULL,
  ts timestamptz,
  player_type text,
  intent text,
  confidence double precision,
  threat_prob double precision,
  danger_score double precision,
  latency_ms integer,
  error text,
  raw_output jsonb,
  PRIMARY KEY (session_id, tic)
);
COMMENT ON TABLE pdoom.doom_decisions IS 'One row per machine decision. Human sessions have none';

CREATE INDEX IF NOT EXISTS doom_sessions_player_idx ON pdoom.doom_sessions (player_type, started_at);
