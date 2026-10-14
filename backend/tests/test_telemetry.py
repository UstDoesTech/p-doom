import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pdoom.databricks_sql import SqlRunner, quote_ident
from pdoom.models import DecisionRow, EventRow
from pdoom.telemetry import COLUMNS, SqlSink, insert_statement

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


class CaptureSql(SqlRunner):
    def __init__(self):
        super().__init__("wh", "main", "pdoom", client=object())
        self.calls: list[tuple[str, dict]] = []

    def run(self, statement, params=None):
        self.calls.append((statement, params or {}))
        return []


def event(i: int) -> EventRow:
    return EventRow(
        session_id="s", tic=i, ts=NOW, health=100, armor=0, weapon="pistol", ammo=50,
        x=0, y=0, angle=90, action="fire",
    )


def test_sql_sink_sends_one_json_parameter_per_batch():
    sql = CaptureSql()
    SqlSink(sql).write_events([event(i) for i in range(3)])
    statement, params = sql.calls[0]
    assert statement.startswith("INSERT INTO `main`.`pdoom`.`doom_events` (")
    assert "explode(from_json(:rows, 'array<struct<" in statement
    assert "CAST(e.ts AS TIMESTAMP)" in statement
    assert len(json.loads(params["rows"])) == 3


def test_sql_sink_splits_large_batches():
    sql = CaptureSql()
    SqlSink(sql).write_events([event(i) for i in range(501)])
    assert [len(json.loads(p["rows"])) for _, p in sql.calls] == [500, 1]


def test_sql_sink_skips_empty_writes():
    sql = CaptureSql()
    SqlSink(sql).write_decisions([])
    assert sql.calls == []


def test_decision_raw_output_is_parsed_as_variant():
    statement = insert_statement("t", "doom_decisions")
    assert "parse_json(e.raw_output)" in statement
    row = DecisionRow(session_id="s", tic=1, ts=NOW, player_type="rule_bot", latency_ms=3)
    assert row.raw_output is None


def test_identifiers_are_validated():
    assert quote_ident("my-catalog") == "`my-catalog`"
    with pytest.raises(ValueError):
        quote_ident("x`; DROP TABLE y")


def test_python_columns_match_the_ddl():
    ddl = (Path(__file__).parents[2] / "databricks/sql/01_tables.sql").read_text()
    for table, spec in COLUMNS.items():
        match = re.search(rf"\.{table} \(\n(.*?)\n\)", ddl, re.DOTALL)
        assert match, f"{table} not found in 01_tables.sql"
        ddl_cols = [line.split()[0] for line in match.group(1).splitlines()]
        assert ddl_cols == [name for name, _, _ in spec], table
