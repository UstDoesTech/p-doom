"""Create the Unity Catalog tables and views for p(doom).

    python -m pdoom.setup_db --sql-dir ../databricks/sql

Reads PDOOM_WAREHOUSE_ID, PDOOM_UC_CATALOG and PDOOM_UC_SCHEMA (or the flags below) and the
usual DATABRICKS_* credentials. Every statement is idempotent, so it is safe to re-run.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from .config import get_settings
from .databricks_sql import SqlRunner

_SPLIT = re.compile(r";[ \t]*(?:\r?\n|$)")


TABLES = ("doom_sessions", "doom_events", "doom_decisions")


def load_statements(
    sql_dir: Path, catalog: str, schema: str, lakebase_catalog: str = ""
) -> list[str]:
    """`*.uc.sql` creates the telemetry tables in Unity Catalog, `*.lakebase.sql` creates views
    over the registered Lakebase catalog instead. Only one of the two sets is used."""
    statements: list[str] = []
    for path in sorted(sql_dir.glob("*.sql")):
        if path.name.endswith(".uc.sql") and lakebase_catalog:
            continue
        if path.name.endswith(".lakebase.sql") and not lakebase_catalog:
            continue
        text = path.read_text(encoding="utf-8")
        text = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("--"))
        text = text.replace("{catalog}", catalog).replace("{schema}", schema)
        text = text.replace("{lakebase_catalog}", lakebase_catalog)
        statements += [s.strip() for s in _SPLIT.split(text) if s.strip()]
    return statements


def retire_uc_tables(runner: SqlRunner) -> None:
    """Move real tables of the old Unity Catalog sink out of the way so the views can take their
    names. Nothing is dropped: the tables are renamed to <name>_uc_legacy."""
    for table in TABLES:
        rows = runner.run(
            f"SELECT table_type FROM `{runner.catalog}`.information_schema.tables "
            "WHERE table_schema = :schema AND table_name = :name",
            {"schema": runner.schema, "name": table},
        )
        if rows and rows[0][0] != "VIEW":
            print(f"Renaming table {table} to {table}_uc_legacy")
            old, new = runner.table(table), runner.table(table + "_uc_legacy")
            runner.run(f"ALTER TABLE {old} RENAME TO {new}")


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sql-dir", type=Path, required=True)
    parser.add_argument("--warehouse-id", default=settings.warehouse_id)
    parser.add_argument("--catalog", default=settings.uc_catalog)
    parser.add_argument("--schema", default=settings.uc_schema)
    parser.add_argument(
        "--lakebase-catalog",
        default=settings.uc_lakebase_catalog,
        help="Catalog the Lakebase database is registered as. Telemetry then lives in Lakebase "
        "and Unity Catalog gets views over it",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    statements = load_statements(args.sql_dir, args.catalog, args.schema, args.lakebase_catalog)
    if args.dry_run:
        print("\n\n".join(statements))
        return
    if not args.warehouse_id:
        parser.error("--warehouse-id or PDOOM_WAREHOUSE_ID is required")

    runner = SqlRunner(args.warehouse_id, args.catalog, args.schema)
    if args.lakebase_catalog:
        retire_uc_tables(runner)
    for i, statement in enumerate(statements, 1):
        print(f"[{i}/{len(statements)}] {statement.splitlines()[0][:90]}")
        runner.run(statement)
    print("Done.")


if __name__ == "__main__":
    main()
