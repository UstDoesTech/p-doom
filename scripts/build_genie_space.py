"""Builds databricks/genie/pdoom.geniespace.json, the serialized Genie space the bundle deploys.

The text instructions come from databricks/genie/instructions.md, so there is one place to edit
them. Run it after changing that file or the views:  python scripts/build_genie_space.py
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG, SCHEMA = "workspace", "pdoom"
FQ = f"{CATALOG}.{SCHEMA}"


def nid(label: str) -> str:
    """A stable 32-hex id, so the file only changes when its content does."""
    return hashlib.md5(label.encode()).hexdigest()


def instruction_lines() -> list[str]:
    """Each top-level bullet in instructions.md, continuation lines joined."""
    lines: list[str] = []
    for raw in (ROOT / "databricks/genie/instructions.md").read_text().splitlines():
        if raw.startswith("- "):
            lines.append(raw[2:].strip())
        elif raw.startswith("  ") and lines:
            lines[-1] += " " + raw.strip()
    return lines


SAMPLES = [
    "What is p(doom) for each player type?",
    "Which player types are significantly better than the rest?",
    "How many games were abandoned, stopped or lost, by player type?",
    "On how many seeds did at least one machine player clear the level?",
    "Which machine players fail the most decisions?",
]

EXAMPLES = [
    (
        "What is p(doom) for each player type?",
        f"SELECT player_type, MEASURE(attempts) AS attempts, MEASURE(p_doom) AS p_doom "
        f"FROM {FQ}.doom_metrics WHERE engine = 'wasm' GROUP BY player_type ORDER BY p_doom",
    ),
    (
        "Which player types are significantly better than the rest?",
        f"SELECT player_type, attempts, p_doom, p_doom_low, p_doom_high FROM {FQ}.doom_leaderboard "
        f"WHERE engine = 'wasm' ORDER BY p_doom",
    ),
    (
        "How many games were abandoned, stopped or lost, by player type?",
        f"SELECT player_type, MEASURE(games_started) AS games_started, MEASURE(aborted) AS aborted, "
        f"MEASURE(abandoned) AS abandoned, MEASURE(lost) AS lost "
        f"FROM {FQ}.doom_metrics WHERE engine = 'wasm' GROUP BY player_type",
    ),
    (
        "On how many seeds did at least one machine player clear the level?",
        f"SELECT level, mode, COUNT(*) AS seeds, SUM(cleared_any) AS seeds_cleared, "
        f"AVG(cleared_any) AS any_clear_rate FROM {FQ}.doom_seed_outcomes "
        f"WHERE engine = 'wasm' GROUP BY level, mode",
    ),
    (
        "Which machine players fail the most decisions?",
        f"SELECT player_type, COUNT(*) AS games, AVG(decision_failure_rate) AS failure_rate "
        f"FROM {FQ}.doom_games WHERE engine = 'wasm' AND player_type <> 'human' "
        f"AND status <> 'open' GROUP BY player_type ORDER BY failure_rate DESC",
    ),
]

TABLES = {
    "doom_decisions": [
        {"column_name": "player_type", "enable_format_assistance": True},
        {"column_name": "intent", "enable_format_assistance": True},
    ],
    "doom_games": [
        {"column_name": "player_type", "enable_format_assistance": True, "enable_entity_matching": True},
        {"column_name": "status", "enable_format_assistance": True},
        {"column_name": "outcome", "enable_format_assistance": True},
        {"column_name": "engine", "enable_format_assistance": True},
        {"column_name": "mode", "enable_format_assistance": True},
    ],
    "doom_leaderboard": [
        {"column_name": "player_type", "enable_format_assistance": True, "enable_entity_matching": True},
    ],
    "doom_seed_outcomes": [],
    "doom_sessions": [
        {"column_name": "outcome", "enable_format_assistance": True},
    ],
}


def build() -> dict:
    tables = []
    for name in sorted(TABLES):
        entry: dict = {"identifier": f"{FQ}.{name}"}
        if TABLES[name]:
            entry["column_configs"] = sorted(TABLES[name], key=lambda c: c["column_name"])
        tables.append(entry)
    return {
        "version": 2,
        "config": {
            "sample_questions": sorted(
                ({"id": nid("sample:" + q), "question": [q]} for q in SAMPLES), key=lambda x: x["id"]
            )
        },
        "data_sources": {
            "tables": tables,
            "metric_views": [{"identifier": f"{FQ}.doom_metrics"}],
        },
        "instructions": {
            "example_question_sqls": sorted(
                ({"id": nid("sql:" + q), "question": [q], "sql": [s]} for q, s in EXAMPLES),
                key=lambda x: x["id"],
            ),
            "text_instructions": [{"id": nid("text"), "content": instruction_lines()}],
        },
    }


def main() -> int:
    out = ROOT / "databricks/genie/pdoom.geniespace.json"
    text = json.dumps(build(), indent=2) + "\n"
    if "--check" in sys.argv:
        if not out.exists() or out.read_text() != text:
            print(f"{out.name} is stale; run python scripts/build_genie_space.py")
            return 1
        return 0
    out.write_text(text)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
