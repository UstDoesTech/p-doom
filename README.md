# p(doom)

Doom played by many kinds of player (human, rule bot, `ai_decide`, vision LLM, and more to come),
with every attempt logged to Databricks and analysed in Genie. p(doom) for a player type is
deaths / attempts. The roster's p(doom) is the chance that at least one machine player type clears
the level: 1 − Π(1 − cᵢ), shown beside the observed any-clear rate.

The app is a React front end plus a FastAPI backend in one container, hosted on Azure Container Apps.
The backend calls Databricks for `ai_decide`, telemetry and analytics. See `docs/architecture.md`.

## Quickstart (local, mock arena)

```
cd backend && pip install -e ".[dev]" && cp ../.env.example .env
uvicorn pdoom.main:create_app --factory --reload
cd web && npm install && npm run dev      # http://localhost:5173
```

With `PDOOM_SINK=jsonl` nothing needs Databricks. For Databricks: set the `DATABRICKS_*` and
`PDOOM_WAREHOUSE_ID` variables, `PDOOM_SINK=sql`, then
`python -m pdoom.setup_db --sql-dir ../databricks/sql`. Use `--dry-run` first.

## Status

| Part | State |
|---|---|
| Backend, rule bot, telemetry sinks, API | Unit tested |
| Web app, mock arena, reflex layer, runner | Unit tested, builds |
| `ai_decide` player, SQL telemetry, setup script | Written, never run against a real workspace |
| Vision player | Written, never run against an endpoint |
| Doom WASM engine | Not built. `engine/pdoom_state.c` is an untested sketch |
| tool_agent, rl_policy, bc_policy, supervisor | Planned. The API returns 501 |
| Bundle, Bicep, Dockerfile, workflows | Never validated or run |

Mock arena sessions carry `engine = 'mock'`. Exclude them from every result.

## Limits

- One replica only: the session store is in memory.
- `doom-wasm` is GPL-2.0. See `engine/README.md`.
- The Genie instructions are in `databricks/genie/instructions.md`.
