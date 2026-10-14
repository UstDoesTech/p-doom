# Architecture

```
Browser (React)
  engine (mock or Doom WASM) + reflex layer (holds an intent as key presses every tic)
  runner: asks the backend for a new intent every decision gap, batches telemetry
      |  HTTPS /api
Azure Container App (one container, one replica)
  FastAPI: players, sessions, telemetry sink
      |  service principal (OAuth M2M)
Databricks
  SQL warehouse: ai_decide, telemetry inserts
  Unity Catalog: doom_sessions, doom_events, doom_decisions, views, metric view
  Genie: questions over the above
```

- Modes: `paused` waits for every decision. `realtime` holds the last intent while the model thinks.
- The session store lives in memory, so run exactly one replica.
- Telemetry sinks: memory, jsonl, sql. Zerobus is a possible future sink; it is not built.
