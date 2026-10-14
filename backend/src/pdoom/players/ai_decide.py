"""Plays Doom by asking Databricks ai_decide three typed questions about the state.

ai_decide is a Beta SQL function. The questions argument must be a constant, so the JSON is
embedded in the statement text, and the state goes in as a parameter. to_json() turns the
returned VARIANT into text the warehouse API hands back reliably.

The docs also mention a REST endpoint. Its request shape is not recorded here yet. When Beta
access is on, add it as a second transport behind AiDecidePlayer.decide.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

from ..databricks_sql import SqlRunner
from ..models import Decision, GameState, Intent, PlayerType

EXCLUDE_FROM_STATE = {"tic": True, "seed": True}


def load_questions() -> dict[str, Any]:
    text = resources.files("pdoom.players").joinpath("ai_decide_questions.json").read_text("utf-8")
    return json.loads(text)


def sql_literal(text: str) -> str:
    """Escape text for a single-quoted Spark SQL string literal."""
    return text.replace("\\", "\\\\").replace("'", "\\'")


def parse_decision(raw_json: str) -> Decision:
    """Turn the to_json(ai_decide(...)) text into a Decision. Never raises."""
    try:
        obj = json.loads(raw_json)
    except (TypeError, json.JSONDecodeError) as exc:
        return Decision(
            error=f"Unreadable ai_decide output: {exc}", raw_output=str(raw_json)[:2000]
        )

    if obj.get("error_message") or not obj.get("response"):
        return Decision(error=str(obj.get("error_message") or "Empty response"), raw_output=obj)

    try:
        answers = obj["response"]["answers"]
        choice = answers["intent"]["choice"]
        confidence = answers["intent"].get("confidence")
        threat = answers["threat"].get("probability")
        danger = answers["danger"].get("score")
    except (KeyError, TypeError, AttributeError) as exc:
        return Decision(error=f"Missing answer {exc}", raw_output=obj)

    if choice not in Intent.__members__:
        return Decision(error=f"Unknown intent {choice!r}", raw_output=obj)

    return Decision(
        intent=Intent(choice),
        confidence=confidence,
        threat_prob=threat,
        danger_score=danger,
        raw_output=obj,
    )


class AiDecidePlayer:
    player_type = PlayerType.ai_decide

    def __init__(self, sql: SqlRunner, questions: dict[str, Any] | None = None) -> None:
        self.sql = sql
        questions_json = json.dumps(questions or load_questions(), separators=(",", ":"))
        self.statement = (
            "SELECT to_json(ai_decide(:state, "
            f"'{sql_literal(questions_json)}', map('version', '1.0'))) AS decision"
        )

    def decide(self, state: GameState, screenshot_b64: str | None = None) -> Decision:
        payload = state.model_dump_json(exclude=EXCLUDE_FROM_STATE)
        rows = self.sql.run(self.statement, {"state": payload})
        if not rows or not rows[0] or rows[0][0] is None:
            return Decision(error="ai_decide returned no rows")
        return parse_decision(rows[0][0])
