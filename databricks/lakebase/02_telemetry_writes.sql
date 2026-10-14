-- The app writes events and decisions while a session runs and the session row last (and never for
-- an abandoned attempt), so child rows cannot reference a session row. Writes are made idempotent
-- by keys instead: a retried batch hits ON CONFLICT DO NOTHING.
ALTER TABLE pdoom.doom_events DROP CONSTRAINT IF EXISTS doom_events_session_id_fkey;
ALTER TABLE pdoom.doom_decisions DROP CONSTRAINT IF EXISTS doom_decisions_session_id_fkey;
ALTER TABLE pdoom.doom_decisions DROP CONSTRAINT IF EXISTS doom_decisions_pkey;
ALTER TABLE pdoom.doom_decisions ALTER COLUMN ts SET NOT NULL;
ALTER TABLE pdoom.doom_decisions ADD PRIMARY KEY (session_id, tic, ts);
CREATE INDEX IF NOT EXISTS doom_sessions_started_idx ON pdoom.doom_sessions (started_at);
