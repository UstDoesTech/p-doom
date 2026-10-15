-- Governed p(doom) measures for Genie and dashboards. Needs a serverless warehouse
-- (metric view YAML version 1.1).

CREATE OR REPLACE VIEW `{catalog}`.`{schema}`.doom_metrics
WITH METRICS
LANGUAGE YAML
AS $$
  version: 1.1
  source: "`{catalog}`.`{schema}`.doom_sessions"
  comment: "p(doom) measures over Doom attempts. p_doom is deaths divided by attempts. Show attempts beside every rate, and never compare paused with realtime mode."
  dimensions:
    - name: player_type
      expr: player_type
      comment: "Who played the attempt"
    - name: mode
      expr: mode
      comment: "paused or realtime"
    - name: engine
      expr: engine
      comment: "wasm is real Doom. mock is the pipeline test arena"
    - name: level
      expr: level
    - name: seed
      expr: seed
    - name: model_version
      expr: model_version
    - name: day
      expr: "DATE_TRUNC('DAY', started_at)"
  measures:
    - name: attempts
      expr: COUNT(1) FILTER (WHERE outcome NOT IN ('aborted', 'abandoned'))
      comment: "Games played to an end: cleared, died or timeout. Aborted and abandoned games are not attempts"
    - name: games_started
      expr: COUNT(1)
      comment: "Every game that wrote a session row, including aborted and abandoned ones"
    - name: aborted
      expr: COUNT(1) FILTER (WHERE outcome = 'aborted')
      comment: "Games the player stopped with the Stop button"
    - name: abandoned
      expr: COUNT(1) FILTER (WHERE outcome = 'abandoned')
      comment: "Games left when the page closed"
    - name: abandonment_rate
      expr: "try_divide(MEASURE(aborted) + MEASURE(abandoned), MEASURE(games_started))"
      comment: "Share of started games that were stopped or abandoned"
    - name: deaths
      expr: COUNT(1) FILTER (WHERE outcome = 'died')
    - name: clears
      expr: COUNT(1) FILTER (WHERE outcome = 'cleared')
    - name: timeouts
      expr: COUNT(1) FILTER (WHERE outcome = 'timeout')
    - name: p_doom
      expr: "try_divide(MEASURE(deaths), MEASURE(attempts))"
      comment: "Probability of dying before the exit"
    - name: clear_rate
      expr: "try_divide(MEASURE(clears), MEASURE(attempts))"
    - name: mean_duration_s
      expr: AVG(duration_s) FILTER (WHERE outcome NOT IN ('aborted', 'abandoned'))
    - name: mean_kills
      expr: AVG(kills) FILTER (WHERE outcome NOT IN ('aborted', 'abandoned'))
$$;
