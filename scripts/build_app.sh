#!/usr/bin/env bash
# Assembles the Databricks App directory.
#   scripts/build_app.sh <engine-dir> <out-dir> [catalog] [schema]
# Env: SINK (default lakebase), LAKEBASE_ENDPOINT, LAKEBASE_DB. PG_HOST is left as __PG_HOST__ for the
# deploy job to fill in. <engine-dir> holds websockets-doom.js/.wasm, doom1.wad and default.cfg (the engine CI artifact).
# Needs the web app built into web/dist first (npm run build, with the engine files already in
# web/public/doom so Vite copies them), or this script copies them in afterwards.
set -euo pipefail
engine=$1; out=$2; catalog=${3:-workspace}; schema=${4:-pdoom}
root=$(cd "$(dirname "$0")/.." && pwd)

rm -rf "$out"; mkdir -p "$out/static/doom"
cp -r "$root/backend/src/pdoom" "$out/pdoom"
find "$out/pdoom" -name __pycache__ -prune -exec rm -rf {} +
cp -r "$root/web/dist/." "$out/static/"
cp "$engine"/* "$out/static/doom/"
cp -r "$root/databricks/sql" "$out/sql"

cat > "$out/requirements.txt" <<REQ
uvicorn[standard]>=0.30
pydantic-settings>=2.3
databricks-sdk[openai]>=0.81
psycopg[binary]>=3.2
REQ

cat > "$out/app.yaml" <<YAML
command: ["python", "-m", "uvicorn", "pdoom.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
env:
  - name: PDOOM_WAREHOUSE_ID
    valueFrom: sql-warehouse
  - name: PDOOM_SINK
    value: "${SINK:-lakebase}"
  - name: PDOOM_LAKEBASE_ENDPOINT
    value: "${LAKEBASE_ENDPOINT:-projects/pdoom/branches/production/endpoints/primary}"
  - name: PDOOM_PG_HOST
    value: "__PG_HOST__"
  - name: PDOOM_PG_DATABASE
    value: "${LAKEBASE_DB:-pdoom}"
  - name: PDOOM_UC_CATALOG
    value: "$catalog"
  - name: PDOOM_UC_SCHEMA
    value: "$schema"
  - name: PDOOM_STATIC_DIR
    value: static
  - name: PDOOM_MODEL_VERSION
    value: "${GITHUB_SHA:-dev}"
YAML
du -sh "$out"; ls "$out" "$out/static/doom"
