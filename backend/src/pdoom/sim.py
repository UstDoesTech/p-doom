"""A small top-down arena in Python, the same rules as web/src/engine/mockWorld.ts.

It is for offline work: training rl_policy, and testing every player type without a browser. The
random numbers differ from the TypeScript version, so a seed gives a similar arena, not the same
one. Results here are never stored as Doom results.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Protocol

from .models import GameState, Intent

TIC_RATE = 35
TURN_RATE = 120.0
MOVE_SPEED = 200.0
STRAFE_SPEED = 160.0
EXIT_DISTANCE = 3000.0
EXIT_RADIUS = 80.0
ENEMY_COUNT = 8
VISIBLE_RANGE = 900.0
WAKE_RANGE = 900.0
ENEMY_SPEED = 60.0
ENEMY_STOP = 250.0
ATTACK_RANGE = 600.0
FIRE_RANGE = 800.0
FIRE_CONE = 10.0

INTENT_KEYS: dict[Intent, str] = {
    Intent.advance: "forward",
    Intent.retreat: "back",
    Intent.turn_left: "left",
    Intent.turn_right: "right",
    Intent.strafe_left: "strafe_left",
    Intent.strafe_right: "strafe_right",
    Intent.fire: "fire",
    Intent.use: "use",
}


def wrap180(deg: float) -> float:
    return ((deg + 180.0) % 360.0 + 360.0) % 360.0 - 180.0


@dataclass
class Enemy:
    x: float
    y: float
    hp: int = 20
    cooldown: float = 0.0


class MockWorld:
    def __init__(self, seed: int, level: str = "E1M1") -> None:
        self.seed, self.level = seed, level
        self.rand = random.Random(seed)
        self.tic = 0
        self.health = 100
        self.kills = 0
        self.ammo = 200
        self.x = self.y = 0.0
        self.angle = self.rand.random() * 360.0
        self.fire_cooldown = 0.0
        self.exit = (0.0, EXIT_DISTANCE)
        self.enemies: list[Enemy] = []
        for i in range(ENEMY_COUNT):
            along = (i + 1) / (ENEMY_COUNT + 1) * EXIT_DISTANCE * 0.9
            across = self.rand.random() * 600.0 - 300.0
            self.enemies.append(Enemy(across, along))

    def status(self) -> str:
        if self.health <= 0:
            return "died"
        if math.hypot(self.exit[0] - self.x, self.exit[1] - self.y) < EXIT_RADIUS:
            return "cleared"
        return "running"

    def bearing_to(self, tx: float, ty: float) -> float:
        return wrap180(self.angle - math.degrees(math.atan2(ty - self.y, tx - self.x)))

    def exit_distance(self) -> float:
        return math.hypot(self.exit[0] - self.x, self.exit[1] - self.y)

    def step(self, key: str | None, dt: float = 1.0 / TIC_RATE) -> None:
        if self.status() != "running":
            return
        self.tic += 1
        if key == "left":
            self.angle += TURN_RATE * dt
        if key == "right":
            self.angle -= TURN_RATE * dt
        self.angle %= 360.0
        rad = math.radians(self.angle)
        fx, fy = math.cos(rad), math.sin(rad)
        rx, ry = math.sin(rad), -math.cos(rad)
        mx = my = 0.0
        if key == "forward":
            mx, my = fx * MOVE_SPEED, fy * MOVE_SPEED
        elif key == "back":
            mx, my = -fx * MOVE_SPEED, -fy * MOVE_SPEED
        elif key == "strafe_right":
            mx, my = rx * STRAFE_SPEED, ry * STRAFE_SPEED
        elif key == "strafe_left":
            mx, my = -rx * STRAFE_SPEED, -ry * STRAFE_SPEED
        self.x += mx * dt
        self.y += my * dt

        self.fire_cooldown = max(0.0, self.fire_cooldown - dt)
        if key == "fire" and self.fire_cooldown == 0.0 and self.ammo > 0:
            self.ammo -= 1
            self.fire_cooldown = 0.3
            target = self._nearest_in_cone()
            if target and self.rand.random() < 0.7:
                target.hp -= 10
                if target.hp <= 0:
                    self.kills += 1

        for e in self.enemies:
            if e.hp <= 0:
                continue
            dx, dy = self.x - e.x, self.y - e.y
            dist = math.hypot(dx, dy)
            if dist >= WAKE_RANGE:
                continue
            if dist > ENEMY_STOP:
                e.x += dx / dist * ENEMY_SPEED * dt
                e.y += dy / dist * ENEMY_SPEED * dt
            e.cooldown = max(0.0, e.cooldown - dt)
            if dist < ATTACK_RANGE and e.cooldown == 0.0:
                e.cooldown = 1.0
                if self.rand.random() < 0.4:
                    self.health -= 5 + int(self.rand.random() * 6)
        self.health = max(self.health, 0)

    def _nearest_in_cone(self) -> Enemy | None:
        best, best_d = None, math.inf
        for e in self.enemies:
            if e.hp <= 0:
                continue
            d = math.hypot(e.x - self.x, e.y - self.y)
            if d > FIRE_RANGE or abs(self.bearing_to(e.x, e.y)) > FIRE_CONE:
                continue
            if d < best_d:
                best, best_d = e, d
        return best

    def state(self) -> GameState:
        seen = sorted(
            (
                (math.hypot(e.x - self.x, e.y - self.y), e)
                for e in self.enemies
                if e.hp > 0
            ),
            key=lambda t: t[0],
        )
        enemies = [
            {
                "type": "zombieman",
                "distance": round(d),
                "bearing": round(self.bearing_to(e.x, e.y), 1),
                "line_of_sight": True,
            }
            for d, e in seen
            if d <= VISIBLE_RANGE
        ][:5]
        return GameState.model_validate(
            {
                "tic": self.tic,
                "level": self.level,
                "seed": self.seed,
                "player": {
                    "health": self.health,
                    "armor": 0,
                    "weapon": "pistol",
                    "ammo": {"bullets": self.ammo},
                    "x": round(self.x),
                    "y": round(self.y),
                    "angle": round(self.angle, 1),
                },
                "enemies": enemies,
                "pickups": [],
                "exit_bearing": round(self.bearing_to(*self.exit), 1),
            }
        )


class Decider(Protocol):
    def decide(self, state: GameState, screenshot_b64: str | None = None): ...


@dataclass
class Result:
    outcome: str
    tics: int
    kills: int
    health: int


def play(
    player: Decider, seed: int, gap_tics: int = 35, max_tics: int = TIC_RATE * 120
) -> Result:
    """Paused-mode play: the world waits while the player decides, then holds the intent for
    gap_tics tics (about a second of game time)."""
    world = MockWorld(seed)
    while world.status() == "running" and world.tic < max_tics:
        decision = player.decide(world.state())
        key = INTENT_KEYS.get(decision.intent) if decision.intent else None
        for _ in range(gap_tics):
            world.step(key)
            if world.status() != "running" or world.tic >= max_tics:
                break
    status = world.status()
    outcome = status if status != "running" else "timeout"
    return Result(outcome, world.tic, world.kills, world.health)


def evaluate(player: Decider, seeds: range | list[int], **kw) -> dict[str, float]:
    results = [play(player, s, **kw) for s in seeds]
    n = len(results)
    return {
        "attempts": n,
        "clear_rate": sum(r.outcome == "cleared" for r in results) / n,
        "p_doom": sum(r.outcome == "died" for r in results) / n,
        "timeout_rate": sum(r.outcome == "timeout" for r in results) / n,
        "mean_kills": sum(r.kills for r in results) / n,
    }
