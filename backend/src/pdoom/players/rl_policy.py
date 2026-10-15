"""rl_policy: a linear softmax policy over hand-built features, trained with REINFORCE.

The weights file is produced by `python -m pdoom.train.rl` against the Python mock arena
(pdoom/sim.py). It has not been trained on real Doom, so how well it transfers is untested.
At play time the policy takes the most likely intent; training samples from it.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

from ..features import N_FEATURES, featurize
from ..models import Decision, GameState, Intent, PlayerType
from .base import PlayerUnavailable

ACTIONS: tuple[Intent, ...] = tuple(Intent)
DEFAULT_WEIGHTS = Path(__file__).resolve().parent.parent / "weights" / "rl_policy.json"


def softmax(logits: list[float]) -> list[float]:
    m = max(logits)
    exps = [math.exp(v - m) for v in logits]
    total = sum(exps)
    return [e / total for e in exps]


def logits(weights: list[list[float]], feats: list[float]) -> list[float]:
    return [sum(w * f for w, f in zip(row, feats, strict=True)) for row in weights]


def load_weights(path: Path) -> list[list[float]]:
    data = json.loads(path.read_text())
    weights = data["weights"]
    if data.get("actions") != [a.value for a in ACTIONS]:
        raise ValueError("weights file was trained with a different action set")
    if len(weights) != len(ACTIONS) or any(len(r) != N_FEATURES for r in weights):
        raise ValueError("weights file has the wrong shape for the current features")
    return weights


class RlPolicy:
    player_type = PlayerType.rl_policy

    def __init__(self, weights: list[list[float]], meta: dict | None = None) -> None:
        self.weights = weights
        self.meta = meta or {}

    @classmethod
    def from_file(cls, path: str | Path) -> RlPolicy:
        p = Path(path)
        if not p.exists():
            raise PlayerUnavailable(503, f"No rl_policy weights at {p}; run pdoom.train.rl")
        try:
            return cls(load_weights(p), json.loads(p.read_text()).get("meta"))
        except (ValueError, KeyError) as exc:
            raise PlayerUnavailable(503, f"rl_policy weights unusable: {exc}") from exc

    def decide(self, state: GameState, screenshot_b64: str | None = None) -> Decision:
        t0 = time.perf_counter()
        probs = softmax(logits(self.weights, featurize(state)))
        best = max(range(len(ACTIONS)), key=probs.__getitem__)
        return Decision(
            intent=ACTIONS[best],
            confidence=round(probs[best], 4),
            latency_ms=int((time.perf_counter() - t0) * 1000),
            raw_output={
                "probs": {a.value: round(p, 3) for a, p in zip(ACTIONS, probs, strict=True)}
            },
        )
