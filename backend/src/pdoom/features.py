"""One fixed-length feature vector per GameState, shared by rl_policy and bc_policy training.

Bearings follow the game state's convention: positive means the target is to the right.
"""

from __future__ import annotations

import math

from .models import GameState

FIRE_CONE_DEG = 8.0
FEATURE_NAMES = (
    "bias",
    "health",
    "has_ammo",
    "n_enemies",
    "enemy_near",
    "enemy_sin",
    "enemy_cos",
    "enemy_in_cone",
    "exit_sin",
    "exit_cos",
    "exit_aligned",
    "med_present",
    "med_sin",
    "med_cos",
)
N_FEATURES = len(FEATURE_NAMES)


def _sc(bearing: float | None) -> tuple[float, float]:
    if bearing is None:
        return 0.0, 0.0
    r = math.radians(bearing)
    return math.sin(r), math.cos(r)


def featurize(state: GameState) -> list[float]:
    p = state.player
    enemies = [e for e in state.enemies if e.line_of_sight] or list(state.enemies)
    nearest = min(enemies, key=lambda e: e.distance) if enemies else None
    medkit = min(
        (k for k in state.pickups if k.type in ("medikit", "stimpack", "health")),
        key=lambda k: k.distance,
        default=None,
    )
    e_sin, e_cos = _sc(nearest.bearing if nearest else None)
    x_sin, x_cos = _sc(state.exit_bearing)
    m_sin, m_cos = _sc(medkit.bearing if medkit else None)
    return [
        1.0,
        p.health / 100.0,
        1.0 if sum(p.ammo.values()) > 0 else 0.0,
        min(len(state.enemies), 5) / 5.0,
        max(0.0, 1.0 - nearest.distance / 1000.0) if nearest else 0.0,
        e_sin,
        e_cos if nearest else 0.0,
        1.0 if nearest and abs(nearest.bearing) <= FIRE_CONE_DEG else 0.0,
        x_sin,
        x_cos,
        1.0 if state.exit_bearing is not None and abs(state.exit_bearing) <= 15.0 else 0.0,
        1.0 if medkit else 0.0,
        m_sin,
        m_cos if medkit else 0.0,
    ]
