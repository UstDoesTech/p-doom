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


def load_statements(sql_dir: Path, catalog: str, schema: str) -> list[str]:
    statements: list[str] = []
    for path in sorted(sql_dir.glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        text = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("--"))
        text = text.replace("{catalog}", catalog).replace("{schema}", schema)
        statements += [s.strip() for s in _SPLIT.split(text) if s.strip()]
    return statements


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sql-dir", type=Path, required=True)
    parser.add_argument("--warehouse-id", default=settings.warehouse_id)
    parser.add_argument("--catalog", default=settings.uc_catalog)
    parser.add_argument("--schema", default=settings.uc_schema)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    statements = load_statements(args.sql_dir, args.catalog, args.schema)
    if args.dry_run:
        print("\n\n".join(statements))
        return
    if not args.warehouse_id:
        parser.error("--warehouse-id or PDOOM_WAREHOUSE_ID is required")

    runner = SqlRunner(args.warehouse_id, args.catalog, args.schema)
    for i, statement in enumerate(statements, 1):
        print(f"[{i}/{len(statements)}] {statement.splitlines()[0][:90]}")
        runner.run(statement)
    print("Done.")


if __name__ == "__main__":
    main()
