# Architecture

```
Browser (React)
  engine (mock or Doom WASM) + reflex layer (holds an intent as key presses every tic)
  runner: asks the backend for a new intent every decision gap (ai_decide: two calls in flight, spread evenly), batches telemetry
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
- Telemetry sinks: memory, jsonl, sql, lakebase (the primary one; Unity Catalog reads it through the
  foreign catalog `pdoom_lakebase`). Zerobus is a possible future sink; it is not built.
- A session row is written as open (no outcome) when the game starts and upserted when it ends.
  An end call is idempotent. Sessions idle for 15 minutes are swept to `abandoned`; the page's
  `pagehide` sends a keepalive end as `abandoned`; a crashed loop ends as `error`.
- `/api/health` reports `ok` or `degraded` from a cached Lakebase ping, plus open session count.
- Human-only edition: the same backend with `PDOOM_HUMAN_ONLY=true`, built from `Dockerfile.human` and run
  as its own Container App (`infra/human.bicep`). It reaches Lakebase as its own service principal. Session
  creation is validated (player, engine, level, seed) and rate limited in `pdoom/limits.py`; the player
  types and `/decide` are closed. The web app is `HumanApp.tsx`, chosen at build time in `main.tsx`.
- The page can run a set (roster x seed range) one game after another; there is no headless runner.
