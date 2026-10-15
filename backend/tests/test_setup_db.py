from pathlib import Path

from pdoom.databricks_sql import SqlRunner
from pdoom.setup_db import load_statements, retire_uc_tables

SQL = Path(__file__).parents[2] / "databricks/sql"


def first_words(statements):
    return [" ".join(s.split()[:6]) for s in statements]


def test_uc_mode_creates_tables_and_no_lakebase_views():
    text = "\n".join(load_statements(SQL, "workspace", "pdoom"))
    assert "CREATE TABLE IF NOT EXISTS `workspace`.`pdoom`.doom_sessions" in text
    assert "{lakebase_catalog}" not in text and "pdoom_lakebase" not in text


def test_lakebase_mode_creates_views_over_the_registered_catalog():
    statements = load_statements(SQL, "workspace", "pdoom", "pdoom_lakebase")
    text = "\n".join(statements)
    assert "CREATE TABLE" not in text
    for table in ("doom_sessions", "doom_events", "doom_decisions"):
        assert f"CREATE OR REPLACE VIEW `workspace`.`pdoom`.{table} (" in text
        assert f"FROM `pdoom_lakebase`.`pdoom`.{table}" in text
    assert "{" not in text.replace("${", "")
    # the leaderboard and the metric view are still created, after the telemetry views
    assert any("doom_leaderboard" in s for s in statements)
    assert any("doom_metrics" in s for s in statements)


class Rows(SqlRunner):
    def __init__(self, types):
        super().__init__("wh", "workspace", "pdoom", client=object())
        self.types, self.calls = types, []

    def run(self, statement, params=None):
        self.calls.append(statement)
        if "information_schema" in statement:
            t = self.types.get(params["name"])
            return [[t]] if t else []
        return []


def test_real_tables_are_renamed_not_dropped():
    r = Rows({"doom_sessions": "MANAGED", "doom_events": "VIEW"})
    retire_uc_tables(r)
    renames = [c for c in r.calls if c.startswith("ALTER TABLE")]
    assert renames == [
        "ALTER TABLE `workspace`.`pdoom`.`doom_sessions` RENAME TO "
        "`workspace`.`pdoom`.`doom_sessions_uc_legacy`"
    ]
    assert not any("DROP" in c for c in r.calls)
