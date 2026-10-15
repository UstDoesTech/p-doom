"""An offline stand-in for the Doom engine, built from the shareware WAD, to test navigation.

It reads a level's map and reproduces what engine/pdoom_state.c reports about the scenery (walls,
interactables, use target, route), and moves a player through the map with simple collision, doors
and the exit switch. There are no monsters and no pickups. It exists so a player's navigation can be
run end to end in seconds, without a browser. The geometry code mirrors the C file; change both
together. Needs the WAD: set PDOOM_WAD to the path of doom1.wad.
"""

from __future__ import annotations

import heapq
import math
import struct
from dataclasses import dataclass, field

FRAC = 1.0
PLAYER_HEIGHT = 56.0
STEP_HEIGHT = 24.0
RAY_MAX = 512.0
USE_RANGE = 64.0
RADIUS = 16.0
ML_BLOCKING = 1

WALL_BEARINGS = [0, 45, 90, 135, 180, -135, -90, -45]
WALL_NAMES = [
    "ahead", "ahead_right", "right", "behind_right", "behind", "behind_left", "left", "ahead_left",
]

DOORS = {1, 26, 27, 28, 31, 32, 33, 34, 117, 118}
DOOR_KEY = {26: 1, 32: 1, 27: 2, 34: 2, 28: 3, 33: 3}
SWITCHES = {
    7, 9, 14, 15, 18, 20, 21, 23, 29, 41, 42, 43, 45, 49, 50, 55, 60, 61, 62, 63, 64, 65, 66, 67,
    68, 69, 70, 71, 99, 101, 102, 103, 111, 112, 113, 114, 115, 116, 122, 123, 127, 131, 132, 133,
    134, 135, 136, 137, 138, 139, 140,
}
EXIT_USE = {11, 51}
EXIT_ANY = {11, 51, 52, 124}


def kind_of(special: int) -> str | None:
    if special in EXIT_USE:
        return "exit"
    if special in DOORS:
        return "door"
    if special in SWITCHES:
        return "switch"
    return None


@dataclass
class Sector:
    floor: float
    ceil: float
    lines: list[Line] = field(default_factory=list)
    cx: float = 0.0
    cy: float = 0.0
    index: int = 0


@dataclass
class Line:
    x1: float
    y1: float
    x2: float
    y2: float
    flags: int
    special: int
    front: Sector | None
    back: Sector | None
    index: int = 0

    @property
    def dx(self) -> float:
        return self.x2 - self.x1

    @property
    def dy(self) -> float:
        return self.y2 - self.y1

    def side(self, x: float, y: float) -> int:
        """0 for the front (right) side, as P_PointOnLineSide."""
        return 0 if self.dx * (y - self.y1) - self.dy * (x - self.x1) < 0 else 1


@dataclass
class Door:
    sector: Sector
    direction: int  # 1 opening, -1 closing, 0 waiting
    top: float
    wait: int = 0


