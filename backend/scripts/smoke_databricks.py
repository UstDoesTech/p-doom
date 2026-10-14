"""Smoke test against a real workspace. Run with PDOOM_SINK=sql and the DATABRICKS_* variables.

Plays one mock session with rule_bot and one with ai_decide, checks the rows landed, then deletes
them so the tables stay clean. Prints one JSON line.
"""

import json
import sys

from fastapi.testclient import TestClient

from pdoom.config import get_settings
from pdoom.main import create_app

STATE = {
    "tic": 10,
    "level": "E1M1",
    "seed": 3,
    "player": {"health": 90, "x": 0, "y": 0, "angle": 90},
    "enemies": [{"type": "zombieman", "distance": 300, "bearing": 2, "line_of_sight": True}],
    "exit_bearing": 40,
}
EVENT = {"tic": 10, "health": 90, "x": 0, "y": 0, "angle": 90, "ammo": 20, "action": "fire"}


def play(client: TestClient, player_type: str) -> dict:
    created = client.post(
        "/api/sessions",
        json={"player_type": player_type, "mode": "paused", "engine": "mock", "seed": 3},
    )
    if created.status_code != 201:
        return {"player": player_type, "create": created.status_code, "body": created.text[:300]}
    sid = created.json()["session_id"]
    decided = client.post(f"/api/sessions/{sid}/decide", json={"state": STATE})
    events = client.post(f"/api/sessions/{sid}/events", json={"events": [EVENT]})
    ended = client.post(
        f"/api/sessions/{sid}/end", json={"outcome": "died", "duration_s": 1.0, "kills": 0}
    )
    return {
        "player": player_type,
        "session_id": sid,
        "decide": decided.status_code,
        "decision": decided.json() if decided.status_code == 200 else decided.text[:300],
        "events": events.status_code,
        "end": ended.status_code,
        "end_body": "" if ended.status_code < 300 else ended.text[:300],
    }


def main() -> int:
    settings = get_settings()
    app = create_app(settings)
    client = TestClient(app)
    results = [play(client, "rule_bot"), play(client, "ai_decide")]
    ids = [r["session_id"] for r in results if "session_id" in r]

    from pdoom.databricks_sql import SqlRunner

    runner = SqlRunner(settings.warehouse_id, settings.uc_catalog, settings.uc_schema)
    landed = {}
    for table in ("doom_sessions", "doom_events", "doom_decisions"):
        rows = runner.run(
            f"SELECT count(*) FROM {runner.table(table)} "
            "WHERE session_id IN (SELECT explode(from_json(:ids, 'array<string>')))",
            {"ids": json.dumps(ids)},
        )
        landed[table] = rows
    for table in ("doom_sessions", "doom_events", "doom_decisions"):
        runner.run(
            f"DELETE FROM {runner.table(table)} "
            "WHERE session_id IN (SELECT explode(from_json(:ids, 'array<string>')))",
            {"ids": json.dumps(ids)},
        )
    print(json.dumps({"results": results, "landed": landed}, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
