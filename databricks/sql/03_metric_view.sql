-- Governed p(doom) measures for Genie and dashboards. Needs a serverless warehouse
-- (metric view YAML version 1.1).

CREATE OR REPLACE VIEW `{catalog}`.`{schema}`.doom_metrics
WITH METRICS
LANGUAGE YAML
AS $$
  version: 1.1
  source: "`{catalog}`.`{schema}`.doom_games"
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
      expr: COUNT(1) FILTER (WHERE counts_as_attempt)
      comment: "Games that ended as cleared, died or timeout and were not played by a mostly failing player"
    - name: deaths
      expr: COUNT(1) FILTER (WHERE counts_as_attempt AND outcome = 'died')
    - name: clears
      expr: COUNT(1) FILTER (WHERE counts_as_attempt AND outcome = 'cleared')
    - name: timeouts
      expr: COUNT(1) FILTER (WHERE counts_as_attempt AND outcome = 'timeout')
    - name: p_doom
      expr: "try_divide(MEASURE(deaths), MEASURE(attempts))"
      comment: "Probability of dying before the exit"
    - name: clear_rate
      expr: "try_divide(MEASURE(clears), MEASURE(attempts))"
    - name: mean_duration_s
      expr: AVG(duration_s) FILTER (WHERE counts_as_attempt)
    - name: mean_kills
      expr: AVG(kills) FILTER (WHERE counts_as_attempt)
    - name: games_started
      expr: COUNT(1)
      comment: "Every game that wrote a session row, including ones that are not attempts"
    - name: aborted
      expr: COUNT(1) FILTER (WHERE status = 'aborted')
      comment: "Games the player stopped with the Stop button"
    - name: abandoned
      expr: COUNT(1) FILTER (WHERE status = 'abandoned')
      comment: "Games left when the page closed, or that went idle"
    - name: lost
      expr: COUNT(1) FILTER (WHERE status = 'lost')
      comment: "Games that never ended, usually because the app restarted mid-game"
    - name: crashed
      expr: COUNT(1) FILTER (WHERE status = 'error')
      comment: "Games where the browser game loop crashed"
    - name: unreliable
      expr: COUNT(1) FILTER (WHERE status IN ('cleared', 'died', 'timeout') AND NOT counts_as_attempt)
      comment: "Games that ended normally but whose player failed more than half its decisions"
    - name: abandonment_rate
      expr: "try_divide(MEASURE(aborted) + MEASURE(abandoned) + MEASURE(lost), MEASURE(games_started))"
      comment: "Share of started games that were stopped, abandoned or lost"
    - name: decision_failure_rate
      expr: AVG(decision_failure_rate) FILTER (WHERE player_type <> 'human' AND status <> 'open')
      comment: "Average share of decisions that failed or timed out, per game"
$$;
