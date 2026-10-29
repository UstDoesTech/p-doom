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
| `ai_decide` player | REST primary (about 1 s per call from CI), SQL backup (about 11 s); both run against the workspace from CI. Not yet played through a full Doom session |
| SQL telemetry, setup script | Run against the workspace from CI: tables, views, metric view, a rule_bot session landing in `doom_sessions` |
| Vision player | Written, never run against an endpoint |
| Doom WASM engine | Builds in CI (Emscripten 3.1.10) and passes a headless-Chromium check: live E1M1 state, keyboard, firing, pause/resume, seed restart |
| Databricks App | `app.yml` builds engine + web + backend, deploys with the bundle, then plays a rule_bot session on real Doom in a browser against the deployed app and checks the row in Unity Catalog. Not yet in the container image |
| `rl_policy` | Linear softmax policy trained with REINFORCE in the Python mock arena (`pdoom.train.rl`). Weights are committed; 95% cleared on held-out mock seeds against rule_bot's 0%. Never run on real Doom, so transfer is unknown |
| `bc_policy` | k-nearest-neighbours over human sessions (`pdoom.train.bc`). Unit tested with synthetic data; unavailable until real human sessions exist to build the model |
| `tool_agent` | LLM function-calling loop (scan tools, then `act`). Unit tested with a fake client; set `PDOOM_AGENT_ENDPOINT` to a tool-calling chat endpoint. Never run against a real endpoint |
| `supervisor` | Runs rule_bot every call, sends navigation and combat to the first available learned worker and keeps stuck and pickup with rule_bot; falls back to rule_bot if the worker fails. Unit tested |
| Bundle, `app.yml`, `databricks.yml` workflows | Run |
| Human-only app (`Dockerfile.human`, `infra/human.bicep`, `human.yml`) | Backend mode and web UI unit tested; played end to end in a browser against a stand-in engine. The image build, the Bicep (beyond `az bicep build`) and the Azure and Lakebase-role steps have not run yet |
| Bicep, Dockerfile, `deploy.yml` (Azure, the full app) | Never validated or run |

Mock arena sessions carry `engine = 'mock'`. Exclude them from every result.

## Databricks App

Run the `app` workflow (Actions, workflow_dispatch). It needs the three `DATABRICKS_*` secrets and a
service principal that is a workspace admin. The app is `pdoom` in the bundle at `databricks/`; its URL is
in the `app status` annotation. Opening it needs a workspace login with CAN_USE on the app.

## Human-only app (Azure Container Apps)

A separate, public edition for people only. It is the same code with `PDOOM_HUMAN_ONLY=true` and a web
build with `VITE_HUMAN_ONLY=true`: one page, a Play button, real Doom, keyboard. The server accepts only
`human` sessions on the `wasm` engine with a real level and seed, refuses `/decide`, and limits new games per
address (20 a minute), open games (300) and events per game (60 000); set `PDOOM_TRUST_PROXY` behind a proxy so
the address is read from `X-Forwarded-For`. Games go to the same Lakebase tables as the full app, with
`player_type = 'human'`.

Run the `human` workflow. Without `deploy` it builds `Dockerfile.human`, checks `infra/human.bicep`, runs the
container, and plays a game in headless Chromium. With `deploy=true` it also creates a Postgres role for a
dedicated service principal (insert and update on schema `pdoom` only), deploys the registry, identity and
Container App, and plays a game on the deployed URL. It needs the Azure variables and the
`HUMAN_DATABRICKS_CLIENT_ID` / `HUMAN_DATABRICKS_CLIENT_SECRET` secrets listed at the top of
`.github/workflows/human.yml`. The service principal must be allowed to request Lakebase credentials for the
project; the workflow does not grant that. One replica only, as above. The shareware WAD has episode 1 only.

## Telemetry path

The app writes only to Lakebase (`PDOOM_SINK=lakebase`). The Lakebase database is registered in Unity
Catalog as `pdoom_lakebase` (`databricks postgres create-catalog`), and `workspace.pdoom` holds views over it:
`doom_sessions`, `doom_events`, `doom_decisions`, then the leaderboard and the metric view. Genie reads live
rows with no copy. Lakehouse sync would give Delta history tables instead, but it needs a catalog on external
storage, which this workspace does not have. `python -m pdoom.setup_db --lakebase-catalog pdoom_lakebase` builds the views
and renames any tables from the old Unity Catalog sink to `*_uc_legacy`.

## Limits

- One replica only: the session store is in memory.
- `doom-wasm` is GPL-2.0. See `engine/README.md`.
- The Genie instructions are in `databricks/genie/instructions.md`.
