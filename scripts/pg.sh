#!/usr/bin/env bash
# psql against the p(doom) Lakebase database as the Databricks identity in the environment
# (DATABRICKS_HOST, DATABRICKS_CLIENT_ID, DATABRICKS_CLIENT_SECRET). Arguments go to psql.
#   LAKEBASE_PROJECT (default pdoom), LAKEBASE_BRANCH (production), LAKEBASE_ENDPOINT (primary),
#   LAKEBASE_DB (pdoom)
set -euo pipefail
P=${LAKEBASE_PROJECT:-pdoom}; B=${LAKEBASE_BRANCH:-production}; E=${LAKEBASE_ENDPOINT:-primary}
EP="projects/$P/branches/$B/endpoints/$E"
host=$(databricks postgres get-endpoint "$EP" -o json | jq -r '.status.hosts.host')
tok=$(databricks postgres generate-database-credential "$EP" -o json | jq -r .token)
echo "::add-mask::$tok" >&2
PGPASSWORD="$tok" exec psql "host=$host user=$DATABRICKS_CLIENT_ID dbname=${LAKEBASE_DB:-pdoom} sslmode=require" -v ON_ERROR_STOP=1 "$@"
