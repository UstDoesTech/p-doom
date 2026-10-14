-- p(doom) telemetry tables. Placeholders {catalog} and {schema} are filled in by
-- `python -m pdoom.setup_db`. Keep one column per line: a test compares these columns
-- with backend/src/pdoom/telemetry.py.

CREATE SCHEMA IF NOT EXISTS `{catalog}`.`{schema}`;

CREATE TABLE IF NOT EXISTS `{catalog}`.`{schema}`.doom_sessions (
  session_id STRING COMMENT 'Unique id of one attempt at a level',
  player_type STRING COMMENT 'Who played: human, rule_bot, ai_decide, vision_llm, tool_agent, rl_policy, bc_policy or supervisor',
  mode STRING COMMENT 'paused (game waits for each decision) or realtime (last intent is held while the model thinks). Never compare the two',
  engine STRING COMMENT 'wasm is the real Doom build. mock is the test arena used to check the pipeline; exclude it from results',
  level STRING COMMENT 'Level played, such as E1M1',
  seed BIGINT COMMENT 'Random-number seed for the attempt. Every machine player plays the same seeds',
  model_version STRING COMMENT 'Pinned model or prompt version. A change starts a new version',
  started_at TIMESTAMP COMMENT 'When the attempt started (UTC)',
  ended_at TIMESTAMP COMMENT 'When the attempt ended (UTC)',
  outcome STRING COMMENT 'cleared (reached the exit), died, or timeout',
  duration_s DOUBLE COMMENT 'Seconds of play',
  kills INT COMMENT 'Enemies killed'
)
COMMENT 'One row per attempt at a level, for every player type';

CREATE TABLE IF NOT EXISTS `{catalog}`.`{schema}`.doom_events (
  session_id STRING COMMENT 'Joins to doom_sessions.session_id',
  tic INT COMMENT 'Game tic (35 per second)',
  ts TIMESTAMP COMMENT 'When the sample was taken (UTC)',
  health INT COMMENT 'Player health, 0 to 200',
  armor INT COMMENT 'Player armor',
  weapon STRING COMMENT 'Weapon in hand',
  ammo INT COMMENT 'Total ammo across all ammo types',
  x DOUBLE COMMENT 'Map x position',
  y DOUBLE COMMENT 'Map y position',
  angle DOUBLE COMMENT 'Facing angle in degrees',
  action STRING COMMENT 'Intent or key being held at the time. Human sessions fill this in and train bc_policy'
)
COMMENT 'Sampled game state over time, one row per sampled tic';

CREATE TABLE IF NOT EXISTS `{catalog}`.`{schema}`.doom_decisions (
  session_id STRING COMMENT 'Joins to doom_sessions.session_id',
  tic INT COMMENT 'Game tic the decision was made on',
  ts TIMESTAMP COMMENT 'When the decision came back (UTC)',
  player_type STRING COMMENT 'Which machine player decided',
  intent STRING COMMENT 'Chosen action. Null when the player failed and the previous intent was held',
  confidence DOUBLE COMMENT 'ai_decide confidence in the chosen intent, 0 to 1',
  threat_prob DOUBLE COMMENT 'ai_decide probability that damage is imminent, 0 to 1',
  danger_score DOUBLE COMMENT 'ai_decide danger score from 0 (safe) to 4 (critical)',
  latency_ms INT COMMENT 'Milliseconds from request to decision',
  error STRING COMMENT 'Error text when the decision failed',
  raw_output VARIANT COMMENT 'The full model output as returned'
)
COMMENT 'One row per machine decision. Human sessions have none';
