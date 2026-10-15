"""The baseline: if-then rules over the state JSON. Every other player has to beat this.

Rules run in priority order and the first that fires decides. The bot keeps a little memory per
attempt (the last position, health and what it was doing) to notice when it is stuck against a
door or wall, when it is being hit by something it cannot see, and to dodge while fighting. The
memory is keyed by level and seed and is dropped when the game tic runs backwards (a restart).

The Doom engine also reports the scenery: wall distances all round, what `use` would do right now
and a route to the exit through doors. The bot heads along the route, opens doors on the way,
presses the exit switch, and turns away from walls instead of walking into them. The mock arena
sends none of that, and then the bot falls back to the straight bearing to the exit.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from ..models import Decision, GameState, Intent, PlayerType

FIRE_CONE_DEG = 8.0
ENGAGE_RANGE = 700.0
RETREAT_HEALTH = 30
EXIT_ALIGN_DEG = 15.0

HEAL_BELOW = 60  # go and fetch health under this
HEALTH_RANGE = 700.0
AMMO_RANGE = 600.0
PICKUP_ALIGN_DEG = 20.0
SAFE_DISTANCE = 300.0  # no pickup detours with a visible enemy this close
MELEE_RANGE = 120.0
DODGE_RANGE = 450.0  # alternate fire and strafe against enemies this close
STUCK_MOVE = 6.0  # moved less than this since the last decision, while trying to advance
HIT_SEARCH_TURNS = 2
WALL_CLOSE = 48.0  # something this near dead ahead: stop walking at it
KEY_RANGE = 400.0
DOOR_WAIT = 2  # decisions to leave a door alone after pressing use: pressing again closes it
SKIP_PICKUPS = 8  # decisions to ignore pickups after getting stuck on the way to one


@dataclass
class Memory:
    tic: int = -1
    x: float = 0.0
    y: float = 0.0
    health: int = 100
    last: Intent | None = None
    stuck: int = 0
    dodge: int = 0
    search: int = 0
    flip: bool = False
    door_wait: int = 0
    skip_pickups: int = 0
    turn_dir: int = 0  # sticky turn away from a wall, so it does not dither: -1 left, 1 right
    rules: list[str] = field(default_factory=list)


class RuleBot:
    player_type = PlayerType.rule_bot

    def __init__(self) -> None:
        self._memory: dict[tuple[str, int], Memory] = {}
        self._lock = threading.Lock()

    def decide(self, state: GameState, screenshot_b64: str | None = None) -> Decision:
        with self._lock:
            mem = self._memory_for(state)
            intent, rule = self._choose(state, mem)
            self._remember(state, mem, intent)
        return Decision(intent=intent, raw_output={"rule": rule, "stuck": mem.stuck})

    # -- memory ----------------------------------------------------------------------------

    def _memory_for(self, state: GameState) -> Memory:
        key = (state.level, state.seed)
        mem = self._memory.get(key)
        if mem is None or state.tic < mem.tic:
            mem = self._memory[key] = Memory()
            if len(self._memory) > 256:  # abandoned attempts: keep the table small
                self._memory.pop(next(iter(self._memory)))
        return mem

    @staticmethod
    def _remember(state: GameState, mem: Memory, intent: Intent) -> None:
        mem.tic, mem.x, mem.y = state.tic, state.player.x, state.player.y
        mem.health, mem.last = state.player.health, intent

    # -- rules -----------------------------------------------------------------------------

    def _choose(self, state: GameState, mem: Memory) -> tuple[Intent, str]:
        p = state.player
        visible = [e for e in state.enemies if e.line_of_sight]
        nearest = min(visible, key=lambda e: e.distance, default=None)
        hurt_now = mem.tic >= 0 and p.health < mem.health
        total_ammo = sum(p.ammo.values()) if p.ammo else None
        out_of_ammo = total_ammo == 0

        moved = (p.x - mem.x) ** 2 + (p.y - mem.y) ** 2
        trying_to_move = mem.last in (
            Intent.advance, Intent.retreat, Intent.strafe_left, Intent.strafe_right
        ) or (mem.stuck > 0 and mem.last in (Intent.use, Intent.turn_left, Intent.turn_right))
        if mem.tic >= 0 and trying_to_move and moved < STUCK_MOVE**2:
            mem.stuck += 1
        else:
            mem.stuck = 0

        if mem.door_wait > 0:
            mem.door_wait -= 1
        if mem.skip_pickups > 0:
            mem.skip_pickups -= 1
        if mem.stuck >= 3:
            mem.skip_pickups = SKIP_PICKUPS

        if nearest and p.health < RETREAT_HEALTH and nearest.distance < ENGAGE_RANGE:
            return Intent.retreat, "low_health_retreat"

        if nearest and out_of_ammo and nearest.distance > MELEE_RANGE:
            # Nothing to shoot with: do not walk into a fight. Keep going for the exit.
            return self._navigate(state, mem, avoid_fight=True)

        if nearest:
            if abs(nearest.bearing) > FIRE_CONE_DEG:
                turn = Intent.turn_left if nearest.bearing < 0 else Intent.turn_right
                return turn, "face_enemy"
            # Lined up. Against something close, trade shots for steps sideways, so it has to
            # keep re-aiming. Strafe away from the side it is on.
            if nearest.distance < DODGE_RANGE and nearest.distance > MELEE_RANGE and hurt_now:
                mem.dodge += 1
                if mem.dodge % 2 == 1:
                    return (
                        Intent.strafe_left if nearest.bearing >= 0 else Intent.strafe_right,
                        "dodge",
                    )
            return Intent.fire, "enemy_in_cone"

        if hurt_now and mem.search < HIT_SEARCH_TURNS and not visible:
            # Something we cannot see is hurting us. Look around before carrying on.
            mem.search += 1
            mem.flip = not mem.flip
            return (Intent.turn_left if mem.flip else Intent.turn_right), "hit_by_unseen"
        if not hurt_now:
            mem.search = 0

        safe = nearest is None or nearest.distance > SAFE_DISTANCE
        if safe and mem.skip_pickups == 0:
            pickup = self._pickup_target(state, out_of_ammo)
            if pickup is not None:
                bearing, rule = pickup
                if abs(bearing) > PICKUP_ALIGN_DEG:
                    return (Intent.turn_left if bearing < 0 else Intent.turn_right), f"face_{rule}"
                return self._advance_or_unstick(mem, rule, state, bearing)

        return self._navigate(state, mem)

    def _pickup_target(self, state: GameState, out_of_ammo: bool) -> tuple[float, str] | None:
        p = state.player
        wants: list[tuple[float, float, str]] = []
        for item in state.pickups:
            if item.type == "health" and p.health < HEAL_BELOW and item.distance <= HEALTH_RANGE:
                wants.append((item.distance, item.bearing, "health"))
            elif item.type == "ammo" and out_of_ammo and item.distance <= AMMO_RANGE:
                wants.append((item.distance, item.bearing, "ammo"))
            elif item.type == "weapon" and item.distance <= AMMO_RANGE / 2:
                wants.append((item.distance, item.bearing, "weapon"))
            elif item.type == "armor" and p.armor < 50 and item.distance <= AMMO_RANGE / 2:
                wants.append((item.distance, item.bearing, "armor"))
            elif item.type == "key" and item.distance <= KEY_RANGE:
                wants.append((item.distance, item.bearing, "key"))
        if not wants:
            return None
        _, bearing, rule = min(wants)
        return bearing, rule

    def _navigate(
        self, state: GameState, mem: Memory, avoid_fight: bool = False
    ) -> tuple[Intent, str]:
        """Head for the exit: along the route when the engine gives one, opening doors and pressing
        the exit switch, else straight at the exit bearing."""
        route, target = state.route, state.use_target
        suffix = "advance_to_exit" if avoid_fight else "advance"

        if target is not None and not target.locked:
            if target.kind == "exit":
                return Intent.use, "press_exit"
            if target.kind == "door" and not target.open and mem.door_wait == 0:
                # Only a door on the way, or any door when nothing says where to go.
                if route is None or route.via == "door" or abs(route.bearing) < 70:
                    mem.door_wait = DOOR_WAIT
                    return Intent.use, "open_door"
        if mem.door_wait > 0 and mem.stuck == 0:
            return Intent.advance, "through_door"

        bearing = route.bearing if route is not None else state.exit_bearing
        if bearing is not None and abs(bearing) > EXIT_ALIGN_DEG and mem.stuck == 0:
            return (Intent.turn_left if bearing < 0 else Intent.turn_right), "face_exit"
        return self._advance_or_unstick(mem, suffix, state, bearing)

    @staticmethod
    def _wall_turn(state: GameState, mem: Memory, bearing: float | None) -> Intent:
        """Which way to turn away from a wall dead ahead: towards where there is more room, with a
        lean towards the goal, and sticking to it until the way is clear."""
        w = state.walls
        if mem.turn_dir == 0:
            left, right = w.ahead_left + w.left * 0.5, w.ahead_right + w.right * 0.5
            if bearing is not None:
                (left, right) = (left + 30, right) if bearing < 0 else (left, right + 30)
            mem.turn_dir = -1 if left > right else 1
        return Intent.turn_left if mem.turn_dir < 0 else Intent.turn_right

    def _advance_or_unstick(
        self, mem: Memory, rule: str, state: GameState | None = None, bearing: float | None = None
    ) -> tuple[Intent, str]:
        """Walk forward. With wall distances, turn away from a wall dead ahead. If walking has not
        moved us anyway, work through the usual causes in turn: a door or switch that needs `use`,
        a corner to slide round, a wall to turn away from."""
        walls = state.walls if state is not None else None
        if walls is not None and mem.stuck == 0:
            if walls.ahead < WALL_CLOSE:
                return self._wall_turn(state, mem, bearing), "wall_turn"
            mem.turn_dir = 0
        if mem.stuck == 0:
            return Intent.advance, rule
        step = (mem.stuck - 1) % 6
        if step in (0, 3):
            return Intent.use, "stuck_use"
        if step == 1:
            return Intent.strafe_left, "stuck_slide_left"
        if step == 2:
            return Intent.strafe_right, "stuck_slide_right"
        return (Intent.turn_left if step == 4 else Intent.turn_right), "stuck_turn"
