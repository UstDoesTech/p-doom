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

With an enemy in sight, in range and ammo in hand, the assist also does the aiming: if the model
picked anything but a retreat or a sidestep, it turns to face the enemy, and once it is inside the
fire cone it fires. Left to itself the model advanced and turned about while being shot and
never fired (session b05e162a: 62 decisions, none of them fire, 100 health lost).

The model has no memory, so it can walk into a barrel or a corner every second and never learn
why nothing moves. The player remembers where it was and what it asked for; when two decisions in
a row asked to move and the player did not, and no enemy is in sight, it overrides the model with
a short slide to one side, then the other, then a step back and a turn, until it is moving again.
raw_output records it under "assist" as "unstick".

Each decision's raw_output carries "transport": "rest" or "sql", and "rest_error" when the backup
was used, so a run shows which path answered.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from dataclasses import dataclass
from importlib import resources
from typing import Any

from ..databricks_sql import SqlRunner
from ..models import Decision, GameState, Intent, PlayerType
from .rule_bot import ENGAGE_RANGE, FIRE_CONE_DEG, turn_hold, walk_hold

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


MOVING = (Intent.advance, Intent.retreat, Intent.strafe_left, Intent.strafe_right)
STUCK_UNITS_PER_TIC = 2.0  # asked to walk, it should cover at least this per tic held
UNSTICK_AFTER = 2  # decisions in a row without moving before the model is overridden
UNSTICK_HOLD = 14
JAM_TICS = 140  # about four seconds of game time
JAM_UNITS = 40.0  # less net movement than this over JAM_TICS, whatever the feet were doing


@dataclass
class _Track:
    tic: int = -1
    x: float = 0.0
    y: float = 0.0
    last: Intent | None = None
    hold: int | None = None
    stuck: int = 0
    anchor_x: float = 0.0
    anchor_y: float = 0.0
    anchor_tic: int = -1
    jam: bool = False
    overrides: int = 0  # consecutive decisions taken from the model


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
        self._tracks: dict[tuple[str, int], _Track] = {}
        self._lock = threading.Lock()
        self._failures = 0
        self._paused_until = 0.0
        questions_json = json.dumps(questions or load_questions(), separators=(",", ":"))
        self.statement = (
            "SELECT to_json(ai_decide(:state, "
            f"'{sql_literal(questions_json)}', map('version', '1.0'))) AS decision"
        )

    def decide(self, state: GameState, screenshot_b64: str | None = None) -> Decision:
        decision = self._decide(state)
        if not self.assist:
            return decision
        decision = shape(state, decision)
        with self._lock:
            return self._unstick(state, decision)

    def _unstick(self, state: GameState, decision: Decision) -> Decision:
        key = (state.level, state.seed)
        track = self._tracks.get(key)
        if track is None or state.tic < track.tic:
            track = self._tracks[key] = _Track()
            if len(self._tracks) > 256:
                self._tracks.pop(next(iter(self._tracks)))
        p = state.player
        if track.tic >= 0 and track.last in MOVING:
            elapsed = state.tic - track.tic
            if track.hold is not None:
                elapsed = min(elapsed, track.hold)
            expected = max(2.0, STUCK_UNITS_PER_TIC * elapsed) if elapsed > 0 else 6.0
            if math.hypot(p.x - track.x, p.y - track.y) < expected:
                track.stuck += 1
            else:
                track.stuck = 0
        elif track.stuck and track.last not in MOVING:
            pass  # a turn or a shot while stuck neither proves nor clears it
        else:
            track.stuck = 0

        fighting = any(e.line_of_sight and e.distance <= ENGAGE_RANGE for e in state.enemies)
        # Net movement over a window catches what the per-step check cannot: sliding a few units
        # along a barrel each step looks like progress while the player goes nowhere.
        if track.anchor_tic < 0:
            track.anchor_x, track.anchor_y, track.anchor_tic = p.x, p.y, state.tic
        else:
            moved = math.hypot(p.x - track.anchor_x, p.y - track.anchor_y)
            if moved >= JAM_UNITS:  # clear of it: start a fresh window here
                track.jam = False
                track.anchor_x, track.anchor_y, track.anchor_tic = p.x, p.y, state.tic
            elif state.tic - track.anchor_tic >= JAM_TICS:
                track.jam = not fighting
                track.anchor_x, track.anchor_y, track.anchor_tic = p.x, p.y, state.tic
        if fighting:
            track.jam = False
        if (track.stuck >= UNSTICK_AFTER or track.jam) and not fighting:
            step = track.overrides % 5
            track.overrides += 1
            bearing = state.route.bearing if state.route else None
            left_first = bearing is not None and bearing < 0
            first = Intent.strafe_left if left_first else Intent.strafe_right
            other = Intent.strafe_right if first == Intent.strafe_left else Intent.strafe_left
            intent, hold = [
                (first, UNSTICK_HOLD),
                (Intent.advance, UNSTICK_HOLD),
                (other, UNSTICK_HOLD * 2),
                (Intent.advance, UNSTICK_HOLD),
                (Intent.retreat, 8),
            ][step]
            raw = decision.raw_output
            out = dict(raw) if isinstance(raw, dict) else {"output": raw}
            out["assist"] = {
                **(out.get("assist") or {}),
                "unstick": {
                    "stuck": track.stuck,
                    "jam": track.jam,
                    "model_intent": str(decision.intent),
                    "step": step,
                },
            }
            decision = decision.model_copy(
                update={"intent": intent, "hold_tics": hold, "raw_output": out}
            )

        else:
            track.overrides = 0
        track.tic, track.x, track.y = state.tic, p.x, p.y
        track.last, track.hold = decision.intent, decision.hold_tics
        return decision

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


KEEP_IN_A_FIGHT = (Intent.retreat, Intent.strafe_left, Intent.strafe_right, Intent.use)
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

    has_ammo = not state.player.ammo or sum(state.player.ammo.values()) > 0
    if nearest is not None and has_ammo and intent not in KEEP_IN_A_FIGHT:
        # The model can pick retreat or a sidestep, but advancing or turning about while something
        # shoots at it is how a session lost 100 health without firing once. Face it, then fire.
        if abs(nearest.bearing) > FIRE_CONE_DEG:
            intent = Intent.turn_right if nearest.bearing > 0 else Intent.turn_left
            hold, note = turn_hold(nearest.bearing), "faced the enemy"
        elif intent != Intent.fire:
            intent, hold, note = Intent.fire, None, "fired at the enemy in the cone"
        if note is None:
            return decision  # already firing at it
        return _assisted(decision, intent, hold, note)
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


def _assisted(decision: Decision, intent: Intent, hold: int | None, note: str) -> Decision:
    raw = decision.raw_output
    out = dict(raw) if isinstance(raw, dict) else {"output": raw}
    out["assist"] = {"hold_tics": hold, "note": note, "model_intent": str(decision.intent)}
    return decision.model_copy(update={"intent": intent, "hold_tics": hold, "raw_output": out})


def _tag(decision: Decision, transport: str, rest_error: str | None = None) -> Decision:
    raw = decision.raw_output
    out = dict(raw) if isinstance(raw, dict) else {"output": raw}
    out["transport"] = transport
    if rest_error:
        out["rest_error"] = rest_error
    return decision.model_copy(update={"raw_output": out})
