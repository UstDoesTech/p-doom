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
      expr: COUNT(1)
      comment: "Number of attempts"
    - name: deaths
      expr: COUNT(1) FILTER (WHERE outcome = 'died')
    - name: clears
      expr: COUNT(1) FILTER (WHERE outcome = 'cleared')
    - name: timeouts
      expr: COUNT(1) FILTER (WHERE outcome = 'timeout')
    - name: p_doom
      expr: "MEASURE(deaths) / MEASURE(attempts)"
      comment: "Probability of dying before the exit"
    - name: clear_rate
      expr: "MEASURE(clears) / MEASURE(attempts)"
    - name: mean_duration_s
      expr: AVG(duration_s)
    - name: mean_kills
      expr: AVG(kills)
$$;