class Level:
    def __init__(self, wad_path: str, name: str = "E1M1") -> None:
        data = open(wad_path, "rb").read()
        _, n, diroff = struct.unpack_from("<4sii", data, 0)
        order: list[str] = []
        for i in range(n):
            _pos, _size, raw = struct.unpack_from("<ii8s", data, diroff + 16 * i)
            order.append(raw.rstrip(b"\0").decode())
        start = order.index(name)
        found: dict[str, bytes] = {}
        for i in range(start + 1, min(start + 11, n)):
            pos, size, raw = struct.unpack_from("<ii8s", data, diroff + 16 * i)
            found[raw.rstrip(b"\0").decode()] = data[pos : pos + size]

        verts = [struct.unpack_from("<hh", found["VERTEXES"], 4 * i) for i in range(len(found["VERTEXES"]) // 4)]
        self.sectors: list[Sector] = []
        for i in range(len(found["SECTORS"]) // 26):
            fl, ce = struct.unpack_from("<hh", found["SECTORS"], 26 * i)
            self.sectors.append(Sector(float(fl), float(ce), index=i))
        sides = [struct.unpack_from("<hh8s8s8sh", found["SIDEDEFS"], 30 * i)[5] for i in range(len(found["SIDEDEFS"]) // 30)]
        self.lines: list[Line] = []
        for i in range(len(found["LINEDEFS"]) // 14):
            v1, v2, fl, sp, _tag, r, lf = struct.unpack_from("<hhhhhhh", found["LINEDEFS"], 14 * i)
            front = self.sectors[sides[r]] if r >= 0 else None
            back = self.sectors[sides[lf]] if lf >= 0 else None
            (x1, y1), (x2, y2) = verts[v1], verts[v2]
            self.lines.append(Line(float(x1), float(y1), float(x2), float(y2), fl, sp, front, back, i))
        for ln in self.lines:
            if ln.front is not None:
                ln.front.lines.append(ln)
            if ln.back is not None and ln.back is not ln.front:
                ln.back.lines.append(ln)
        for s in self.sectors:
            xs = [v for ln in s.lines for v in (ln.x1, ln.x2)]
            ys = [v for ln in s.lines for v in (ln.y1, ln.y2)]
            if xs:
                s.cx, s.cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2

        self._segs = [struct.unpack_from("<hhhhhh", found["SEGS"], 12 * i) for i in range(len(found["SEGS"]) // 12)]
        self._ssectors = [struct.unpack_from("<hh", found["SSECTORS"], 4 * i) for i in range(len(found["SSECTORS"]) // 4)]
        self._nodes = [struct.unpack_from("<hhhh8hHH", found["NODES"], 28 * i) for i in range(len(found["NODES"]) // 28)]
        things = [struct.unpack_from("<hhhhh", found["THINGS"], 10 * i) for i in range(len(found["THINGS"]) // 10)]
        start = next(t for t in things if t[3] == 1)
        self.start = (float(start[0]), float(start[1]), float(start[2]))
        self.doors: dict[int, Door] = {}

    # -- the map -------------------------------------------------------------------------------

    def sector_at(self, x: float, y: float) -> Sector:
        child = len(self._nodes) - 1
        while not child & 0x8000:
            nx, ny, ndx, ndy, *rest = self._nodes[child]
            side = 0 if ndx * (y - ny) - ndy * (x - nx) < 0 else 1
            child = rest[8 + side] & 0xFFFF
        n, first = self._ssectors[child & 0x7FFF]
        v1, v2, _ang, linedef, side, _off = self._segs[first]
        ln = self.lines[linedef]
        return (ln.front if side == 0 else ln.back) or ln.front

    # -- geometry mirrored from pdoom_state.c ---------------------------------------------------

    @staticmethod
    def gap_open(ln: Line) -> bool:
        if ln.back is None:
            return False
        return min(ln.front.ceil, ln.back.ceil) - max(ln.front.floor, ln.back.floor) >= PLAYER_HEIGHT

    def blocks_walk(self, px: float, py: float, ln: Line) -> bool:
        if ln.back is None or ln.flags & ML_BLOCKING or not self.gap_open(ln):
            return True
        near, far = (ln.front, ln.back) if ln.side(px, py) == 0 else (ln.back, ln.front)
        return far.floor - near.floor > STEP_HEIGHT

    def stops_use(self, ln: Line) -> bool:
        if ln.special != 0 or ln.back is None:
            return True
        return not self.gap_open(ln)

    def cast_ray(self, px: float, py: float, ang: float, max_d: float, mode: int):
        rx, ry = math.cos(ang), math.sin(ang)
        best, hit = max_d, None
        for ln in self.lines:
            wx, wy = ln.x1 - px, ln.y1 - py
            denom = rx * ln.dy - ry * ln.dx
            if abs(denom) < 1e-9:
                continue
            t = (wx * ln.dy - wy * ln.dx) / denom
            u = (wx * ry - wy * rx) / denom
            if t <= 0 or t >= best or u < 0 or u > 1:
                continue
            if mode == 0 and not self.blocks_walk(px, py, ln):
                continue
            if mode == 1 and not self.stops_use(ln):
                continue
            best, hit = t, ln
        return best, hit

    @staticmethod
    def line_distance(px: float, py: float, ln: Line) -> tuple[float, float, float]:
        len2 = ln.dx * ln.dx + ln.dy * ln.dy
        u = 0.0 if len2 == 0 else max(0.0, min(1.0, ((px - ln.x1) * ln.dx + (py - ln.y1) * ln.dy) / len2))
        cx, cy = ln.x1 + u * ln.dx, ln.y1 + u * ln.dy
        return math.hypot(cx - px, cy - py), cx, cy

    @staticmethod
    def bearing(px: float, py: float, facing: float, x: float, y: float) -> float:
        world = math.degrees(math.atan2(y - py, x - px))
        b = -(world - facing)
        while b > 180:
            b -= 360
        while b <= -180:
            b += 360
        return b

    def can_cross(self, keys: set[int], frm: Sector, to: Sector, ln: Line) -> bool:
        if ln.special in DOORS:
            k = DOOR_KEY.get(ln.special, 0)
            return k == 0 or k in keys
        if ln.flags & ML_BLOCKING:
            return False
        if min(frm.ceil, to.ceil) - max(frm.floor, to.floor) < PLAYER_HEIGHT:
            return False
        return to.floor - frm.floor <= STEP_HEIGHT

    # -- the route: a distance field over a grid of cells ---------------------------------------

    CELL = 32.0
    DIRS = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]

    def _grid(self):
        if getattr(self, "_g", None) is not None:
            return self._g
        xs = [v for ln in self.lines for v in (ln.x1, ln.x2)]
        ys = [v for ln in self.lines for v in (ln.y1, ln.y2)]
        c = self.CELL
        ox, oy = min(xs) - c, min(ys) - c
        w, h = int((max(xs) - ox) / c) + 2, int((max(ys) - oy) / c) + 2
        bucket: dict[tuple[int, int], list[Line]] = {}
        for ln in self.lines:
            for gx in range(int((min(ln.x1, ln.x2) - ox) // 128), int((max(ln.x1, ln.x2) - ox) // 128) + 1):
                for gy in range(int((min(ln.y1, ln.y2) - oy) // 128), int((max(ln.y1, ln.y2) - oy) // 128) + 1):
                    bucket.setdefault((gx, gy), []).append(ln)
        g = {"ox": ox, "oy": oy, "w": w, "h": h, "bucket": bucket}
        self._g = g
        walk = [[False] * h for _ in range(w)]
        for ix in range(w):
            for iy in range(h):
                x, y = ox + (ix + 0.5) * c, oy + (iy + 0.5) * c
                if self._near_lines_blocked(g, x, y):
                    continue
                walk[ix][iy] = True
        g["walk"] = walk
        edges: dict[tuple[int, int, int], int] = {}
        for ix in range(w):
            for iy in range(h):
                if not walk[ix][iy]:
                    continue
                for d, (dx, dy) in enumerate(self.DIRS):
                    jx, jy = ix + dx, iy + dy
                    if not (0 <= jx < w and 0 <= jy < h) or not walk[jx][jy]:
                        continue
                    if dx and dy and not (walk[ix + dx][iy] and walk[ix][iy + dy]):
                        continue
                    key = self._edge_key(g, ix, iy, jx, jy)
                    if key is not None:
                        edges[(ix, iy, d)] = key
        g["edges"] = edges
        return g

    def _lines_near(self, g, x1, y1, x2, y2, pad=0.0):
        seen, out = set(), []
        for gx in range(int((min(x1, x2) - pad - g["ox"]) // 128), int((max(x1, x2) + pad - g["ox"]) // 128) + 1):
            for gy in range(int((min(y1, y2) - pad - g["oy"]) // 128), int((max(y1, y2) + pad - g["oy"]) // 128) + 1):
                for ln in g["bucket"].get((gx, gy), ()):
                    if id(ln) not in seen:
                        seen.add(id(ln))
                        out.append(ln)
        return out

    def _near_lines_blocked(self, g, x, y):
        z = self.sector_at(x, y).floor
        for ln in self._lines_near(g, x, y, x, y, RADIUS):
            d, _, _ = self.line_distance(x, y, ln)
            if d >= RADIUS:
                continue
            if ln.back is None or ln.flags & ML_BLOCKING:
                return True
            if ln.special in DOORS:
                continue
            if not self.gap_open(ln) or max(ln.front.floor, ln.back.floor) - z > STEP_HEIGHT:
                return True
        return False

    @staticmethod
    def _crosses(ax, ay, bx, by, ln):
        d1x, d1y = bx - ax, by - ay
        denom = d1x * ln.dy - d1y * ln.dx
        if abs(denom) < 1e-9:
            return False
        t = ((ln.x1 - ax) * ln.dy - (ln.y1 - ay) * ln.dx) / denom
        u = ((ln.x1 - ax) * d1y - (ln.y1 - ay) * d1x) / denom
        return 0 <= t <= 1 and 0 <= u <= 1

    def _edge_key(self, g, ix, iy, jx, jy):
        """None if the step between two cells is blocked, else the key needed (0 for none, 4 for
        a door that needs no key) for the doors it crosses."""
        c = self.CELL
        ax, ay = g["ox"] + (ix + 0.5) * c, g["oy"] + (iy + 0.5) * c
        bx, by = g["ox"] + (jx + 0.5) * c, g["oy"] + (jy + 0.5) * c
        need = 0
        for ln in self._lines_near(g, ax, ay, bx, by):
            if not self._crosses(ax, ay, bx, by, ln):
                continue
            if ln.back is None or ln.flags & ML_BLOCKING:
                return None
            if ln.special in DOORS:
                need = max(need, 4 + DOOR_KEY.get(ln.special, 0))
                continue
            if not self.gap_open(ln):
                return None
            near, far = (ln.front, ln.back) if ln.side(ax, ay) == 0 else (ln.back, ln.front)
            if far.floor - near.floor > STEP_HEIGHT:
                return None
        return need

    def distance_field(self, keys: set[int]):
        g = self._grid()
        cache = g.setdefault("field", {})
        k = tuple(sorted(keys))
        if k in cache:
            return cache[k]
        c = self.CELL
        w, h = g["w"], g["h"]
        dist = [[1e30] * h for _ in range(w)]
        heap = []
        for ln in self.lines:
            if ln.special not in EXIT_ANY or ln.front is None:
                continue
            mx, my = (ln.x1 + ln.x2) / 2, (ln.y1 + ln.y2) / 2
            for ix in range(w):
                for iy in range(h):
                    if not g["walk"][ix][iy]:
                        continue
                    x, y = g["ox"] + (ix + 0.5) * c, g["oy"] + (iy + 0.5) * c
                    d, _, _ = self.line_distance(x, y, ln)
                    if d <= 40.0 and ln.side(x, y) == 0 and abs(x - mx) < 400 and abs(y - my) < 400:
                        dist[ix][iy] = 0.0
                        heap.append((0.0, ix, iy))
        heapq.heapify(heap)
        while heap:
            d, ix, iy = heapq.heappop(heap)
            if d > dist[ix][iy]:
                continue
            for dd, (dx, dy) in enumerate(self.DIRS):
                jx, jy = ix + dx, iy + dy
                if not (0 <= jx < w and 0 <= jy < h):
                    continue
                back = (dd + 4) % 8
                key = g["edges"].get((jx, jy, back))
                if key is None:
                    continue
                if key >= 5 and (key - 4) not in keys:
                    continue
                nd = d + (45.25 if dx and dy else 32.0) + (64.0 if key >= 4 else 0.0)
                if nd < dist[jx][jy]:
                    dist[jx][jy] = nd
                    heapq.heappush(heap, (nd, jx, jy))
        cache[k] = dist
        return dist

    def find_route(self, keys: set[int], px: float, py: float, facing: float):
        g = self._grid()
        dist = self.distance_field(keys)
        c = self.CELL
        ix, iy = int((px - g["ox"]) // c), int((py - g["oy"]) // c)
        # the player's cell, or the best walkable one beside it
        best_cell = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                jx, jy = ix + dx, iy + dy
                if 0 <= jx < g["w"] and 0 <= jy < g["h"] and dist[jx][jy] < 1e29:
                    cx, cy = g["ox"] + (jx + 0.5) * c, g["oy"] + (jy + 0.5) * c
                    cost = dist[jx][jy] + math.hypot(cx - px, cy - py)
                    if best_cell is None or cost < best_cell[0]:
                        best_cell = (cost, jx, jy)
        if best_cell is None:
            return None
        remaining, cx, cy = best_cell
        if dist[cx][cy] == 0.0:
            return ("exit", None, remaining, None)
        path = [(cx, cy)]
        door_at = None
        for _ in range(24):
            ccx, ccy = path[-1]
            nxt = None
            for dd, (dx, dy) in enumerate(self.DIRS):
                jx, jy = ccx + dx, ccy + dy
                key = g["edges"].get((ccx, ccy, dd))
                if key is None or not (0 <= jx < g["w"] and 0 <= jy < g["h"]):
                    continue
                if key >= 5 and (key - 4) not in keys:
                    continue
                if dist[jx][jy] < dist[ccx][ccy] and (nxt is None or dist[jx][jy] < nxt[0]):
                    nxt = (dist[jx][jy], jx, jy, key)
            if nxt is None:
                break
            if nxt[3] >= 4 and door_at is None:
                door_at = len(path)  # index of the cell just before the door step
            path.append((nxt[1], nxt[2]))
            if door_at is not None:
                break
            if nxt[0] == 0.0:
                break
        # the furthest path cell in a straight, clear line from the player
        target = path[0]
        limit = door_at if door_at is not None else len(path) - 1
        for idx in range(0, limit + 1):
            tx, ty = g["ox"] + (path[idx][0] + 0.5) * c, g["oy"] + (path[idx][1] + 0.5) * c
            d = math.hypot(tx - px, ty - py)
            if d < 1:
                target = path[idx]
                continue
            free, _ = self.cast_ray(px, py, math.atan2(ty - py, tx - px), d, 0)
            if free >= d - 0.5:
                target = path[idx]
            elif idx > 1:
                break
        tx, ty = g["ox"] + (target[0] + 0.5) * c, g["oy"] + (target[1] + 0.5) * c
        via = "portal"
        if door_at is not None and target == path[door_at]:
            via = "door"
        return ("route", via, remaining, (tx, ty))

    def scenery(self, px: float, py: float, facing_deg: float, keys: set[int]) -> dict:
        face = math.radians(facing_deg)
        walls = {
            name: round(self.cast_ray(px, py, face - math.radians(b), RAY_MAX, 0)[0])
            for name, b in zip(WALL_NAMES, WALL_BEARINGS, strict=True)
        }
        near = []
        for ln in self.lines:
            kind = kind_of(ln.special)
            if kind is None:
                continue
            d, cx, cy = self.line_distance(px, py, ln)
            if d > 256:
                continue
            key = DOOR_KEY.get(ln.special, 0)
            near.append((d, ln, {
                "kind": kind, "distance": d, "bearing": self.bearing(px, py, facing_deg, cx, cy),
                "facing": ln.side(px, py) == 0, "open": self.gap_open(ln),
                "locked": bool(key and key not in keys),
            }))
        near.sort(key=lambda t: t[0])
        _, hit = self.cast_ray(px, py, face, USE_RANGE, 1)
        use_target = None
        if hit is not None and kind_of(hit.special) and hit.side(px, py) == 0:
            key = DOOR_KEY.get(hit.special, 0)
            use_target = {"kind": kind_of(hit.special), "open": self.gap_open(hit),
                          "locked": bool(key and key not in keys)}
        route = None
        found = self.find_route(keys, px, py, facing_deg)
        if found is not None:
            mode, via, remaining, point = found
            if mode == "exit":
                ex = [ln for ln in self.lines if ln.special in EXIT_ANY]
                ln = min(ex, key=lambda e: self.line_distance(px, py, e)[0])
                _, mx, my = self.line_distance(px, py, ln)
                via = "exit"
            else:
                mx, my = point
            route = {"bearing": self.bearing(px, py, facing_deg, mx, my),
                     "distance": math.hypot(mx - px, my - py), "via": via, "remaining": remaining}
        ex = [ln for ln in self.lines if ln.special in EXIT_ANY]
        eb = ed = None
        if ex:
            ln = min(ex, key=lambda e: math.hypot((e.x1 + e.x2) / 2 - px, (e.y1 + e.y2) / 2 - py))
            mx, my = (ln.x1 + ln.x2) / 2, (ln.y1 + ln.y2) / 2
            eb, ed = self.bearing(px, py, facing_deg, mx, my), math.hypot(mx - px, my - py)
        return {
            "walls": walls, "keys": [], "interactables": [i for _, _, i in near[:6]],
            "use_target": use_target, "route": route, "exit_bearing": eb, "exit_distance": ed,
        }

    # -- movement ------------------------------------------------------------------------------

    def blocked_at(self, x: float, y: float, z: float) -> bool:
        for ln in self.lines:
            d, _, _ = self.line_distance(x, y, ln)
            if d >= RADIUS:
                continue
            if ln.back is None or ln.flags & ML_BLOCKING:
                return True
            if not self.gap_open(ln):
                return True
            hi = max(ln.front.floor, ln.back.floor)
            if hi - z > STEP_HEIGHT:
                return True
        return False

    def use(self, px: float, py: float, facing_deg: float) -> str | None:
        """Press use. Returns 'exit' when the level is cleared."""
        _, hit = self.cast_ray(px, py, math.radians(facing_deg), USE_RANGE, 1)
        if hit is None or hit.side(px, py) != 0:
            return None
        if hit.special in EXIT_USE:
            return "exit"
        if hit.special in DOORS and hit.back is not None:
            door = self.doors.get(hit.back.index)
            if door is not None:
                door.direction = 1 if door.direction == -1 else -1
            else:
                top = min(n.ceil for ln in hit.back.lines for n in (ln.front, ln.back) if n is not None and n is not hit.back) - 4
                self.doors[hit.back.index] = Door(hit.back, 1, top)
        return None

    def tick_doors(self) -> None:
        for idx, door in list(self.doors.items()):
            s = door.sector
            if door.direction == 1:
                s.ceil = min(door.top, s.ceil + 2)
                if s.ceil >= door.top:
                    door.direction, door.wait = 0, 150
            elif door.direction == 0:
                door.wait -= 1
                if door.wait <= 0:
                    door.direction = -1
            else:
                s.ceil = max(s.floor, s.ceil - 2)
                if s.ceil <= s.floor:
                    del self.doors[idx]


@dataclass
class Player:
    x: float
    y: float
    angle: float
    mom_x: float = 0.0
    mom_y: float = 0.0
    turn_tics: int = 0


KEYS_OF = {
    "advance": (1, 0, 0), "retreat": (-1, 0, 0), "strafe_left": (0, -1, 0), "strafe_right": (0, 1, 0),
    "turn_left": (0, 0, 1), "turn_right": (0, 0, -1),
}


def step_tic(level: Level, p: Player, intent: str | None) -> None:
    fwd, strafe, turn = KEYS_OF.get(intent or "", (0, 0, 0))
    if turn:
        p.turn_tics += 1
        p.angle = (p.angle + turn * (1.75 if p.turn_tics < 6 else 3.5)) % 360
    else:
        p.turn_tics = 0
    a = math.radians(p.angle)
    thrust = 25 * 2048 / 65536
    p.mom_x += thrust * fwd * math.cos(a) + thrust * strafe * math.sin(a)
    p.mom_y += thrust * fwd * math.sin(a) - thrust * strafe * math.cos(a)
    z = level.sector_at(p.x, p.y).floor
    nx, ny = p.x + p.mom_x, p.y + p.mom_y
    if not level.blocked_at(nx, ny, z):
        p.x, p.y = nx, ny
    elif not level.blocked_at(nx, p.y, z):
        p.x, p.mom_y = nx, 0.0
    elif not level.blocked_at(p.x, ny, z):
        p.y, p.mom_x = ny, 0.0
    else:
        p.mom_x = p.mom_y = 0.0
    p.mom_x *= 0.90625
    p.mom_y *= 0.90625
    level.tick_doors()


def play(level: Level, bot, max_s: float = 300.0, decision_tics: int = 8, trace=None) -> dict:
    """Run a bot (an object with decide(GameState)) from the level start. Returns the outcome."""
    from pdoom.models import GameState

    p = Player(level.start[0], level.start[1], level.start[2])
    tic, best_hops, rules = 0, 1e9, {}
    while tic < max_s * 35:
        sc = level.scenery(p.x, p.y, p.angle, set())
        state = GameState(
            tic=tic, level="E1M1", seed=1,
            player={"health": 100, "armor": 0, "weapon": "pistol", "ammo": {"total": 50},
                    "x": p.x, "y": p.y, "angle": p.angle},
            **sc,
        )
        if sc["route"]:
            best_hops = min(best_hops, sc["route"]["remaining"])
        decision = bot.decide(state)
        intent = decision.intent.value if decision.intent else None
        hold = decision.hold_tics
        rule = (decision.raw_output or {}).get("rule", "?")
        rules[rule] = rules.get(rule, 0) + 1
        if trace is not None:
            trace.append((tic, round(p.x), round(p.y), round(p.angle), intent, rule))
        for i in range(decision_tics):
            if intent == "use" and i == 0 and level.use(p.x, p.y, p.angle) == "exit":
                return {"outcome": "cleared", "tic": tic, "best_hops": 0, "rules": rules}
            active = hold is None or i < hold
            step_tic(level, p, intent if active and intent not in ("use", "fire") else None)
            tic += 1
    return {"outcome": "timeout", "tic": tic, "best_hops": best_hops, "rules": rules,
            "at": (round(p.x), round(p.y))}
