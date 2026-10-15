"""Build the bc_policy model from human sessions.

    python -m pdoom.train.bc --jsonl telemetry.jsonl
    python -m pdoom.train.bc --lakebase   # reads the Lakebase tables, needs PDOOM_* settings

Only wasm (real Doom) human sessions that ended cleared are used by default, because those are
the demonstrations worth copying. Pass --all-outcomes to keep the rest.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

from ..players.bc_policy import DEFAULT_MODEL, action_to_intent, vector

MAX_PER_LEVEL = 20_000


def build(rows: Iterable[dict], source: str) -> dict:
    """rows need: level, x, y, angle, health, action."""
    samples: dict[str, list] = defaultdict(list)
    skipped = 0
    for r in rows:
        intent = action_to_intent(r.get("action"))
        if intent is None:
            skipped += 1
            continue
        bucket = samples[r["level"]]
        if len(bucket) < MAX_PER_LEVEL:
            bucket.append([vector(r["x"], r["y"], r["angle"], r["health"]), intent.value])
    return {
        "samples": samples,
        "meta": {
            "source": source,
            "rows": sum(len(v) for v in samples.values()),
            "skipped_no_action": skipped,
            "levels": sorted(samples),
        },
    }


def rows_from_jsonl(path: Path) -> list[dict]:
    """Reads a telemetry JSONL file written by the jsonl sink."""
    sessions: dict[str, dict] = {}
    events: list[dict] = []
    for line in path.read_text().splitlines():
        rec = json.loads(line)
        table = rec.get("table")
        row = rec.get("row", rec)
        if table == "doom_sessions":
            sessions[row["session_id"]] = row
        elif table == "doom_events":
            events.append(row)
    return [
        {**e, "level": s["level"], "outcome": s.get("outcome")}
        for e in events
        if (s := sessions.get(e["session_id"]))
        and s.get("player_type") == "human"
        and s.get("engine") == "wasm"
    ]


QUERY = """
SELECT s.level, s.outcome, e.x, e.y, e.angle, e.health, e.action
FROM pdoom.doom_events e JOIN pdoom.doom_sessions s USING (session_id)
WHERE s.player_type = 'human' AND s.engine = 'wasm'
"""


def rows_from_lakebase() -> list[dict]:
    from ..config import get_settings
    from ..lakebase import TokenSource

    s = get_settings()
    import psycopg

    tokens = TokenSource(s.lakebase_endpoint)
    with psycopg.connect(
        host=s.pg_host, dbname=s.pg_database, user=s.pg_user,
        password=tokens.get(), sslmode="require",
    ) as conn:
        cur = conn.execute(QUERY)
        names = [c.name for c in cur.description]
        return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


def main() -> None:
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--jsonl", type=Path)
    src.add_argument("--lakebase", action="store_true")
    ap.add_argument("--all-outcomes", action="store_true")
    ap.add_argument("--out", type=Path, default=DEFAULT_MODEL)
    args = ap.parse_args()
    rows = rows_from_jsonl(args.jsonl) if args.jsonl else rows_from_lakebase()
    if not args.all_outcomes:
        rows = [r for r in rows if r.get("outcome") == "cleared"]
    model = build(rows, str(args.jsonl) if args.jsonl else "lakebase")
    if not model["meta"]["rows"]:
        raise SystemExit("No usable human rows; nothing written")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(model))
    print(json.dumps(model["meta"], indent=1))


if __name__ == "__main__":
    main()
