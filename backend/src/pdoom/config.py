"""Runtime settings, read from PDOOM_* environment variables or a local .env file.

Databricks credentials are not read here. The Databricks SDK reads DATABRICKS_HOST,
DATABRICKS_CLIENT_ID and DATABRICKS_CLIENT_SECRET itself.
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PDOOM_", env_file=".env", extra="ignore")

    warehouse_id: str = ""
    uc_catalog: str = "main"
    uc_schema: str = "pdoom"
    uc_lakebase_catalog: str = ""  # set when telemetry lives in Lakebase

    sink: Literal["memory", "jsonl", "sql", "lakebase", "both"] = "memory"
    jsonl_path: str = "telemetry.jsonl"

    # Lakebase (Postgres). In a Databricks App with a postgres resource, PGHOST, PGDATABASE and
    # PGUSER are injected; the PDOOM_ variables override them.
    lakebase_endpoint: str = ""  # projects/<id>/branches/<id>/endpoints/<id>, for OAuth tokens
    pg_host: str = ""
    pg_database: str = ""
    pg_user: str = ""
    pg_schema: str = "pdoom"

    model_version: str = "dev"
    vision_endpoint: str = ""
    ai_decide_api: bool = True  # REST is primary for ai_decide; the warehouse is the backup
    agent_endpoint: str = ""  # chat endpoint with tool calling, for tool_agent
    rl_model: str = ""  # path override for the rl_policy weights
    bc_model: str = ""  # path override for the bc_policy model
    supervisor_workers: list[str] = ["rl_policy", "bc_policy", "tool_agent", "ai_decide"]

    static_dir: str = ""
    cors_origins: list[str] = ["http://localhost:5173"]

    session_ttl_s: int = 3600


@lru_cache
def get_settings() -> Settings:
    return Settings()
