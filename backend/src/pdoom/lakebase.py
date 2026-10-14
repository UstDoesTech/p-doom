"""Lakebase (Postgres) telemetry sink.

Writes the same three tables as the Unity Catalog sink, in schema `pdoom` of the Lakebase
database. Every insert is idempotent (ON CONFLICT DO NOTHING, sessions are upserted), so a
retried batch cannot double-count. One connection is kept and replaced when it fails or when
its OAuth token is close to expiring. Tokens last an hour; they are renewed after 45 minutes.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any

from pydantic import BaseModel

from .models import DecisionRow, EventRow, SessionRow
from .telemetry import COLUMNS

log = logging.getLogger("pdoom.lakebase")

TOKEN_TTL_S = 45 * 60
BATCH = 500

CONFLICT = {
    "doom_sessions": "ON CONFLICT (session_id) DO UPDATE SET "
    + ", ".join(
        f"{c}=EXCLUDED.{c}" for c, _, _ in COLUMNS["doom_sessions"] if c != "session_id"
    ),
    "doom_events": "ON CONFLICT (session_id, tic) DO NOTHING",
    "doom_decisions": "ON CONFLICT (session_id, tic, ts) DO NOTHING",
}


def insert_sql(schema: str, table: str) -> str:
    """Parameterised INSERT for one table. Column order matches COLUMNS, and so the DDL."""
    import re

    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        raise ValueError(f"Invalid schema name: {schema!r}")
    spec = COLUMNS[table]
    cols = ", ".join(name for name, _, _ in spec)
    marks = ", ".join("%s::jsonb" if name == "raw_output" else "%s" for name, _, _ in spec)
    return f'INSERT INTO "{schema}".{table} ({cols}) VALUES ({marks}) {CONFLICT[table]}'


def row_values(table: str, row: BaseModel) -> tuple[Any, ...]:
    data = row.model_dump(mode="python")
    return tuple(data[name] for name, _, _ in COLUMNS[table])


class TokenSource:
    """OAuth tokens for the Lakebase endpoint, cached for TOKEN_TTL_S."""

    def __init__(self, endpoint: str, client: Any = None, clock: Callable[[], float] = time.time):
        self.endpoint = endpoint
        self._client = client
        self._clock = clock
        self._token = ""
        self._at = 0.0

    def get(self, force: bool = False) -> str:
        if force or not self._token or self._clock() - self._at > TOKEN_TTL_S:
            if self._client is None:
                from databricks.sdk import WorkspaceClient

                self._client = WorkspaceClient()
            self._token = self._client.postgres.generate_database_credential(
                endpoint=self.endpoint
            ).token
            self._at = self._clock()
        return self._token


class PgSink:
    def __init__(
        self,
        host: str,
        database: str,
        user: str,
        tokens: TokenSource,
        schema: str = "pdoom",
        connect: Callable[..., Any] | None = None,
    ) -> None:
        self.host, self.database, self.user, self.schema = host, database, user, schema
        self.tokens = tokens
        self._connect = connect
        self._conn: Any = None
        self._conn_at = 0.0
        self._lock = threading.Lock()

    def _open(self) -> Any:
        connect = self._connect
        if connect is None:
            import psycopg

            connect = psycopg.connect
        conn = connect(
            host=self.host,
            dbname=self.database,
            user=self.user,
            password=self.tokens.get(),
            sslmode="require",
            connect_timeout=15,
        )
        self._conn_at = time.time()
        return conn

    def _connection(self) -> Any:
        stale = time.time() - self._conn_at > TOKEN_TTL_S
        if self._conn is None or getattr(self._conn, "closed", False) or stale:
            self._close()
            self._conn = self._open()
        return self._conn

    def _close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None

    def _insert(self, table: str, rows: Sequence[BaseModel]) -> None:
        if not rows:
            return
        sql = insert_sql(self.schema, table)
        with self._lock:
            for start in range(0, len(rows), BATCH):
                values = [row_values(table, r) for r in rows[start : start + BATCH]]
                for attempt in (1, 2):
                    try:
                        conn = self._connection()
                        with conn.transaction(), conn.cursor() as cur:
                            cur.executemany(sql, values)
                        break
                    except Exception:
                        # A dropped connection or an expired token: reconnect once with a new token.
                        self._close()
                        self.tokens.get(force=True)
                        if attempt == 2:
                            raise
                        log.warning("Lakebase write failed, reconnecting", exc_info=True)

    def write_sessions(self, rows: Sequence[SessionRow]) -> None:
        self._insert("doom_sessions", rows)

    def write_events(self, rows: Sequence[EventRow]) -> None:
        self._insert("doom_events", rows)

    def write_decisions(self, rows: Sequence[DecisionRow]) -> None:
        self._insert("doom_decisions", rows)

    def ping(self) -> bool:
        with self._lock:
            with self._connection().cursor() as cur:
                cur.execute("SELECT 1")
                return cur.fetchone() == (1,)


class FanoutSink:
    """Writes to each sink in order. Put the idempotent one first: if a later sink fails the
    caller retries the whole batch, and the first sink ignores the repeat."""

    def __init__(self, *sinks: Any) -> None:
        self.sinks = sinks

    def write_sessions(self, rows: Sequence[SessionRow]) -> None:
        for s in self.sinks:
            s.write_sessions(rows)

    def write_events(self, rows: Sequence[EventRow]) -> None:
        for s in self.sinks:
            s.write_events(rows)

    def write_decisions(self, rows: Sequence[DecisionRow]) -> None:
        for s in self.sinks:
            s.write_decisions(rows)
