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

    # The public app for human players. Only human sessions on the real engine are accepted, no
    # player is built, and the limits below apply. A value of 0 for a limit means "the default for
    # this mode": none in the full app, the figures in HUMAN_DEFAULTS in human-only mode.
    human_only: bool = False
    trust_proxy: bool = False  # count requests by the X-Forwarded-For the ingress adds
    max_open_sessions: int = 0
    sessions_per_minute: int = 0  # new sessions per client address
    max_events_per_session: int = 0

    session_ttl_s: int = 900  # idle this long and the session is recorded as abandoned
    sweep_interval_s: int = 60


@lru_cache
def get_settings() -> Settings:
    return Settings()
