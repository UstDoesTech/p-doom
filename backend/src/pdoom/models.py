"""Shared models: game state, decisions, API payloads and telemetry rows.

Bearing convention: degrees relative to the direction the player faces, -180 to 180,
positive to the player's right (clockwise), negative to the left.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class PlayerType(StrEnum):
    human = "human"
    rule_bot = "rule_bot"
    ai_decide = "ai_decide"
    vision_llm = "vision_llm"
    tool_agent = "tool_agent"
    rl_policy = "rl_policy"
    bc_policy = "bc_policy"
    supervisor = "supervisor"


class Mode(StrEnum):
    paused = "paused"
    realtime = "realtime"


class Intent(StrEnum):
    advance = "advance"
    turn_left = "turn_left"
    turn_right = "turn_right"
    strafe_left = "strafe_left"
    strafe_right = "strafe_right"
    fire = "fire"
    retreat = "retreat"
    use = "use"


class Outcome(StrEnum):
    cleared = "cleared"
    died = "died"
    timeout = "timeout"


Engine = Literal["wasm", "mock"]


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


# ---- game state -------------------------------------------------------------------------


class PlayerState(BaseModel):
    health: int
    armor: int = 0
    weapon: str = "pistol"
    ammo: dict[str, int] = Field(default_factory=dict)
    x: float
    y: float
    angle: float


class Enemy(BaseModel):
    type: str
    distance: float
    bearing: float
    line_of_sight: bool


class Pickup(BaseModel):
    type: str
    distance: float
    bearing: float


class GameState(BaseModel):
    tic: int
    level: str
    seed: int
    player: PlayerState
    enemies: list[Enemy] = Field(default_factory=list, max_length=16)
    pickups: list[Pickup] = Field(default_factory=list, max_length=16)
    exit_bearing: float | None = None


class Decision(BaseModel):
    """What a player returns. intent is None when the player failed, so the caller holds."""

    intent: Intent | None = None
    confidence: float | None = None
    threat_prob: float | None = None
    danger_score: float | None = None
    latency_ms: int = 0
    error: str | None = None
    raw_output: Any = None


# ---- API payloads -----------------------------------------------------------------------


class CreateSession(BaseModel):
    player_type: PlayerType
    mode: Mode = Mode.realtime
    engine: Engine = "wasm"
    level: str = Field("E1M1", max_length=32)
    seed: int = 0


class SessionCreated(BaseModel):
    session_id: str
    model_version: str


class DecideRequest(BaseModel):
    state: GameState
    screenshot: str | None = Field(default=None, max_length=3_000_000)


class DecideResponse(BaseModel):
    intent: Intent | None
    confidence: float | None = None
    threat_prob: float | None = None
    danger_score: float | None = None
    latency_ms: int
    error: str | None = None


class EventIn(BaseModel):
    tic: int = Field(ge=0)
    ts: datetime | None = None
    health: int
    armor: int = 0
    weapon: str = "pistol"
    ammo: int = 0
    x: float
    y: float
    angle: float
    action: str | None = None

    @field_validator("ts")
    @classmethod
    def _utc(cls, v: datetime | None) -> datetime | None:
        return _aware(v) if v else v


class EventsRequest(BaseModel):
    events: list[EventIn] = Field(max_length=2000)


class EndRequest(BaseModel):
    outcome: Outcome
    duration_s: float = Field(ge=0)
    kills: int = Field(default=0, ge=0)


# ---- telemetry rows (one model per Delta table) -----------------------------------------


class SessionRow(BaseModel):
    session_id: str
    player_type: str
    mode: str
    engine: str
    level: str
    seed: int
    model_version: str
    started_at: datetime
    ended_at: datetime
    outcome: str
    duration_s: float
    kills: int


class EventRow(BaseModel):
    session_id: str
    tic: int
    ts: datetime
    health: int
    armor: int
    weapon: str
    ammo: int
    x: float
    y: float
    angle: float
    action: str | None = None


class DecisionRow(BaseModel):
    session_id: str
    tic: int
    ts: datetime
    player_type: str
    intent: str | None = None
    confidence: float | None = None
    threat_prob: float | None = None
    danger_score: float | None = None
    latency_ms: int
    error: str | None = None
    raw_output: str | None = None  # JSON text, stored as VARIANT
