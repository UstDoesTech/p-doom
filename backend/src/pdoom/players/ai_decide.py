"""Plays Doom by asking Databricks ai_decide three typed questions about the state.

ai_decide is a Beta feature with two ways in. The REST endpoint
(POST /api/2.0/ai-functions/ai-decide) is the primary: no warehouse, state sent as JSON. The SQL
function is the backup, used when the REST call fails or comes back without a usable answer.
The SQL function's questions argument must be a constant, so the JSON is embedded in the
statement text and the state goes in as a parameter. to_json() turns the returned VARIANT into
text the warehouse API hands back reliably.

Each decision's raw_output carries "transport": "rest" or "sql", and "rest_error" when the backup
was used, so a run shows which path answered.
"""

from __future__ import annotations

import json
import logging
import time
from importlib import resources
from typing import Any

from ..databricks_sql import SqlRunner
from ..models import Decision, GameState, Intent, PlayerType

EXCLUDE_FROM_STATE = {"tic": True, "seed": True}
API_PATH = "/api/2.0/ai-functions/ai-decide"
BREAKER_FAILURES = 3  # consecutive transport failures before the REST path is skipped
BREAKER_PAUSE_S = 60.0

log = logging.getLogger(__name__)


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


class AiDecideApi:
    """The REST transport. Raises on HTTP or network errors; returns the response dict."""

    def __init__(self, questions: dict[str, Any], client: Any = None) -> None:
        self.questions = questions
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            from databricks.sdk import WorkspaceClient

            self._client = WorkspaceClient()
        return self._client

    def run(self, state: GameState) -> dict[str, Any]:
        body = {
            "state": state.model_dump(mode="json", exclude=EXCLUDE_FROM_STATE),
            "questions": self.questions,
            "options": {"version": "1.0"},
        }
        return self.client.api_client.do("POST", API_PATH, body=body)


class AiDecidePlayer:
    player_type = PlayerType.ai_decide

    def __init__(
        self,
        sql: SqlRunner | None = None,
        questions: dict[str, Any] | None = None,
        api: AiDecideApi | None = None,
        clock: Any = time.monotonic,
    ) -> None:
        if sql is None and api is None:
            raise ValueError("AiDecidePlayer needs the REST API, a SQL runner, or both")
        self.sql, self.api, self._clock = sql, api, clock
        self._failures = 0
        self._paused_until = 0.0
        questions_json = json.dumps(questions or load_questions(), separators=(",", ":"))
        self.statement = (
            "SELECT to_json(ai_decide(:state, "
            f"'{sql_literal(questions_json)}', map('version', '1.0'))) AS decision"
        )

    def decide(self, state: GameState, screenshot_b64: str | None = None) -> Decision:
        rest_error: str | None = None
        if self.api is not None and self._clock() >= self._paused_until:
            decision, rest_error = self._decide_rest(state)
            if decision is not None:
                return decision
        elif self.api is not None:
            rest_error = "REST paused after repeated failures"
        if self.sql is None:
            return Decision(error=rest_error or "ai_decide has no transport")
        decision = self._decide_sql(state)
        if rest_error is not None:
            decision = _tag(decision, "sql", rest_error)
        else:
            decision = _tag(decision, "sql")
        return decision

    def _decide_rest(self, state: GameState) -> tuple[Decision | None, str | None]:
        """Returns (decision, None) on success, or (None, why) when the backup should run.
        With no backup configured, a model-level error is still returned as the decision."""
        try:
            raw = self.api.run(state)
        except Exception as exc:  # noqa: BLE001 - any transport failure falls back
            self._failures += 1
            if self._failures >= BREAKER_FAILURES:
                self._paused_until = self._clock() + BREAKER_PAUSE_S
                self._failures = 0
                log.warning("ai_decide REST failed %d times, pausing it", BREAKER_FAILURES)
            return None, f"{type(exc).__name__}: {str(exc)[:300]}"
        self._failures = 0
        decision = parse_decision(json.dumps(raw))
        if decision.intent is not None:
            return _tag(decision, "rest"), None
        if self.sql is None:
            return _tag(decision, "rest"), None
        return None, decision.error or "REST gave no usable answer"

    def _decide_sql(self, state: GameState) -> Decision:
        payload = state.model_dump_json(exclude=EXCLUDE_FROM_STATE)
        rows = self.sql.run(self.statement, {"state": payload})
        if not rows or not rows[0] or rows[0][0] is None:
            return Decision(error="ai_decide returned no rows")
        return parse_decision(rows[0][0])


def _tag(decision: Decision, transport: str, rest_error: str | None = None) -> Decision:
    raw = decision.raw_output
    out = dict(raw) if isinstance(raw, dict) else {"output": raw}
    out["transport"] = transport
    if rest_error:
        out["rest_error"] = rest_error
    return decision.model_copy(update={"raw_output": out})
