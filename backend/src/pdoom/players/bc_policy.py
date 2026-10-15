"""bc_policy: behaviour cloning from human play, as k-nearest-neighbours over where the player is.

Human sessions log position, angle, health and the keys held, not what was on screen, so the
model can only imitate the human's route: given roughly where the player is and which way it
faces, do what humans did there. It is per level, so it is useless on a map nobody has played.
Build the model with `python -m pdoom.train.bc`.
"""

from __future__ import annotations

import json
import math
import time
from collections import Counter
from pathlib import Path

from ..models import Decision, GameState, Intent, PlayerType
from .base import PlayerUnavailable

DEFAULT_MODEL = Path(__file__).resolve().parent.parent / "weights" / "bc_policy.json"
K = 7
# Keys pressed together become one intent. Firing wins, then moving, then turning.
PRIORITY = (
    "fire",
    "advance",
    "retreat",
    "strafe_left",
    "strafe_right",
    "turn_left",
    "turn_right",
    "use",
)
KEY_ALIASES = {
    "forward": "advance",
    "back": "retreat",
    "left": "turn_left",
    "right": "turn_right",
    "attack": "fire",
}


def action_to_intent(action: str | None) -> Intent | None:
    """'advance+fire' -> fire. Unknown or empty actions give None."""
    if not action:
        return None
    parts = {KEY_ALIASES.get(p.strip(), p.strip()) for p in action.split("+")}
    for name in PRIORITY:
        if name in parts:
            return Intent(name)
    return None


def vector(x: float, y: float, angle: float, health: float) -> list[float]:
    r = math.radians(angle)
    return [x / 1000.0, y / 1000.0, math.sin(r), math.cos(r), health / 100.0]


class BcPolicy:
    player_type = PlayerType.bc_policy

    def __init__(self, samples: dict[str, list[tuple[list[float], str]]], meta: dict | None = None):
        self.samples = samples
        self.meta = meta or {}

    @classmethod
    def from_file(cls, path: str | Path) -> BcPolicy:
        p = Path(path)
        if not p.exists():
            msg = f"No bc_policy model at {p}; it needs human sessions, then pdoom.train.bc"
            raise PlayerUnavailable(503, msg)
        data = json.loads(p.read_text())
        samples = {lvl: [(v, a) for v, a in rows] for lvl, rows in data["samples"].items()}
        return cls(samples, data.get("meta"))

    def decide(self, state: GameState, screenshot_b64: str | None = None) -> Decision:
        t0 = time.perf_counter()
        rows = self.samples.get(state.level)
        if not rows:
            return Decision(error=f"No human demonstrations for level {state.level}")
        p = state.player
        q = vector(p.x, p.y, p.angle, p.health)
        def dist(row: tuple[list[float], str]) -> float:
            return sum((a - b) ** 2 for a, b in zip(row[0], q, strict=True))

        near = sorted(rows, key=dist)[:K]
        votes = Counter(a for _, a in near)
        action, n = votes.most_common(1)[0]
        return Decision(
            intent=Intent(action),
            confidence=round(n / len(near), 3),
            latency_ms=int((time.perf_counter() - t0) * 1000),
            raw_output={"votes": dict(votes), "k": len(near)},
        )
