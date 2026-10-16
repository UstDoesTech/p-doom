"""The baseline: if-then rules over the state JSON. Every other player has to beat this.

Rules run in priority order and the first that fires decides. The bot keeps a little memory per
attempt (the last position, health and what it was doing) to notice when it is stuck against a
door or wall, when it is being hit by something it cannot see, and to dodge while fighting. The
memory is keyed by level and seed and is dropped when the game tic runs backwards (a restart).

The Doom engine also reports the scenery: wall distances all round, what `use` would do right now
and a route to the exit through doors. The bot heads along the route, opens doors on the way,
presses the exit switch, and goes round a wall that is in the way instead of walking into it. The
mock arena sends none of that, and then the bot falls back to the straight bearing to the exit.

Turns and steps say how many game tics to hold the key (`hold_tics`). A key held for a whole
decision gap turns about 90 degrees, which overshoots any target closer than that.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field

from ..models import Decision, GameState, Intent, PlayerType, Walls

FIRE_CONE_DEG = 8.0
ENGAGE_RANGE = 700.0
RETREAT_HEALTH = 30
EXIT_ALIGN_DEG = 12.0

HEAL_BELOW = 60  # go and fetch health under this
HEALTH_RANGE = 700.0
AMMO_RANGE = 600.0
PICKUP_ALIGN_DEG = 20.0
SAFE_DISTANCE = 300.0  # no pickup detours with a visible enemy this close
MELEE_RANGE = 120.0
DODGE_RANGE = 450.0  # alternate fire and strafe against enemies this close
HIT_SEARCH_TURNS = 2
KEY_RANGE = 400.0

STUCK_UNITS_PER_TIC = 2.0  # walking should cover at least this per tic held, or it is stuck
TURN_SLOW_TICS = 6  # a held turn key turns slowly for this many tics, then fast
TURN_SLOW_DEG = 1.75
TURN_FAST_DEG = 3.5
WALK_PER_TIC = 7.0  # map units walked per tic, a little under the real speed
MAX_HOLD = 30
DOOR_WAIT_TICS = 70  # leave a door alone after pressing use: pressing again closes it
SKIP_PICKUP_TICS = 280  # ignore pickups this long after getting stuck on the way to one
WALL_CLOSE = 48.0  # something this near dead ahead: do not walk at it
CLEAR_AHEAD = 96.0  # room needed along a heading before the bot commits to walking it
ANCHOR_TICS = 140  # not having got anywhere in this long (while free to move) means being jammed
ANCHOR_UNITS = 48.0
ESCAPE_STEPS = 5
PICKUP_GIVE_UP_TICS = 150  # chasing one pickup this long without getting it: leave it
DETOUR_TICS = 60  # stay on a chosen way round an obstacle this long
RAY_BEARINGS = {
    "ahead": 0.0, "ahead_right": 45.0, "right": 90.0, "behind_right": 135.0, "behind": 180.0,
    "behind_left": -135.0, "left": -90.0, "ahead_left": -45.0,
}


def turn_hold(bearing: float) -> int:
    """Game tics to hold a turn key to turn through this many degrees."""
    a = abs(bearing)
    slow_total = TURN_SLOW_TICS * TURN_SLOW_DEG
    if a <= slow_total:
        tics = math.ceil(a / TURN_SLOW_DEG)
    else:
        tics = TURN_SLOW_TICS + math.ceil((a - slow_total) / TURN_FAST_DEG)
    return max(1, min(MAX_HOLD, tics))


def walk_hold(distance: float) -> int:
    return max(2, min(14, math.ceil(distance / WALK_PER_TIC)))


def angle_diff(a: float, b: float) -> float:
    d = (a - b + 180.0) % 360.0 - 180.0
    return d


def clearance(walls: Walls, bearing: float) -> float:
    """Room along a heading. On one of the eight rays that ray; between two, the smaller of them."""
    ranked = sorted(RAY_BEARINGS.items(), key=lambda kv: abs(angle_diff(bearing, kv[1])))
    nearest = ranked[0]
    if abs(angle_diff(bearing, nearest[1])) < 10.0:
        return getattr(walls, nearest[0])
    return min(getattr(walls, name) for name, _ in ranked[:2])


@dataclass
class Memory:
    tic: int = -1
    x: float = 0.0
    y: float = 0.0
    health: int = 100
    last: Intent | None = None
    last_hold: int | None = None
    stuck: int = 0
    dodge: int = 0
    search: int = 0
    flip: bool = False
    door_until: int = -1  # game tic until which a door just pressed is left alone
    skip_until: int = -1  # game tic until which pickups are ignored
    detour: float | None = None  # a world heading chosen to get round an obstacle
    detour_until: int = -1
    anchor_x: float = 0.0  # where the bot was ANCHOR_TICS ago, to tell a jam from slow progress
    anchor_y: float = 0.0
    anchor_tic: int = -1
    pickup_since: int = -1  # game tic the current pickup chase began
    escape: int = 0  # steps left of the get-out-of-a-jam manoeuvre
    escapes: int = 0
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
            intent, rule, hold = self._choose(state, mem)
            self._remember(state, mem, intent, hold)
        return Decision(
            intent=intent, hold_tics=hold, raw_output={"rule": rule, "stuck": mem.stuck}
        )

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
    def _remember(state: GameState, mem: Memory, intent: Intent, hold: int | None) -> None:
        mem.tic, mem.x, mem.y = state.tic, state.player.x, state.player.y
        mem.health, mem.last, mem.last_hold = state.player.health, intent, hold

    # -- rules -----------------------------------------------------------------------------

    def _choose(self, state: GameState, mem: Memory) -> tuple[Intent, str, int | None]:
        p = state.player
        visible = [e for e in state.enemies if e.line_of_sight]
        nearest = min(visible, key=lambda e: e.distance, default=None)
        hurt_now = mem.tic >= 0 and p.health < mem.health
        total_ammo = sum(p.ammo.values()) if p.ammo else None
        out_of_ammo = total_ammo == 0

        self._update_stuck(state, mem)
        if mem.stuck >= 3:
            mem.skip_until = state.tic + SKIP_PICKUP_TICS

        if nearest and p.health < RETREAT_HEALTH and nearest.distance < ENGAGE_RANGE:
            return Intent.retreat, "low_health_retreat", None

        if nearest and out_of_ammo and nearest.distance > MELEE_RANGE:
            # Nothing to shoot with: do not walk into a fight. Keep going for the exit.
            return self._navigate(state, mem, avoid_fight=True)

        if nearest:
            if abs(nearest.bearing) > FIRE_CONE_DEG:
                turn = Intent.turn_left if nearest.bearing < 0 else Intent.turn_right
                return turn, "face_enemy", turn_hold(nearest.bearing)
            # Lined up. Against something close, trade shots for steps sideways, so it has to
            # keep re-aiming. Strafe away from the side it is on.
            if nearest.distance < DODGE_RANGE and nearest.distance > MELEE_RANGE and hurt_now:
                mem.dodge += 1
                if mem.dodge % 2 == 1:
                    return (
                        Intent.strafe_left if nearest.bearing >= 0 else Intent.strafe_right,
                        "dodge",
                        None,
                    )
            return Intent.fire, "enemy_in_cone", None

        if hurt_now and mem.search < HIT_SEARCH_TURNS and not visible:
            # Something we cannot see is hurting us. Look around before carrying on.
            mem.search += 1
            mem.flip = not mem.flip
            return (Intent.turn_left if mem.flip else Intent.turn_right), "hit_by_unseen", 12
        if not hurt_now:
            mem.search = 0

        safe = nearest is None or nearest.distance > SAFE_DISTANCE
        if safe and state.tic >= mem.skip_until:
            pickup = self._pickup_target(state, out_of_ammo)
            if pickup is None:
                mem.pickup_since = -1
            elif mem.pickup_since < 0:
                mem.pickup_since = state.tic
            elif state.tic - mem.pickup_since > PICKUP_GIVE_UP_TICS:
                mem.skip_until = state.tic + SKIP_PICKUP_TICS
                mem.pickup_since = -1
                pickup = None
            if pickup is not None:
                bearing, rule, distance = pickup
                if abs(bearing) > PICKUP_ALIGN_DEG:
                    return (
                        (Intent.turn_left if bearing < 0 else Intent.turn_right),
                        f"face_{rule}",
                        turn_hold(bearing),
                    )
                return self._go(state, mem, rule, bearing, distance)

        return self._navigate(state, mem)

    @staticmethod
    def _update_stuck(state: GameState, mem: Memory) -> None:
        """Count decisions in a row where walking did not get anywhere."""
        p = state.player
        moved = math.hypot(p.x - mem.x, p.y - mem.y)
        elapsed = state.tic - mem.tic
        trying_to_move = mem.last in (
            Intent.advance, Intent.retreat, Intent.strafe_left, Intent.strafe_right
        ) or (mem.stuck > 0 and mem.last in (Intent.use, Intent.turn_left, Intent.turn_right))
        if mem.last_hold is not None:
            elapsed = min(elapsed, mem.last_hold)
        expected = max(2.0, STUCK_UNITS_PER_TIC * elapsed) if elapsed > 0 else 6.0
        if mem.tic >= 0 and trying_to_move and moved < expected:
            mem.stuck += 1
        else:
            mem.stuck = 0

    def _pickup_target(
        self, state: GameState, out_of_ammo: bool
    ) -> tuple[float, str, float] | None:
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
        distance, bearing, rule = min(wants)
        return bearing, rule, distance

    def _navigate(
        self, state: GameState, mem: Memory, avoid_fight: bool = False
    ) -> tuple[Intent, str, int | None]:
        """Head for the exit: along the route when the engine gives one, opening doors and pressing
        the exit switch, else straight at the exit bearing."""
        route, target = state.route, state.use_target
        rule = "advance_to_exit" if avoid_fight else "advance"

        if target is not None and not target.locked:
            if target.kind == "exit":
                return Intent.use, "press_exit", None
            if target.kind == "door" and not target.open and state.tic >= mem.door_until:
                # Only a door on the way, or any door when nothing says where to go.
                if route is None or route.via == "door" or abs(route.bearing) < 70:
                    mem.door_until = state.tic + DOOR_WAIT_TICS
                    return Intent.use, "open_door", None
        if state.tic < mem.door_until and mem.stuck == 0:
            return Intent.advance, "through_door", 6

        jam = self._escape(state, mem)
        if jam is not None:
            return jam

        if route is not None:
            bearing, distance = route.bearing, route.distance
            if route.via in ("door", "exit"):
                distance = max(distance - 40.0, 8.0)  # stop at the door, not in it
        else:
            bearing, distance = state.exit_bearing, 9999.0
        return self._go(state, mem, rule, bearing, distance, trusted=route is not None)

    @staticmethod
    def _escape(state: GameState, mem: Memory) -> tuple[Intent, str, int | None] | None:
        """Last resort for a jam the other rules cannot see: no real progress for a while, so back
        out, turn a different way each time, and walk off before trying the route again."""
        p = state.player
        if mem.escape == 0:
            if mem.anchor_tic < 0 or state.tic - mem.anchor_tic >= ANCHOR_TICS:
                stuck_here = (
                    mem.anchor_tic >= 0
                    and math.hypot(p.x - mem.anchor_x, p.y - mem.anchor_y) < ANCHOR_UNITS
                )
                mem.anchor_x, mem.anchor_y, mem.anchor_tic = p.x, p.y, state.tic
                if stuck_here:
                    mem.escape = ESCAPE_STEPS
                    mem.escapes += 1
                    mem.detour = None
            if mem.escape == 0:
                return None
        step = ESCAPE_STEPS - mem.escape
        mem.escape -= 1
        mem.anchor_x, mem.anchor_y, mem.anchor_tic = p.x, p.y, state.tic
        if step == 0:
            return Intent.retreat, "escape_back", 10
        if step == 1:
            turn = Intent.turn_left if mem.escapes % 2 else Intent.turn_right
            return turn, "escape_turn", turn_hold(60.0 + 40.0 * (mem.escapes % 3))
        return Intent.advance, "escape_walk", 14

    def _go(
        self,
        state: GameState,
        mem: Memory,
        rule: str,
        bearing: float | None,
        distance: float,
        trusted: bool = False,
    ) -> tuple[Intent, str, int | None]:
        """Walk towards a bearing: turn to face it, then step. With wall distances, go round
        whatever is in the way."""
        walls = state.walls
        if bearing is None:
            return self._advance_or_unstick(state, mem, rule, None, 9999.0)

        # A route waypoint is picked by the engine as the furthest point in a clear straight line,
        # so its direction is walkable even when the wall rays beside us look cramped.
        if walls is not None and mem.stuck == 0 and not trusted:
            facing = state.player.angle
            heading, why = self._heading(walls, mem, state.tic, facing, bearing, distance)
            if why is not None:
                rule = why
            bearing = heading

        if abs(bearing) > EXIT_ALIGN_DEG and mem.stuck == 0:
            turn = Intent.turn_left if bearing < 0 else Intent.turn_right
            why = "exit" if rule.startswith("advance") else rule
            return turn, "face_" + why, turn_hold(bearing)
        return self._advance_or_unstick(state, mem, rule, bearing, distance, trusted)

    @staticmethod
    def _heading(
        walls: Walls, mem: Memory, tic: int, facing: float, bearing: float, distance: float
    ) -> tuple[float, str | None]:
        """The bearing to actually walk. The goal's own bearing when there is room that way, else
        the most open direction closest to it. A way round is kept for a while, as a heading in the
        world rather than a bearing, so turning towards it does not make the bot change its mind."""
        if mem.detour is not None and tic < mem.detour_until:
            return angle_diff(facing, mem.detour), "detour"
        mem.detour = None
        need = min(distance, CLEAR_AHEAD)
        if clearance(walls, bearing) >= need:
            return bearing, None
        best, best_score = None, -1e9
        for name, b in RAY_BEARINGS.items():
            if name == "behind":
                continue
            room = clearance(walls, b)
            if room < CLEAR_AHEAD:
                continue
            score = min(room, 250.0) - 1.5 * abs(angle_diff(b, bearing))
            if score > best_score:
                best, best_score = b, score
        if best is None:
            best = 150.0  # boxed in: turn right round
        mem.detour, mem.detour_until = facing - best, tic + DETOUR_TICS
        return best, "detour"

    def _advance_or_unstick(
        self,
        state: GameState,
        mem: Memory,
        rule: str,
        bearing: float | None,
        distance: float,
        trusted: bool = False,
    ) -> tuple[Intent, str, int | None]:
        """Walk forward. If that has not moved us, work through the usual causes in turn: a door
        or switch that needs `use`, a corner to slide round, a wall to turn away from."""
        walls = state.walls
        if mem.stuck == 0:
            if walls is not None:
                if walls.ahead < WALL_CLOSE and not trusted:
                    return self._wall_turn(walls, mem, bearing), "wall_turn", 8
                mem.turn_dir = 0
                room = min(distance, walls.ahead - 24.0)
                return Intent.advance, rule, walk_hold(room)
            return Intent.advance, rule, None
        step = (mem.stuck - 1) % 6
        if step == 0:
            return Intent.use, "stuck_use", None
        if step == 3:
            return Intent.retreat, "stuck_back_off", 8  # back out of a corner, then try again
        if step == 1:
            return Intent.strafe_left, "stuck_slide_left", 6
        if step == 2:
            return Intent.strafe_right, "stuck_slide_right", 6
        return (
            (Intent.turn_left if step == 4 else Intent.turn_right),
            "stuck_turn",
            10,
        )

    @staticmethod
    def _wall_turn(walls: Walls, mem: Memory, bearing: float | None) -> Intent:
        """Which way to turn away from a wall dead ahead: towards where there is more room, with a
        lean towards the goal, and sticking to it until the way is clear."""
        if mem.turn_dir == 0:
            left = walls.ahead_left + walls.left * 0.5
            right = walls.ahead_right + walls.right * 0.5
            if bearing is not None:
                if bearing < 0:
                    left += 30
                else:
                    right += 30
            mem.turn_dir = -1 if left > right else 1
        return Intent.turn_left if mem.turn_dir < 0 else Intent.turn_right
