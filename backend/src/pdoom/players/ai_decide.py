"""Plays Doom by asking Databricks ai_decide three typed questions about the state.

ai_decide is a Beta feature with two ways in. The REST endpoint
(POST /api/2.0/ai-functions/ai-decide) is the primary: no warehouse, state sent as JSON. The SQL
function is the backup, used when the REST call fails or comes back without a usable answer.
The SQL function's questions argument must be a constant, so the JSON is embedded in the
statement text and the state goes in as a parameter. to_json() turns the returned VARIANT into
text the warehouse API hands back reliably.

The model picks one intent a second. Left alone, a held turn swings about 90 degrees and a held
walk carries on past the point it was for, so the player cannot follow a route. After the model
answers, `shape()` says how many tics to hold the key for, from the route (or the nearest enemy),
and, when no enemy is in sight and the model turned the wrong way round the route bearing, turns
it the right way. The model still chooses what to do; the assist only sizes and aims the step.
raw_output records what it changed under "assist".

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
from .rule_bot import ENGAGE_RANGE, turn_hold, walk_hold

EXCLUDE_FROM_STATE = {"tic": True, "seed": True}
API_PATH = "/api/2.0/ai-functions/ai-decide"
BREAKER_FAILURES = 3  # consecutive transport failures before the REST path is skipped
BREAKER_PAUSE_S = 60.0
REST_TIMEOUT_S = 8.0  # one REST attempt, including retries
SQL_TIMEOUT_S = 15.0

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
            from databricks.sdk.core import Config

            # The SDK retries transient errors for up to five minutes by default. A game cannot
            # wait that long for one move.
            self._client = WorkspaceClient(
                config=Config(
                    http_timeout_seconds=REST_TIMEOUT_S, retry_timeout_seconds=int(REST_TIMEOUT_S)
                )
            )
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
        assist: bool = True,
    ) -> None:
        if sql is None and api is None:
            raise ValueError("AiDecidePlayer needs the REST API, a SQL runner, or both")
        self.sql, self.api, self._clock = sql, api, clock
        self.assist = assist
        self._failures = 0
        self._paused_until = 0.0
        questions_json = json.dumps(questions or load_questions(), separators=(",", ":"))
        self.statement = (
            "SELECT to_json(ai_decide(:state, "
            f"'{sql_literal(questions_json)}', map('version', '1.0'))) AS decision"
        )

    def decide(self, state: GameState, screenshot_b64: str | None = None) -> Decision:
        decision = self._decide(state)
        return shape(state, decision) if self.assist else decision

    def _decide(self, state: GameState) -> Decision:
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
        rows = self.sql.run(self.statement, {"state": payload}, max_wait_s=SQL_TIMEOUT_S)
        if not rows or not rows[0] or rows[0][0] is None:
            return Decision(error="ai_decide returned no rows")
        return parse_decision(rows[0][0])


ALIGNED_DEG = 15.0
FLIP_ABOVE_DEG = 20.0


def shape(state: GameState, decision: Decision) -> Decision:
    """Size and aim the model's step. Returns the decision unchanged when it has no intent or
    there is nothing to steer by."""
    intent = decision.intent
    if intent is None:
        return decision
    visible = [e for e in state.enemies if e.line_of_sight and e.distance <= ENGAGE_RANGE]
    nearest = min(visible, key=lambda e: e.distance, default=None)
    route = state.route
    hold: int | None = None
    note: str | None = None

    if intent in (Intent.turn_left, Intent.turn_right):
        target = nearest.bearing if nearest else route.bearing if route else None
        if target is None:
            return decision
        if abs(target) <= ALIGNED_DEG / 2:
            return decision
        right = target > 0
        wrong_way = (intent == Intent.turn_right) != right
        if nearest is None and abs(target) > FLIP_ABOVE_DEG and wrong_way:
            intent = Intent.turn_right if right else Intent.turn_left
            note = f"turned {'right' if right else 'left'} toward the route, not away"
        elif nearest is not None and wrong_way:
            return decision  # turning away from an enemy on purpose
        hold = turn_hold(target)
    elif intent == Intent.advance and nearest is None and route is not None:
        reach = route.distance
        if route.via in ("door", "exit", "switch"):
            reach = max(0.0, reach - 40.0)
        hold = walk_hold(reach)

    if hold is None and note is None:
        return decision
    raw = decision.raw_output
    out = dict(raw) if isinstance(raw, dict) else {"output": raw}
    out["assist"] = {"hold_tics": hold, **({"note": note} if note else {})}
    return decision.model_copy(update={"intent": intent, "hold_tics": hold, "raw_output": out})


def _tag(decision: Decision, transport: str, rest_error: str | None = None) -> Decision:
    raw = decision.raw_output
    out = dict(raw) if isinstance(raw, dict) else {"output": raw}
    out["transport"] = transport
    if rest_error:
        out["rest_error"] = rest_error
    return decision.model_copy(update={"raw_output": out})
