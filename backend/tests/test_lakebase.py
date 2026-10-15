import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pdoom.config import Settings
from pdoom.lakebase import BATCH, FanoutSink, PgSink, TokenSource, insert_sql, row_values
from pdoom.main import build_sink
from pdoom.models import DecisionRow, EventRow
from pdoom.telemetry import COLUMNS, MemorySink

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


class Cursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def executemany(self, sql, values):
        if self.conn.fail:
            self.conn.fail -= 1
            raise OSError("connection lost")
        self.conn.calls.append((sql, list(values)))

    def execute(self, sql):
        self.conn.calls.append((sql, []))

    def fetchone(self):
        return (1,)


class Tx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Conn:
    def __init__(self, budget):
        self.budget, self.calls, self.closed = budget, [], False

    @property
    def fail(self):
        return self.budget["n"]

    @fail.setter
    def fail(self, v):
        self.budget["n"] = v

    def transaction(self):
        return Tx()

    def cursor(self):
        return Cursor(self)

    def close(self):
        self.closed = True


class Client:
    def __init__(self):
        self.n = 0
        self.postgres = self

    def generate_database_credential(self, endpoint):
        self.n += 1
        return type("C", (), {"token": f"tok{self.n}"})()


def make(fail=0):
    budget = {"n": fail}
    conns: list[Conn] = []
    seen: list[dict] = []

    def connect(**kw):
        seen.append(kw)
        c = Conn(budget)
        conns.append(c)
        return c

    client = Client()
    tokens = TokenSource("projects/p/branches/b/endpoints/e", client)
    sink = PgSink("h", "pdoom", "sp", tokens, connect=connect)
    return sink, conns, seen, client


def event(i):
    return EventRow(session_id="s", tic=i, ts=NOW, health=100, armor=0, weapon="pistol", ammo=5,
                    x=1, y=2, angle=90)


def decision(i, raw=None):
    return DecisionRow(session_id="s", tic=i, ts=NOW, player_type="rule_bot", intent="advance",
                       latency_ms=1, raw_output=raw)


def test_statements_are_parameterised_and_idempotent():
    ev = insert_sql("pdoom", "doom_events")
    assert ev.startswith('INSERT INTO "pdoom".doom_events (session_id, tic, ts,')
    assert ev.count("%s") == len(COLUMNS["doom_events"])
    assert ev.endswith("ON CONFLICT (session_id, tic) DO NOTHING")
    assert "DO UPDATE SET" in insert_sql("pdoom", "doom_sessions")
    assert "%s::jsonb" in insert_sql("pdoom", "doom_decisions")
    with pytest.raises(ValueError):
        insert_sql('x"; drop table y; --', "doom_events")


def test_values_follow_column_order():
    assert row_values("doom_events", event(7))[:3] == ("s", 7, NOW)


def test_events_and_decisions_written_in_one_transaction_per_batch():
    sink, conns, seen, _ = make()
    sink.write_events([event(i) for i in range(BATCH + 1)])
    sink.write_decisions([decision(1, '{"a":1}')])
    sink.write_sessions([])
    assert [len(v) for _, v in conns[0].calls] == [BATCH, 1, 1]
    assert len(conns) == 1  # one connection reused
    assert seen[0]["sslmode"] == "require" and seen[0]["password"] == "tok1"


def test_reconnects_once_with_a_fresh_token_after_a_failure():
    sink, conns, seen, client = make(fail=1)
    sink.write_events([event(1)])
    assert len(conns) == 2 and conns[0].closed
    assert seen[1]["password"] == "tok2"
    assert len(conns[1].calls) == 1


def test_gives_up_after_the_retry():
    sink, conns, _, _ = make(fail=5)
    conns_before = len(conns)
    with pytest.raises(OSError):
        sink.write_events([event(1)])
    assert len(conns) > conns_before


def test_token_is_cached_then_renewed():
    now = [0.0]
    client = Client()
    t = TokenSource("e", client, clock=lambda: now[0])
    assert t.get() == t.get() == "tok1"
    now[0] = 46 * 60
    assert t.get() == "tok2"


def test_fanout_writes_every_sink_in_order():
    a, b = MemorySink(), MemorySink()
    FanoutSink(a, b).write_events([event(1)])
    assert len(a.events) == len(b.events) == 1


def test_build_sink_modes_and_errors():
    s = Settings(sink="lakebase", _env_file=None)
    with pytest.raises(RuntimeError, match="PDOOM_LAKEBASE_ENDPOINT"):
        build_sink(s, None)
    s = Settings(sink="lakebase", pg_host="h", pg_database="d", pg_user="u",
                 lakebase_endpoint="projects/p/branches/b/endpoints/e", _env_file=None)
    assert isinstance(build_sink(s, None), PgSink)
    with pytest.raises(RuntimeError, match="WAREHOUSE"):
        build_sink(Settings(sink="both", _env_file=None), None)


def test_python_columns_match_the_postgres_ddl():
    ddl = (Path(__file__).parents[2] / "databricks/lakebase/01_tables.sql").read_text()
    for table, spec in COLUMNS.items():
        match = re.search(rf"pdoom\.{table} \(\n(.*?)\n\);", ddl, re.DOTALL)
        assert match, table
        lines = [ln for ln in match.group(1).splitlines() if not ln.strip().startswith("PRIMARY")]
        cols = [ln.split()[0] for ln in lines]
        assert cols == [n for n, _, _ in spec], table


def test_fanout_sends_the_open_session_row_only_to_sinks_that_upsert():
    class Upserting(MemorySink):
        def write_open_session(self, rows):
            self.write_sessions(rows)

    from pdoom.lakebase import FanoutSink

    a, b = Upserting(), MemorySink()
    FanoutSink(a, b).write_open_session([object()])
    assert len(a.sessions) == 1 and b.sessions == []


def test_the_open_row_has_empty_outcome_columns():
    from pdoom.models import SessionRow

    opened = SessionRow(
        session_id="s", player_type="rule_bot", mode="paused", engine="wasm", level="E1M1",
        seed=1, model_version="d", started_at=datetime.now(UTC),
    )
    values = dict(zip([c for c, _, _ in COLUMNS["doom_sessions"]],
                      row_values("doom_sessions", opened), strict=True))
    assert values["outcome"] is None and values["decisions"] is None
