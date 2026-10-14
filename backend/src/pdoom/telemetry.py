"""Telemetry sinks. Every player type writes the same three tables.

The SQL sink sends each batch as ONE JSON parameter and unpacks it on the warehouse with
from_json and explode. That keeps statements small, typed and free of string-built values.
Zerobus can replace SqlSink later behind the same TelemetrySink protocol.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from .databricks_sql import SqlRunner
from .models import DecisionRow, EventRow, SessionRow

# table -> [(column, from_json type, optional SELECT expression)]. Order matches the DDL in
# databricks/sql/01_tables.uc.sql, and a test keeps the two in step.
COLUMNS: dict[str, list[tuple[str, str, str | None]]] = {
    "doom_sessions": [
        ("session_id", "string", None),
        ("player_type", "string", None),
        ("mode", "string", None),
        ("engine", "string", None),
        ("level", "string", None),
        ("seed", "bigint", None),
        ("model_version", "string", None),
        ("started_at", "string", "CAST(e.started_at AS TIMESTAMP)"),
        ("ended_at", "string", "CAST(e.ended_at AS TIMESTAMP)"),
        ("outcome", "string", None),
        ("duration_s", "double", None),
        ("kills", "int", None),
    ],
    "doom_events": [
        ("session_id", "string", None),
        ("tic", "int", None),
        ("ts", "string", "CAST(e.ts AS TIMESTAMP)"),
        ("health", "int", None),
        ("armor", "int", None),
        ("weapon", "string", None),
        ("ammo", "int", None),
        ("x", "double", None),
        ("y", "double", None),
        ("angle", "double", None),
        ("action", "string", None),
    ],
    "doom_decisions": [
        ("session_id", "string", None),
        ("tic", "int", None),
        ("ts", "string", "CAST(e.ts AS TIMESTAMP)"),
        ("player_type", "string", None),
        ("intent", "string", None),
        ("confidence", "double", None),
        ("threat_prob", "double", None),
        ("danger_score", "double", None),
        ("latency_ms", "int", None),
        ("error", "string", None),
        ("raw_output", "string", "parse_json(e.raw_output)"),
    ],
}

BATCH = 500


class TelemetrySink(Protocol):
    def write_sessions(self, rows: Sequence[SessionRow]) -> None: ...
    def write_events(self, rows: Sequence[EventRow]) -> None: ...
    def write_decisions(self, rows: Sequence[DecisionRow]) -> None: ...


def insert_statement(table_fqn: str, table: str) -> str:
    spec = COLUMNS[table]
    struct = ",".join(f"{name}:{typ}" for name, typ, _ in spec)
    cols = ", ".join(name for name, _, _ in spec)
    exprs = ", ".join(expr or f"e.{name}" for name, _, expr in spec)
    return (
        f"INSERT INTO {table_fqn} ({cols}) SELECT {exprs} "
        f"FROM (SELECT explode(from_json(:rows, 'array<struct<{struct}>>')) AS e)"
    )


class MemorySink:
    def __init__(self) -> None:
        self.sessions: list[SessionRow] = []
        self.events: list[EventRow] = []
        self.decisions: list[DecisionRow] = []

    def write_sessions(self, rows: Sequence[SessionRow]) -> None:
        self.sessions.extend(rows)

    def write_events(self, rows: Sequence[EventRow]) -> None:
        self.events.extend(rows)

    def write_decisions(self, rows: Sequence[DecisionRow]) -> None:
        self.decisions.extend(rows)


class JsonlSink:
    """Appends one JSON line per row, tagged with its table. For local runs."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def _append(self, table: str, rows: Sequence[BaseModel]) -> None:
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps({"table": table, **row.model_dump(mode="json")}) + "\n")

    def write_sessions(self, rows: Sequence[SessionRow]) -> None:
        self._append("doom_sessions", rows)

    def write_events(self, rows: Sequence[EventRow]) -> None:
        self._append("doom_events", rows)

    def write_decisions(self, rows: Sequence[DecisionRow]) -> None:
        self._append("doom_decisions", rows)


class SqlSink:
    def __init__(self, runner: SqlRunner) -> None:
        self.runner = runner

    def _insert(self, table: str, rows: Sequence[BaseModel]) -> None:
        if not rows:
            return
        statement = insert_statement(self.runner.table(table), table)
        for start in range(0, len(rows), BATCH):
            chunk = rows[start : start + BATCH]
            payload = json.dumps([r.model_dump(mode="json") for r in chunk])
            self.runner.run(statement, {"rows": payload})

    def write_sessions(self, rows: Sequence[SessionRow]) -> None:
        self._insert("doom_sessions", rows)

    def write_events(self, rows: Sequence[EventRow]) -> None:
        self._insert("doom_events", rows)

    def write_decisions(self, rows: Sequence[DecisionRow]) -> None:
        self._insert("doom_decisions", rows)
