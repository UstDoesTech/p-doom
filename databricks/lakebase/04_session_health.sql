-- Existing databases: the columns added to doom_sessions after the first release. The app now also
-- writes the session row when a game opens (outcome null) and fills it in when the game ends.
ALTER TABLE pdoom.doom_sessions ADD COLUMN IF NOT EXISTS decisions integer;
ALTER TABLE pdoom.doom_sessions ADD COLUMN IF NOT EXISTS decision_errors integer;
ALTER TABLE pdoom.doom_sessions ADD COLUMN IF NOT EXISTS decision_timeouts integer;
COMMENT ON COLUMN pdoom.doom_sessions.decisions IS 'Decisions the server answered for this game';
COMMENT ON COLUMN pdoom.doom_sessions.decision_errors IS 'Decisions that failed; the previous move was held';
COMMENT ON COLUMN pdoom.doom_sessions.decision_timeouts IS 'Decision calls the browser gave up waiting for';
