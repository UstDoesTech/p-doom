"""What ai_decide's player remembers between calls, and how it tells the model.

ai_decide has no memory: each call sees one frame. Sent only that, it walks into the same barrel
every second and never learns why nothing moves (session c276eef0). This keeps a short history of
one session and boils it down to a small `context` object that is sent beside the state:

  where it is   explored_areas, area_visits (how often this patch of map has been entered),
                seconds_here
  where it's been  trail (earlier positions as distance and bearing from here), moved_5s,
                moved_15s, progress_5s, progress_15s (units closer to the exit; negative is farther)
  what it did   recent (the last few actions, how far each really moved, and whether it was blocked)
  what's around  enemies_in_sight, threat (nearest enemy, and whether it is closing), damage_3s,
                damage_10s, last_hit_s, ammo_spent_5s
  and a one-line `summary` in words, because a model reads a sentence more reliably than a number.

Everything is in the player's own frame: bearings are degrees from where it faces, positive to the
right, the same as the rest of the state.

Calls overlap when the browser sends several at once, so answers land late and out of order. Each
action is therefore filed against the game tic at which it probably took effect (the tic of the
state it answered plus how long the answer took), and movement is judged against that timeline.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from ..models import GameState, Intent

TIC_RATE = 35
CELL = 128.0  # side of a map patch, in units, for counting visits
REVISIT_AFTER_TICS = 3 * TIC_RATE  # away this long, and coming back is a new visit
SAMPLE_CAP = 900
ACT_CAP = 80
TRAIL_SECONDS = (2, 5, 10, 20, 40)
HOLD_IF_NONE = TIC_RATE // 2  # how long an intent with no hold is assumed to run for
RECENT = 4

MOVING = (Intent.advance, Intent.retreat, Intent.strafe_left, Intent.strafe_right)
STUCK_UNITS_PER_TIC = 2.0  # asked to walk, it should cover at least this per tic held
STUCK_SPAN_TICS = 24  # judge walking over at least this long (calls come every 10 to 15 tics)
STUCK_MIN_EFFORT = 8  # and only when it was asked to walk for at least this many tics of it


def wrap180(deg: float) -> float:
    return (deg + 180.0) % 360.0 - 180.0


def bearing_of(angle: float, dx: float, dy: float) -> float:
    """Bearing of an offset from a player facing `angle` (degrees, counter-clockwise from east).
    Positive is to the right, as in the engine."""
    return wrap180(angle - math.degrees(math.atan2(dy, dx)))


@dataclass(slots=True)
class Sample:
    tic: int
    x: float
    y: float
    health: int
    ammo: int
    nearest: float | None  # distance to the nearest enemy in sight
    to_go: float | None  # route length still to walk


@dataclass(slots=True)
class Act:
    tic: int  # when it probably took effect
    intent: Intent
    hold: int | None
    x: float  # where the player stood when the state it answered was read
    y: float
    overridden: bool = False


@dataclass(slots=True)
class Patch:
    visits: int = 1
    entered: int = 0
    last_seen: int = 0


@dataclass
class Memory:
    samples: deque[Sample] = field(default_factory=lambda: deque(maxlen=SAMPLE_CAP))
    acts: deque[Act] = field(default_factory=lambda: deque(maxlen=ACT_CAP))
    patches: dict[tuple[int, int], Patch] = field(default_factory=dict)
    first_tic: int = -1
    here: tuple[int, int] | None = None

    @property
    def last_tic(self) -> int:
        return self.samples[-1].tic if self.samples else -1

    # ---- writing ------------------------------------------------------------------------

    def observe(self, state: GameState) -> Sample | None:
        """File this state. Returns None for one that is not newer than the last (answers and
        requests can arrive out of order), which then adds nothing to the history."""
        if state.tic <= self.last_tic:
            return None
        p = state.player
        seen = [e.distance for e in state.enemies if e.line_of_sight]
        route = state.route
        to_go = None
        if route is not None:
            to_go = route.remaining if route.remaining > 0 else route.distance
        sample = Sample(
            tic=state.tic,
            x=p.x,
            y=p.y,
            health=p.health,
            ammo=sum(p.ammo.values()),
            nearest=min(seen, default=None),
            to_go=to_go,
        )
        if self.first_tic < 0:
            self.first_tic = state.tic
        self.samples.append(sample)

        cell = (math.floor(p.x / CELL), math.floor(p.y / CELL))
        patch = self.patches.get(cell)
        if patch is None:
            if len(self.patches) < 4000:
                self.patches[cell] = Patch(1, state.tic, state.tic)
        else:
            if cell != self.here and state.tic - patch.last_seen > REVISIT_AFTER_TICS:
                patch.visits += 1
                patch.entered = state.tic
            patch.last_seen = state.tic
        self.here = cell
        return sample

    def record(self, state: GameState, intent: Intent, hold: int | None, latency_s: float,
               overridden: bool = False) -> None:
        took_effect = state.tic + round(max(0.0, latency_s) * TIC_RATE)
        self.acts.append(
            Act(took_effect, intent, hold, state.player.x, state.player.y, overridden)
        )

    # ---- reading ------------------------------------------------------------------------

    def baseline(self) -> Sample | None:
        """The newest sample at least STUCK_SPAN_TICS before the latest one, else the one before
        it: where to measure walking from."""
        if len(self.samples) < 2:
            return None
        now = self.samples[-1].tic
        history = list(self.samples)
        for s in reversed(history[:-1]):
            if s.tic <= now - STUCK_SPAN_TICS:
                return s
        return history[-2]

    def moving_tics(self, since: int, until: int) -> int:
        """Game tics in (since, until] during which a walking intent was probably in force."""
        if until <= since:
            return 0
        acts = sorted(self.acts, key=lambda a: a.tic)
        total = 0
        for i, act in enumerate(acts):
            if act.intent not in MOVING:
                continue
            end = acts[i + 1].tic if i + 1 < len(acts) else until
            if act.hold is not None:
                end = min(end, act.tic + act.hold)
            else:
                end = min(end, act.tic + 2 * HOLD_IF_NONE)
            lo, hi = max(act.tic, since), min(end, until)
            if hi > lo:
                total += hi - lo
        return total

    def _at(self, tic: int) -> Sample | None:
        """The sample nearest in time to `tic`, or None if the history does not reach back."""
        if not self.samples or self.samples[0].tic > tic + TIC_RATE:
            return None
        return min(self.samples, key=lambda s: abs(s.tic - tic))

    def context(self, state: GameState, stuck: int = 0, jam: bool = False) -> dict[str, Any]:
        p = state.player
        now = state.tic
        out: dict[str, Any] = {"seconds_played": max(0, round((now - self.first_tic) / TIC_RATE))
                               if self.first_tic >= 0 else 0}

        patch = self.patches.get((math.floor(p.x / CELL), math.floor(p.y / CELL)))
        out["explored_areas"] = max(1, len(self.patches))
        out["area_visits"] = patch.visits if patch else 1
        out["seconds_here"] = round((now - patch.entered) / TIC_RATE) if patch else 0

        trail: list[dict[str, Any]] = []
        used: set[int] = set()
        for secs in TRAIL_SECONDS:
            s = self._at(now - secs * TIC_RATE)
            if s is None or s.tic in used or s.tic >= now:
                continue
            used.add(s.tic)
            dx, dy = s.x - p.x, s.y - p.y
            trail.append({
                "seconds_ago": round((now - s.tic) / TIC_RATE),
                "distance": round(math.hypot(dx, dy)),
                "bearing": round(bearing_of(p.angle, dx, dy)),
            })
        if trail:
            out["trail"] = trail

        for secs in (5, 15):
            s = self._at(now - secs * TIC_RATE)
            if s is not None and s.tic < now:
                out[f"moved_{secs}s"] = round(math.hypot(p.x - s.x, p.y - s.y))
                cur = self.samples[-1].to_go if self.samples else None
                if cur is not None and s.to_go is not None:
                    out[f"progress_{secs}s"] = round(s.to_go - cur)

        out["stuck"] = stuck
        out["jammed"] = jam

        recent = self._recent(state)
        if recent:
            out["recent"] = recent

        out.update(self._around(state))
        out["summary"] = self._summary(out, state)
        return out

    def _recent(self, state: GameState) -> list[dict[str, Any]]:
        acts = sorted(self.acts, key=lambda a: a.tic)[-RECENT:]
        p = state.player
        rows: list[dict[str, Any]] = []
        for i, act in enumerate(acts):
            nx, ny = (acts[i + 1].x, acts[i + 1].y) if i + 1 < len(acts) else (p.x, p.y)
            moved = math.hypot(nx - act.x, ny - act.y)
            row: dict[str, Any] = {
                "seconds_ago": max(0, round((state.tic - act.tic) / TIC_RATE, 1)),
                "intent": act.intent.value,
                "moved": round(moved),
            }
            if act.intent in MOVING:
                span = (acts[i + 1].tic if i + 1 < len(acts) else state.tic) - act.tic
                span = min(span, act.hold) if act.hold else span
                row["blocked"] = span >= 6 and moved < STUCK_UNITS_PER_TIC * span
            if act.overridden:
                row["overridden"] = True
            rows.append(row)
        return rows

    def _around(self, state: GameState) -> dict[str, Any]:
        now = state.tic
        out: dict[str, Any] = {}
        seen = [e for e in state.enemies if e.line_of_sight]
        out["enemies_in_sight"] = len(seen)
        if seen:
            near = min(seen, key=lambda e: e.distance)
            threat: dict[str, Any] = {
                "type": near.type,
                "distance": round(near.distance),
                "bearing": round(near.bearing),
            }
            before = self._at(now - TIC_RATE)
            if before is not None and before.nearest is not None and before.tic < now:
                dt = (now - before.tic) / TIC_RATE
                threat["closing_per_s"] = round((before.nearest - near.distance) / dt)
            out["threat"] = threat

        def drops(seconds: int) -> int:
            window = [s for s in self.samples if s.tic >= now - seconds * TIC_RATE]
            pairs = zip(window, window[1:], strict=False)
            return sum(max(0, a.health - b.health) for a, b in pairs)

        out["damage_3s"] = drops(3)
        out["damage_10s"] = drops(10)
        last_hit = None
        history = list(self.samples)
        for a, b in zip(reversed(history[:-1]), reversed(history), strict=False):
            if b.health < a.health:
                last_hit = round((now - b.tic) / TIC_RATE)
                break
        if last_hit is not None:
            out["last_hit_s"] = last_hit
        window = [s for s in self.samples if s.tic >= now - 5 * TIC_RATE]
        spent = sum(max(0, a.ammo - b.ammo) for a, b in zip(window, window[1:], strict=False))
        out["ammo_spent_5s"] = spent
        return out

    @staticmethod
    def _summary(c: dict[str, Any], state: GameState) -> str:
        bits: list[str] = []
        visits = c.get("area_visits", 1)
        if visits > 1:
            bits.append(f"You have been in this area {visits} times already.")
        if c.get("jammed"):
            bits.append("You have gone nowhere for several seconds: something is in the way.")
        elif c.get("stuck"):
            bits.append("Your last moves did not get you anywhere: you are blocked.")
        else:
            blocked = [r for r in c.get("recent", []) if r.get("blocked")]
            if blocked:
                bits.append(f"{blocked[-1]['intent']} was just blocked.")
        p15 = c.get("progress_15s")
        if p15 is not None:
            if p15 <= 10 and c.get("seconds_played", 0) >= 15:
                bits.append("No progress toward the exit in 15 s.")
            elif p15 > 10:
                bits.append(f"{p15} units closer to the exit than 15 s ago.")
        threat = c.get("threat")
        if threat:
            closing = threat.get("closing_per_s")
            tail = f", closing at {closing} a second" if closing and closing > 20 else ""
            bits.append(f"{threat['type']} in sight {threat['distance']} away{tail}.")
        if c.get("damage_3s"):
            bits.append(f"Lost {c['damage_3s']} health in the last 3 s.")
        return " ".join(bits) or "Nothing unusual."
