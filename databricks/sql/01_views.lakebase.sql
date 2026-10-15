-- Unity Catalog views over the Lakebase telemetry tables, for Genie, the leaderboard and the metric
-- view. {lakebase_catalog} is the catalog the Lakebase database is registered as
-- (databricks postgres create-catalog). Generated from 01_tables.uc.sql comments. Used instead of
-- the UC tables when setup_db is given --lakebase-catalog.

CREATE OR REPLACE VIEW `{catalog}`.`{schema}`.doom_sessions (
  session_id COMMENT 'Unique id of one attempt at a level',
  player_type COMMENT 'Who played: human, rule_bot, ai_decide, vision_llm, tool_agent, rl_policy, bc_policy or supervisor',
  mode COMMENT 'paused (game waits for each decision) or realtime (last intent is held while the model thinks). Never compare the two',
  engine COMMENT 'wasm is the real Doom build. mock is the test arena used to check the pipeline; exclude it from results',
  level COMMENT 'Level played, such as E1M1',
  seed COMMENT 'Random-number seed for the attempt. Every machine player plays the same seeds',
  model_version COMMENT 'Pinned model or prompt version. A change starts a new version',
  started_at COMMENT 'When the attempt started (UTC)',
  ended_at COMMENT 'When the attempt ended (UTC)',
  outcome COMMENT 'cleared (reached the exit), died, timeout, aborted (Stop pressed) or abandoned (page closed). The last two are not attempts',
  duration_s COMMENT 'Seconds of play',
  kills COMMENT 'Enemies killed'
)
COMMENT 'One row per attempt at a level, for every player type. Live view of the Lakebase table'
AS SELECT session_id, player_type, mode, engine, level, seed, model_version, started_at, ended_at, outcome, duration_s, kills
FROM `{lakebase_catalog}`.`pdoom`.doom_sessions;

CREATE OR REPLACE VIEW `{catalog}`.`{schema}`.doom_events (
  session_id COMMENT 'Joins to doom_sessions.session_id',
  tic COMMENT 'Game tic (35 per second)',
  ts COMMENT 'When the sample was taken (UTC)',
  health COMMENT 'Player health, 0 to 200',
  armor COMMENT 'Player armor',
  weapon COMMENT 'Weapon in hand',
  ammo COMMENT 'Total ammo across all ammo types',
  x COMMENT 'Map x position',
  y COMMENT 'Map y position',
  angle COMMENT 'Facing angle in degrees',
  action COMMENT 'Intent or key being held at the time. Human sessions fill this in and train bc_policy'
)
COMMENT 'Sampled game state over time, one row per sampled tic. Live view of the Lakebase table'
AS SELECT session_id, tic, ts, health, armor, weapon, ammo, x, y, angle, action
FROM `{lakebase_catalog}`.`pdoom`.doom_events;

CREATE OR REPLACE VIEW `{catalog}`.`{schema}`.doom_decisions (
  session_id COMMENT 'Joins to doom_sessions.session_id',
  tic COMMENT 'Game tic the decision was made on',
  ts COMMENT 'When the decision came back (UTC)',
  player_type COMMENT 'Which machine player decided',
  intent COMMENT 'Chosen action. Null when the player failed and the previous intent was held',
  confidence COMMENT 'ai_decide confidence in the chosen intent, 0 to 1',
  threat_prob COMMENT 'ai_decide probability that damage is imminent, 0 to 1',
  danger_score COMMENT 'ai_decide danger score from 0 (safe) to 4 (critical)',
  latency_ms COMMENT 'Milliseconds from request to decision',
  error COMMENT 'Error text when the decision failed',
  raw_output COMMENT 'The full model output as returned'
)
COMMENT 'One row per machine decision. Human sessions have none. Live view of the Lakebase table'
AS SELECT session_id, tic, ts, player_type, intent, confidence, threat_prob, danger_score, latency_ms, error, raw_output
FROM `{lakebase_catalog}`.`pdoom`.doom_decisions;
