-- doom_games: every game with a verdict on whether it counts. Then the leaderboard with a 95% Wilson
-- interval, and the per-seed outcome view behind any-clear.

CREATE OR REPLACE VIEW `{catalog}`.`{schema}`.doom_games
COMMENT 'doom_sessions plus status and counts_as_attempt. Use this, not doom_sessions, for any rate. A game counts as an attempt when it ended as cleared, died or timeout and, for a machine player, no more than half its decisions failed or timed out'
AS
SELECT
  s.*,
  CASE
    WHEN outcome IS NOT NULL THEN outcome
    WHEN started_at < current_timestamp() - INTERVAL 1 HOUR THEN 'lost'
    ELSE 'open'
  END AS status,
  try_divide(coalesce(decision_errors, 0) + coalesce(decision_timeouts, 0),
             greatest(coalesce(decisions, 0), 1)) AS decision_failure_rate,
  coalesce(outcome IN ('cleared', 'died', 'timeout'), false)
    AND (player_type = 'human'
         OR try_divide(coalesce(decision_errors, 0) + coalesce(decision_timeouts, 0),
                       greatest(coalesce(decisions, 0), 1)) <= 0.5) AS counts_as_attempt
FROM `{catalog}`.`{schema}`.doom_sessions s;

CREATE OR REPLACE VIEW `{catalog}`.`{schema}`.doom_leaderboard
COMMENT 'p(doom) per player type, level and mode with a 95% Wilson interval. Filter engine = wasm. Always show attempts beside p_doom. Games that were stopped, abandoned, lost, crashed or played by a mostly failing player are not attempts; they are counted in excluded_games'
AS
WITH base AS (
  SELECT
    engine, mode, level, player_type,
    COUNT_IF(counts_as_attempt) AS attempts,
    COUNT_IF(counts_as_attempt AND outcome = 'died') AS deaths,
    COUNT_IF(counts_as_attempt AND outcome = 'cleared') AS clears,
    COUNT_IF(status <> 'open' AND NOT counts_as_attempt) AS excluded_games,
    AVG(CASE WHEN counts_as_attempt THEN duration_s END) AS mean_duration_s
  FROM `{catalog}`.`{schema}`.doom_games
  GROUP BY engine, mode, level, player_type
), rate AS (
  SELECT *, try_divide(deaths, attempts) AS p_doom FROM base
)
SELECT
  engine, mode, level, player_type, attempts, deaths, clears, excluded_games, p_doom,
  mean_duration_s,
  (p_doom + 1.9208 / attempts
    - 1.96 * sqrt(p_doom * (1 - p_doom) / attempts + 0.9604 / (attempts * attempts)))
    / (1 + 3.8416 / attempts) AS p_doom_low,
  (p_doom + 1.9208 / attempts
    + 1.96 * sqrt(p_doom * (1 - p_doom) / attempts + 0.9604 / (attempts * attempts)))
    / (1 + 3.8416 / attempts) AS p_doom_high
FROM rate;

CREATE OR REPLACE VIEW `{catalog}`.`{schema}`.doom_seed_outcomes
COMMENT 'One row per engine, level, mode and seed: did at least one machine player clear it. Humans are excluded, and so are games that are not attempts. any_clear_rate = AVG(cleared_any)'
AS
SELECT
  engine, level, mode, seed,
  COUNT(*) AS machine_attempts,
  COUNT_IF(outcome = 'cleared') AS machine_clears,
  MAX(CASE WHEN outcome = 'cleared' THEN 1 ELSE 0 END) AS cleared_any
FROM `{catalog}`.`{schema}`.doom_games
WHERE player_type <> 'human' AND counts_as_attempt
GROUP BY engine, level, mode, seed;
