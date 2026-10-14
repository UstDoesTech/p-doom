-- Leaderboard with a 95% Wilson interval, and the per-seed outcome view behind any-clear.

CREATE OR REPLACE VIEW `{catalog}`.`{schema}`.doom_leaderboard
COMMENT 'p(doom) per player type, level and mode with a 95% Wilson interval. Filter engine = wasm. Always show attempts beside p_doom'
AS
WITH base AS (
  SELECT
    engine, mode, level, player_type,
    COUNT(*) AS attempts,
    COUNT_IF(outcome = 'died') AS deaths,
    COUNT_IF(outcome = 'cleared') AS clears,
    AVG(duration_s) AS mean_duration_s
  FROM `{catalog}`.`{schema}`.doom_sessions
  GROUP BY engine, mode, level, player_type
), rate AS (
  SELECT *, deaths / attempts AS p_doom FROM base
)
SELECT
  engine, mode, level, player_type, attempts, deaths, clears, p_doom, mean_duration_s,
  (p_doom + 1.9208 / attempts
    - 1.96 * sqrt(p_doom * (1 - p_doom) / attempts + 0.9604 / (attempts * attempts)))
    / (1 + 3.8416 / attempts) AS p_doom_low,
  (p_doom + 1.9208 / attempts
    + 1.96 * sqrt(p_doom * (1 - p_doom) / attempts + 0.9604 / (attempts * attempts)))
    / (1 + 3.8416 / attempts) AS p_doom_high
FROM rate;

CREATE OR REPLACE VIEW `{catalog}`.`{schema}`.doom_seed_outcomes
COMMENT 'One row per engine, level, mode and seed: did at least one machine player clear it. Humans are excluded. any_clear_rate = AVG(cleared_any)'
AS
SELECT
  engine, level, mode, seed,
  COUNT(*) AS machine_attempts,
  COUNT_IF(outcome = 'cleared') AS machine_clears,
  MAX(CASE WHEN outcome = 'cleared' THEN 1 ELSE 0 END) AS cleared_any
FROM `{catalog}`.`{schema}`.doom_sessions
WHERE player_type <> 'human'
GROUP BY engine, level, mode, seed;
