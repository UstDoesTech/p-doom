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
WALK_BOX = 14.0
DOOR_BOX = 9.0
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
TELEPORT = {39, 97, 125, 126}
PLAYER_TELEPORT = {39, 97}
LIGHT = {12, 13, 17, 35, 79, 80, 81, 104, 138, 139, 156, 157, 158, 159, 169, 170, 171, 172, 173, 174, 175, 176, 177, 178, 179, 180}
KEY_THINGS = {5: 1, 40: 1, 6: 2, 39: 2, 13: 3, 38: 3}
STAIRS = {7, 8, 100, 127}
BOSS_TAGS = {666, 667}
BOSS_THINGS = (3003, 16, 7)
KEY_COST = 300.0
GATE_COST = 150.0
JUMP_COST = 100.0


def is_trigger(special: int) -> bool:
    """A line special that moves a tagged sector (doors, floors, lifts, stairs)."""
    return special != 0 and special not in LIGHT and special not in TELEPORT and special not in EXIT_ANY and special != 48


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
    tag: int = 0
    special: int = 0


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
    tag: int = 0

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
            _light, sspecial, stag = struct.unpack_from("<hhh", found["SECTORS"], 26 * i + 20)
            self.sectors.append(Sector(float(fl), float(ce), index=i, tag=stag, special=sspecial))
        sides = [struct.unpack_from("<hh8s8s8sh", found["SIDEDEFS"], 30 * i)[5] for i in range(len(found["SIDEDEFS"]) // 30)]
        self.lines: list[Line] = []
        for i in range(len(found["LINEDEFS"]) // 14):
            v1, v2, fl, sp, ltag, r, lf = struct.unpack_from("<hhhhhhh", found["LINEDEFS"], 14 * i)
            front = self.sectors[sides[r]] if r >= 0 else None
            back = self.sectors[sides[lf]] if lf >= 0 else None
            (x1, y1), (x2, y2) = verts[v1], verts[v2]
            self.lines.append(Line(float(x1), float(y1), float(x2), float(y2), fl, sp, front, back, i, ltag))
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
        self.things = things
        start = next(t for t in things if t[3] == 1)
        self.start = (float(start[0]), float(start[1]), float(start[2]))
        self.doors: dict[int, Door] = {}
        self.taken: set = set()

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

    def _trigger_tags(self) -> set[int]:
        if getattr(self, "_ttags", None) is None:
            # 666 and 667 are moved when a map's bosses die (A_BossDeath), not by a line
            self._ttags = {ln.tag for ln in self.lines if ln.tag and is_trigger(ln.special)} | BOSS_TAGS
        return self._ttags

    def _stair_sectors(self) -> dict[int, int]:
        """Sector index -> tag for the steps a build-stairs trigger raises. Only the first step
        is tagged; the rest are found by walking to neighbours that start at the same height."""
        if getattr(self, "_stairs", None) is None:
            out: dict[int, int] = {}
            for ln in self.lines:
                if ln.special not in STAIRS or not ln.tag:
                    continue
                roots = [s for s in self.sectors if s.tag == ln.tag]
                todo = list(roots)
                for r in roots:
                    out[r.index] = ln.tag
                while todo and len(out) < 400:
                    cur = todo.pop()
                    for e in cur.lines:
                        if e.back is None:
                            continue
                        o = e.back if e.front is cur else e.front
                        if o.index not in out and o.floor == cur.floor:
                            out[o.index] = ln.tag
                            todo.append(o)
            self._stairs = out
        return self._stairs

    def _door_class(self, ln: Line) -> int | None:
        """4 + key for a line that is a door, or the other face of one: a door sector starts shut
        (floor = ceiling) and has its door special on only some of its lines."""
        if ln.special in DOORS:
            return 4 + DOOR_KEY.get(ln.special, 0)
        if ln.back is None:
            return None
        out = None
        for s in (ln.front, ln.back):
            if s.floor == s.ceil:
                keys = [DOOR_KEY.get(e.special, 0) for e in s.lines if e.special in DOORS]
                if keys:
                    out = max(out or 4, 4 + max(keys))
        return out

    def _gate_tag(self, ln: Line) -> int | None:
        """The tag of the sector a trigger would have to move to make this line passable."""
        tags = self._trigger_tags()
        stairs = self._stair_sectors()
        for s in (ln.front, ln.back):
            if s is not None and s.tag in tags:
                return s.tag
            if s is not None and s.index in stairs:
                return stairs[s.index]
        return None

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
        edges: dict[tuple[int, int, int], tuple[int, tuple[int, ...]]] = {}
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
                    info = self._edge_info(g, ix, iy, jx, jy)
                    if info is not None:
                        edges[(ix, iy, d)] = info
        g["edges"] = edges
        # a door or a trigger-moved sector can be thinner than a cell, so no cell lies inside it:
        # hop over it from a cell on one side to a cell on the other
        hops: dict[tuple[int, int], list] = {}
        hop_in: dict[tuple[int, int], list] = {}
        gate_lines = [ln for ln in self.lines if ln.back is not None and not ln.flags & ML_BLOCKING
                      and (self._door_class(ln) is not None or (self._gate_tag(ln) is not None and (
                          not self.gap_open(ln) or abs(ln.front.floor - ln.back.floor) > STEP_HEIGHT)))]
        near_gate: set[tuple[int, int]] = set()
        for ln in gate_lines:
            near_gate.update(self._cells_near_line(g, ln, 48.0, any_side=True))
        for (ix, iy) in near_gate:
            for dx in range(-3, 4):
                for dy in range(-3, 4):
                    if max(abs(dx), abs(dy)) < 2:
                        continue
                    jx, jy = ix + dx, iy + dy
                    if not (0 <= jx < w and 0 <= jy < h) or not walk[jx][jy]:
                        continue
                    info = self._edge_info(g, ix, iy, jx, jy)
                    if info is None or (info[0] < 4 and not info[1]):
                        continue
                    length = math.hypot(dx, dy) * c
                    hops.setdefault((ix, iy), []).append(((jx, jy), info, length))
                    hop_in.setdefault((jx, jy), []).append(((ix, iy), info, length))
        g["hops"], g["hop_in"] = hops, hop_in
        # teleporters: standing on the line sends the player to the destination
        jump_in: dict[tuple[int, int], list[tuple[tuple[int, int], tuple[float, float]]]] = {}
        jumps: dict[tuple[int, int], tuple[tuple[int, int], Line]] = {}
        for ln in self.lines:
            if ln.special not in PLAYER_TELEPORT or not ln.tag:
                continue
            dest = next((t for t in self.things if t[3] == 14 and self.sector_at(float(t[0]), float(t[1])).tag == ln.tag), None)
            if dest is None:
                continue
            dcell = self._nearest_walkable(g, float(dest[0]), float(dest[1]))
            if dcell is None:
                continue
            for ix, iy in self._cells_near_line(g, ln, 24.0, any_side=True):
                jumps[(ix, iy)] = (dcell, ln)
                jump_in.setdefault(dcell, []).append(((ix, iy), (ln.x1, ln.y1)))
        g["jumps"], g["jump_in"] = jumps, jump_in
        return g

    def _cell_xy(self, g, ix, iy):
        return g["ox"] + (ix + 0.5) * self.CELL, g["oy"] + (iy + 0.5) * self.CELL

    def _nearest_walkable(self, g, x, y):
        ix, iy = int((x - g["ox"]) // self.CELL), int((y - g["oy"]) // self.CELL)
        best = None
        for r in range(0, 4):
            for dx in range(-r, r + 1):
                for dy in range(-r, r + 1):
                    jx, jy = ix + dx, iy + dy
                    if 0 <= jx < g["w"] and 0 <= jy < g["h"] and g["walk"][jx][jy]:
                        cx, cy = self._cell_xy(g, jx, jy)
                        d = math.hypot(cx - x, cy - y)
                        if best is None or d < best[0]:
                            best = (d, (jx, jy))
            if best:
                return best[1]
        return None

    def _cells_near_point(self, g, x, y, reach):
        """Walkable cells whose centre is within reach of a point, or the nearest one. A key on a
        pedestal can be taken from the floor beside it: pickups ignore a height difference of
        less than the player's height."""
        out = []
        ix, iy = int((x - g["ox"]) // self.CELL), int((y - g["oy"]) // self.CELL)
        for dx in range(-2, 3):
            for dy in range(-2, 3):
                jx, jy = ix + dx, iy + dy
                if 0 <= jx < g["w"] and 0 <= jy < g["h"] and g["walk"][jx][jy]:
                    cx, cy = self._cell_xy(g, jx, jy)
                    if abs(cx - x) <= reach and abs(cy - y) <= reach:
                        out.append((jx, jy))
        if not out:
            c = self._nearest_walkable(g, x, y)
            if c:
                out.append(c)
        return out

    def _cells_near_line(self, g, ln, reach, any_side=False):
        out = []
        mx, my = (ln.x1 + ln.x2) / 2, (ln.y1 + ln.y2) / 2
        span = math.hypot(ln.dx, ln.dy) / 2 + reach + self.CELL
        x0, x1 = int((mx - span - g["ox"]) // self.CELL), int((mx + span - g["ox"]) // self.CELL)
        y0, y1 = int((my - span - g["oy"]) // self.CELL), int((my + span - g["oy"]) // self.CELL)
        for ix in range(max(0, x0), min(g["w"] - 1, x1) + 1):
            for iy in range(max(0, y0), min(g["h"] - 1, y1) + 1):
                if not g["walk"][ix][iy]:
                    continue
                x, y = self._cell_xy(g, ix, iy)
                d, _, _ = self.line_distance(x, y, ln)
                if d <= reach and (any_side or ln.side(x, y) == 0):
                    out.append((ix, iy))
        return out

    def _lines_near(self, g, x1, y1, x2, y2, pad=0.0):
        seen, out = set(), []
        for gx in range(int((min(x1, x2) - pad - g["ox"]) // 128), int((max(x1, x2) + pad - g["ox"]) // 128) + 1):
            for gy in range(int((min(y1, y2) - pad - g["oy"]) // 128), int((max(y1, y2) + pad - g["oy"]) // 128) + 1):
                for ln in g["bucket"].get((gx, gy), ()):
                    if id(ln) not in seen:
                        seen.add(id(ln))
                        out.append(ln)
        return out

    @staticmethod
    def _touches_box(ln, x, y, h):
        dx, dy = ln.x2 - ln.x1, ln.y2 - ln.y1
        t0, t1 = 0.0, 1.0
        for p, q in ((-dx, ln.x1 - (x - h)), (dx, (x + h) - ln.x1), (-dy, ln.y1 - (y - h)), (dy, (y + h) - ln.y1)):
            if abs(p) < 1e-12:
                if q < 0:
                    return False
            else:
                r = q / p
                if p < 0:
                    if r > t1:
                        return False
                    t0 = max(t0, r)
                else:
                    if r < t0:
                        return False
                    t1 = min(t1, r)
        return True

    def _near_lines_blocked(self, g, x, y):
        z = self.sector_at(x, y).floor
        box = WALK_BOX
        # a throat beside a door or a trigger-moved sector may be too tight for the full box
        for ln in self._lines_near(g, x, y, x, y, 48.0):
            if (self._door_class(ln) is not None or (ln.back is not None and self._gate_tag(ln) is not None
                                        and not self.gap_open(ln))) and self.line_distance(x, y, ln)[0] < 40.0:
                box = DOOR_BOX
                break
        for ln in self._lines_near(g, x, y, x, y, box):
            if not self._touches_box(ln, x, y, box):
                continue
            if ln.back is None or ln.flags & ML_BLOCKING:
                return True
            if self._door_class(ln) is not None:
                continue
            if not self.gap_open(ln) or max(ln.front.floor, ln.back.floor) - z > STEP_HEIGHT:
                if self._gate_tag(ln) is None:
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

    @staticmethod
    def _cross_t(ax, ay, bx, by, ln):
        denom = (bx - ax) * ln.dy - (by - ay) * ln.dx
        return ((ln.x1 - ax) * ln.dy - (ln.y1 - ay) * ln.dx) / denom

    def _edge_info(self, g, ix, iy, jx, jy):
        """None if the step between two cells can never be made, else (door, gates): the door
        class (0 none, 4 a door that needs no key, 5..7 a key door) and the tags of the sectors
        a trigger has to move first (closed doors, raised floors, lifts)."""
        ax, ay = self._cell_xy(g, ix, iy)
        bx, by = self._cell_xy(g, jx, jy)
        need, gates = 0, []
        first_door = None  # (t, key class) of the first door line crossed
        for ln in self._lines_near(g, ax, ay, bx, by):
            if not self._crosses(ax, ay, bx, by, ln):
                continue
            if ln.back is None or ln.flags & ML_BLOCKING:
                return None
            dc = self._door_class(ln)
            if dc is not None:
                # the key is the one on the face you press from: a door sector can need a key
                # from one side and none from the other
                t = self._cross_t(ax, ay, bx, by, ln)
                cls = 4 + DOOR_KEY.get(ln.special, 0) if ln.special in DOORS and ln.side(ax, ay) == 0 else 4
                if first_door is None or t < first_door[0]:
                    first_door = (t, cls)
                continue
            near, far = (ln.front, ln.back) if ln.side(ax, ay) == 0 else (ln.back, ln.front)
            if not self.gap_open(ln) or far.floor - near.floor > STEP_HEIGHT:
                tag = self._gate_tag(ln)
                if tag is None:
                    return None
                gates.append(tag)
        if first_door is not None:
            need = first_door[1]
        return need, tuple(sorted(set(gates)))

    def _edge_cost(self, info, length, keys, opened, optimistic):
        need, gates = info
        cost = length
        if need >= 4:
            cost += 64.0
        if need >= 5 and (need - 4) not in keys:
            if not optimistic or ("key", need - 4) in self._banned:
                return None
            cost += KEY_COST
        for t in gates:
            if t not in opened:
                if not optimistic or ("gate", t) in self._banned:
                    return None
                cost += GATE_COST
        return cost

    def _solve(self, goals, keys, opened, optimistic):
        g = self._grid()
        w, h = g["w"], g["h"]
        dist = [[1e30] * h for _ in range(w)]
        heap = []
        for ix, iy in goals:
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
                info = g["edges"].get((jx, jy, (dd + 4) % 8))
                if info is None:
                    continue
                cost = self._edge_cost(info, 45.25 if dx and dy else 32.0, keys, opened, optimistic)
                if cost is None:
                    continue
                if d + cost < dist[jx][jy]:
                    dist[jx][jy] = d + cost
                    heapq.heappush(heap, (d + cost, jx, jy))
            for (sx, sy), info, length in g["hop_in"].get((ix, iy), ()):
                cost = self._edge_cost(info, length, keys, opened, optimistic)
                if cost is not None and d + cost < dist[sx][sy]:
                    dist[sx][sy] = d + cost
                    heapq.heappush(heap, (d + cost, sx, sy))
            for (sx, sy), _pt in g["jump_in"].get((ix, iy), ()):
                if d + JUMP_COST < dist[sx][sy]:
                    dist[sx][sy] = d + JUMP_COST
                    heapq.heappush(heap, (d + JUMP_COST, sx, sy))
        return dist

    def _exit_goals(self, g):
        exits = [ln for ln in self.lines if ln.special in EXIT_ANY and ln.front is not None]
        plain = [ln for ln in exits if ln.special != 51]
        cells = set()
        for ln in plain or exits:
            found = self._cells_near_line(g, ln, 40.0)
            # a small alcove may have no cell centre that close: stand anywhere in reach
            cells.update(found or self._cells_near_line(g, ln, 72.0))
        if cells:
            return cells, "exit", plain or exits
        # no exit line (last map of an episode): it ends when the boss dies
        for t in self.things:
            if t[3] in BOSS_THINGS:
                c = self._nearest_walkable(g, float(t[0]), float(t[1]))
                if c:
                    cells.add(c)
        return cells, "kill", []

    def _player_cell(self, dist, px, py):
        g = self._g
        c = self.CELL
        ix, iy = int((px - g["ox"]) // c), int((py - g["oy"]) // c)
        best = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                jx, jy = ix + dx, iy + dy
                if 0 <= jx < g["w"] and 0 <= jy < g["h"] and dist[jx][jy] < 1e29:
                    cx, cy = self._cell_xy(g, jx, jy)
                    cost = dist[jx][jy] + math.hypot(cx - px, cy - py)
                    if best is None or cost < best[0]:
                        best = (cost, jx, jy)
        return best

    def _descend(self, dist, cell, keys, opened, optimistic, steps):
        """Walk downhill from a cell. Returns the cells and, for each step, how it is made:
        ('walk', info), ('jump', line)."""
        g = self._g
        path, how = [cell], []
        for _ in range(steps):
            cx, cy = path[-1]
            if dist[cx][cy] == 0.0:
                break
            best = None
            for dd, (dx, dy) in enumerate(self.DIRS):
                jx, jy = cx + dx, cy + dy
                info = g["edges"].get((cx, cy, dd))
                if info is None or not (0 <= jx < g["w"] and 0 <= jy < g["h"]):
                    continue
                cost = self._edge_cost(info, 45.25 if dx and dy else 32.0, keys, opened, optimistic)
                if cost is None or dist[jx][jy] >= dist[cx][cy]:
                    continue
                if best is None or cost + dist[jx][jy] < best[0]:
                    best = (cost + dist[jx][jy], (jx, jy), ("walk", info))
            for (jx, jy), info, length in g["hops"].get((cx, cy), ()):
                cost = self._edge_cost(info, length, keys, opened, optimistic)
                if cost is not None and dist[jx][jy] < dist[cx][cy] and (best is None or cost + dist[jx][jy] < best[0]):
                    best = (cost + dist[jx][jy], (jx, jy), ("walk", info))
            jump = g["jumps"].get((cx, cy))
            if jump is not None and dist[jump[0][0]][jump[0][1]] + JUMP_COST < dist[cx][cy] + 1e-6:
                if best is None or JUMP_COST + dist[jump[0][0]][jump[0][1]] <= best[0]:
                    best = (JUMP_COST + dist[jump[0][0]][jump[0][1]], jump[0], ("jump", jump[1]))
            if best is None:
                break
            path.append(best[1])
            how.append(best[2])
        return path, how

    def _subgoal(self, blocker, visited):
        g = self._g
        kind, what = blocker
        cells: set = set()
        if kind == "gate" and what in BOSS_TAGS:
            for t in self.things:
                if t[3] in BOSS_THINGS:
                    c = self._nearest_walkable(g, float(t[0]), float(t[1]))
                    if c:
                        cells.add(c)
            return (cells, "kill", []) if cells else None
        if kind == "key":
            self._goal_pts = []
            for t in self.things:
                if KEY_THINGS.get(t[3]) == what:
                    cells.update(self._cells_near_point(g, float(t[0]), float(t[1]), 48.0))
                    self._goal_pts.append((float(t[0]), float(t[1])))
            return (cells, "pickup", []) if cells else None
        lines = [ln for ln in self.lines if ln.tag == what and is_trigger(ln.special) and ln.index not in visited]
        for ln in lines:
            use = ln.special in SWITCHES
            found = self._cells_near_line(g, ln, 40.0 if use else 24.0, any_side=not use)
            cells.update(found or self._cells_near_line(g, ln, 72.0 if use else 48.0, any_side=not use))
        return (cells, "switch", lines) if cells else None

    def plan(self, keys, px, py, opened=frozenset(), visited=frozenset()):
        """The next place to go: a dict with via (portal, door, exit, switch, walk, pickup),
        remaining, the point to head for, and the goal the stage is working towards."""
        g = self._grid()
        goals, goal_kind, goal_lines = self._exit_goals(g)
        self.blockers = []
        self._banned = set()
        self._goal_pts = []
        stage = 0
        for stage in range(7):
            strict = self._solve(goals, keys, opened, False)
            cell = self._player_cell(strict, px, py)
            if cell is not None:
                return self._follow(strict, cell, keys, opened, px, py, goal_kind, goal_lines, stage)
            opt = self._solve(goals, keys, opened, True)
            cell = self._player_cell(opt, px, py)
            if cell is None:
                self.why = f"stage {stage} {goal_kind}: player unreachable even optimistically (goals {len(goals)})"
                return None
            path, how = self._descend(opt, (cell[1], cell[2]), keys, opened, True, 800)
            blocker = None
            for (kind, info) in how:
                if kind != "walk":
                    continue
                need, gates = info
                if need >= 5 and (need - 4) not in keys:
                    blocker = ("key", need - 4)
                    break
                missing = [t for t in gates if t not in opened]
                if missing:
                    blocker = ("gate", missing[0])
                    break
            if blocker is None:
                self.why = f"stage {stage}: no blocker on optimistic path"
                return None
            self.blockers = self.blockers[:stage] + [blocker]
            self._banned.add(blocker)
            sub = self._subgoal(blocker, visited)
            if sub is None:
                self.why = f"stage {stage}: no subgoal for {blocker}"
                return None
            goals, goal_kind, goal_lines = sub
        self.why = "ran out of stages: " + repr(self.blockers)
        return None

    def _follow(self, dist, cell, keys, opened, px, py, goal_kind, goal_lines, stage):
        g = self._g
        remaining, cx, cy = cell
        start = (cx, cy)
        if dist[cx][cy] == 0.0:
            ln = None
            if goal_lines:
                ln = min(goal_lines, key=lambda e: self.line_distance(px, py, e)[0])
                _, mx, my = self.line_distance(px, py, ln)
            else:
                mx, my = px, py
                if self._goal_pts:
                    mx, my = min(self._goal_pts, key=lambda q: math.hypot(q[0] - px, q[1] - py))
            via = {"exit": "exit", "switch": "walk" if ln is not None and ln.special not in SWITCHES else "switch",
                   "pickup": "portal", "kill": "portal"}[goal_kind]
            return {"via": via, "remaining": remaining, "point": (mx, my), "goal": goal_kind, "line": ln}
        path, how = self._descend(dist, start, keys, opened, False, 24)
        door_at = jump_at = None
        for i, (kind, info) in enumerate(how):
            if kind == "jump":
                jump_at = i
                break
            if info[0] >= 4:
                door_at = i + 1  # the cell across the door
                break
        stop = door_at if door_at is not None else jump_at
        if jump_at is not None:
            ln = how[jump_at][1]
            _, mx, my = self.line_distance(px, py, ln)
            return {"via": "walk", "remaining": remaining, "point": (mx, my), "goal": goal_kind, "line": None}
        limit = stop if stop is not None else len(path) - 1
        target = path[0]
        for idx in range(0, limit + 1):
            tx, ty = self._cell_xy(g, *path[idx])
            d = math.hypot(tx - px, ty - py)
            if d < 1:
                target = path[idx]
                continue
            a = math.atan2(ty - py, tx - px)
            half = 16.0 * (abs(math.cos(a)) + abs(math.sin(a))) + 1.0
            if all(self.cast_ray(px - o * math.sin(a), py + o * math.cos(a), a, d, 0)[0] >= d - 0.5 for o in (0.0, -half, half)):
                target = path[idx]
            elif idx > 1:
                break
        tx, ty = self._cell_xy(g, *target)
        via = "door" if door_at is not None and target == path[door_at] else "portal"
        return {"via": via, "remaining": remaining, "point": (tx, ty), "goal": goal_kind, "line": None}

    def find_route(self, keys, px, py, facing, opened=frozenset(), visited=frozenset()):
        return self.plan(keys, px, py, opened, visited)

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
            mx, my = found["point"]
            route = {"bearing": self.bearing(px, py, facing_deg, mx, my),
                     "distance": math.hypot(mx - px, my - py), "via": found["via"],
                     "remaining": found["remaining"]}
        ex = [ln for ln in self.lines if ln.special in EXIT_ANY]
        eb = ed = None
        if ex:
            ln = min(ex, key=lambda e: math.hypot((e.x1 + e.x2) / 2 - px, (e.y1 + e.y2) / 2 - py))
            mx, my = (ln.x1 + ln.x2) / 2, (ln.y1 + ln.y2) / 2
            eb, ed = self.bearing(px, py, facing_deg, mx, my), math.hypot(mx - px, my - py)
        names = {1: "blue", 2: "yellow", 3: "red"}
        pickups = []
        for t in self.things:
            k = KEY_THINGS.get(t[3])
            if k and k not in keys and (t[0], t[1]) not in self.taken:
                pickups.append({"type": "key", "distance": math.hypot(t[0] - px, t[1] - py),
                                "bearing": self.bearing(px, py, facing_deg, t[0], t[1])})
        pickups.sort(key=lambda q: q["distance"])
        return {
            "walls": walls, "keys": [names[k] for k in sorted(keys)], "pickups": pickups[:16],
            "interactables": [i for _, _, i in near[:6]],
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

    def use(self, px: float, py: float, facing_deg: float, keys=None) -> str | None:
        """Press use. Returns 'exit' when the level is cleared."""
        _, hit = self.cast_ray(px, py, math.radians(facing_deg), USE_RANGE, 1)
        if hit is None or hit.side(px, py) != 0:
            return None
        if hit.special in EXIT_USE:
            return "exit"
        if hit.special in DOORS and hit.back is not None:
            need = DOOR_KEY.get(hit.special, 0)
            if keys is not None and need and need not in keys:
                return None
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
    keys: set[int] = set()
    level.taken = set()
    while tic < max_s * 35:
        sc = level.scenery(p.x, p.y, p.angle, keys)
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
            if intent == "use" and i == 0 and level.use(p.x, p.y, p.angle, keys) == "exit":
                return {"outcome": "cleared", "tic": tic, "best_hops": 0, "rules": rules}
            active = hold is None or i < hold
            step_tic(level, p, intent if active and intent not in ("use", "fire") else None)
            tic += 1
            for t in level.things:
                k = KEY_THINGS.get(t[3])
                if k and k not in keys and abs(t[0] - p.x) < 36 and abs(t[1] - p.y) < 36:
                    keys.add(k)
                    level.taken.add((t[0], t[1]))
    return {"outcome": "timeout", "tic": tic, "best_hops": best_hops, "rules": rules,
            "at": (round(p.x), round(p.y))}
