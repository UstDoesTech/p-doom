/*
 * p(doom) state shim for doom-wasm (Chocolate Doom, Emscripten).
 *
 * Copied into src/doom/ by engine/build.sh. Exports a small C API that web/src/engine/WasmEngine.ts
 * calls through ccall. See docs/state.md for the JSON this produces.
 *
 *   const char *pdoom_get_state(void)            JSON snapshot of the console player's world
 *   void pdoom_new_game(int episode, int map, int seed)
 *   void pdoom_pause(void), pdoom_resume(void)   freeze and unfreeze the world
 *   void pdoom_apply_seed(void)                  called from G_InitNew after M_ClearRandom
 *
 * Besides enemies and pickups the state describes the scenery, so a player can find its way:
 *   walls         distance to the nearest blocking surface in eight directions (capped at 512)
 *   interactables doors, switches and exits within 256 units
 *   use_target    what pressing use would do right now, or null
 *   route         where to head to reach the exit through doors the player can open
 *   keys          key colours held
 *
 * Conventions: positions are map units. "angle" is degrees counter-clockwise from east, 0..360.
 * Every "bearing" is degrees relative to where the player faces, positive to the RIGHT
 * (clockwise), -180..180. Doom's own angles run counter-clockwise, so the sign is flipped here.
 */

#include <math.h>
#include <stdio.h>
#include <string.h>

#include <emscripten.h>

#include "d_main.h"
#include "d_player.h"
#include "doomdef.h"
#include "doomstat.h"
#include "g_game.h"
#include "info.h"
#include "m_random.h"
#include "p_local.h"
#include "p_mobj.h"
#include "r_main.h"
#include "r_state.h"

#define MAX_LISTED 16
#define OUT_SIZE 16384
#define MAX_SECTORS 1024
#define MAX_LINES 2048
#define INF_COST 1e30
#define MAX_INTERACT 6
#define RAY_MAX 512.0
#define PLAYER_HEIGHT (56 * FRACUNIT)
#define STEP_HEIGHT (24 * FRACUNIT)

extern int rndindex;
extern int prndindex;

static char out[OUT_SIZE];
static int pending_seed = 0;

static double normalise(double deg)
{
    while (deg > 180.0) deg -= 360.0;
    while (deg <= -180.0) deg += 360.0;
    return deg;
}

/* Degrees counter-clockwise from east, 0..360. */
static double facing_deg(const mobj_t *m)
{
    return (double)m->angle * 360.0 / 4294967296.0;
}

/* Bearing of a map point from the player: positive means to the right. */
static double bearing_to(const mobj_t *me, double x, double y)
{
    double dx = x - (double)me->x / 65536.0;
    double dy = y - (double)me->y / 65536.0;
    double world = atan2(dy, dx) * 180.0 / M_PI;
    return normalise(-(world - facing_deg(me)));
}

static double dist_to(const mobj_t *me, double x, double y)
{
    double dx = x - (double)me->x / 65536.0;
    double dy = y - (double)me->y / 65536.0;
    return sqrt(dx * dx + dy * dy);
}

static const char *enemy_name(mobjtype_t t)
{
    switch (t) {
    case MT_POSSESSED: return "zombieman";
    case MT_SHOTGUY: return "shotgun_guy";
    case MT_CHAINGUY: return "chaingunner";
    case MT_TROOP: return "imp";
    case MT_SERGEANT: return "demon";
    case MT_SHADOWS: return "spectre";
    case MT_HEAD: return "cacodemon";
    case MT_BRUISER: return "baron";
    case MT_SKULL: return "lost_soul";
    case MT_UNDEAD: return "revenant";
    case MT_FATSO: return "mancubus";
    default: return "monster";
    }
}

static const char *pickup_name(spritenum_t s)
{
    switch (s) {
    case SPR_STIM:
    case SPR_MEDI:
    case SPR_BON1: return "health";
    case SPR_SOUL: return "health";
    case SPR_ARM1:
    case SPR_ARM2:
    case SPR_BON2: return "armor";
    case SPR_CLIP:
    case SPR_AMMO:
    case SPR_SHEL:
    case SPR_SBOX:
    case SPR_BPAK: return "ammo";
    case SPR_BKEY:
    case SPR_RKEY:
    case SPR_YKEY:
    case SPR_BSKU:
    case SPR_RSKU:
    case SPR_YSKU: return "key";
    case SPR_SHOT:
    case SPR_SGN2:
    case SPR_MGUN:
    case SPR_CSAW:
    case SPR_LAUN:
    case SPR_PLAS:
    case SPR_BFUG: return "weapon";
    default: return "other";
    }
}

static const char *weapon_name(weapontype_t w)
{
    static const char *names[] = {"fist",   "pistol",  "shotgun", "chaingun", "rocket_launcher",
                                  "plasma", "bfg",     "chainsaw", "super_shotgun"};
    return (w >= 0 && w < NUMWEAPONS) ? names[w] : "unknown";
}

typedef struct {
    double distance;
    double bearing;
    int los;
    int kind; /* enemy: mobjtype, pickup: spritenum */
} listed_t;

/* Keep the MAX_LISTED nearest entries, sorted by distance. */
static void keep_nearest(listed_t *list, int *n, const listed_t *item)
{
    int i = *n;
    if (i == MAX_LISTED) {
        if (item->distance >= list[MAX_LISTED - 1].distance) return;
        i = MAX_LISTED - 1;
    } else {
        (*n)++;
    }
    while (i > 0 && list[i - 1].distance > item->distance) {
        list[i] = list[i - 1];
        i--;
    }
    list[i] = *item;
}

/* Bearing and distance to the nearest exit line (normal exit 11, walk-over exit 52, secret 51/124). */
static int exit_bearing(const mobj_t *me, double *bearing, double *distance)
{
    int i, found = 0;
    double best = 0.0;
    for (i = 0; i < numlines; i++) {
        const line_t *l = &lines[i];
        double mx, my, d;
        if (l->special != 11 && l->special != 52 && l->special != 51 && l->special != 124)
            continue;
        mx = ((double)l->v1->x + (double)l->v2->x) / 2.0 / 65536.0;
        my = ((double)l->v1->y + (double)l->v2->y) / 2.0 / 65536.0;
        d = dist_to(me, mx, my);
        if (!found || d < best) {
            best = d;
            *bearing = bearing_to(me, mx, my);
            *distance = d;
            found = 1;
        }
    }
    return found;
}

/* ---- scenery ------------------------------------------------------------------------------ */

enum { KIND_NONE = 0, KIND_DOOR = 1, KIND_SWITCH = 2, KIND_EXIT = 3 };

static int is_door(int special)
{
    switch (special) {
    case 1: case 26: case 27: case 28: case 31: case 32: case 33: case 34: case 117: case 118:
        return 1;
    default: return 0;
    }
}

/* 0 none, 1 blue, 2 yellow, 3 red. */
static int door_key(int special)
{
    switch (special) {
    case 26: case 32: return 1;
    case 27: case 34: return 2;
    case 28: case 33: return 3;
    default: return 0;
    }
}

static const char *key_name(int k)
{
    return k == 1 ? "blue" : k == 2 ? "yellow" : k == 3 ? "red" : "none";
}

static int has_key(const player_t *p, int k)
{
    if (k == 1) return p->cards[it_bluecard] || p->cards[it_blueskull];
    if (k == 2) return p->cards[it_yellowcard] || p->cards[it_yellowskull];
    if (k == 3) return p->cards[it_redcard] || p->cards[it_redskull];
    return 1;
}

/* The line specials P_UseSpecialLine handles besides doors: switches and buttons. */
static int is_switch(int special)
{
    switch (special) {
    case 7: case 9: case 14: case 15: case 18: case 20: case 21: case 23: case 29: case 41:
    case 42: case 43: case 45: case 49: case 50: case 55: case 60: case 61: case 62: case 63:
    case 64: case 65: case 66: case 67: case 68: case 69: case 70: case 71: case 99: case 101:
    case 102: case 103: case 111: case 112: case 113: case 114: case 115: case 116: case 122:
    case 123: case 127: case 131: case 132: case 133: case 134: case 135: case 136: case 137:
    case 138: case 139: case 140:
        return 1;
    default: return 0;
    }
}

static int line_kind(const line_t *l)
{
    if (l->special == 11 || l->special == 51) return KIND_EXIT;
    if (is_door(l->special)) return KIND_DOOR;
    if (is_switch(l->special)) return KIND_SWITCH;
    return KIND_NONE;
}

static const char *kind_name(int k)
{
    return k == KIND_DOOR ? "door" : k == KIND_SWITCH ? "switch" : k == KIND_EXIT ? "exit" : "none";
}

static fixed_t min_fixed(fixed_t a, fixed_t b) { return a < b ? a : b; }
static fixed_t max_fixed(fixed_t a, fixed_t b) { return a > b ? a : b; }

/* Is there room to stand in the gap of a two-sided line? Closed doors have none. */
static int gap_open(const line_t *l)
{
    if (l->backsector == NULL) return 0;
    return min_fixed(l->frontsector->ceilingheight, l->backsector->ceilingheight) -
               max_fixed(l->frontsector->floorheight, l->backsector->floorheight) >=
           PLAYER_HEIGHT;
}

/* Does this line stop a walking player coming from the side of (px, py)? */
static int blocks_walk(const mobj_t *me, const line_t *l)
{
    const sector_t *near_s, *far_s;
    if (l->backsector == NULL || (l->flags & ML_BLOCKING)) return 1;
    if (!gap_open(l)) return 1;
    if (P_PointOnLineSide(me->x, me->y, (line_t *)l) == 0) {
        near_s = l->frontsector;
        far_s = l->backsector;
    } else {
        near_s = l->backsector;
        far_s = l->frontsector;
    }
    return far_s->floorheight - near_s->floorheight > STEP_HEIGHT;
}

/* Does this line stop the use trace? Doom stops at the first line with a special, or a closed gap. */
static int stops_use(const line_t *l)
{
    if (l->special != 0) return 1;
    if (l->backsector == NULL) return 1;
    return !gap_open(l);
}

/* Distance along a ray from the player to the first qualifying line, up to max. mode 0: lines that
 * block walking; mode 1: lines that stop the use trace. */
static double cast_ray(const mobj_t *me, double ang, double max, int mode, const line_t **hit)
{
    double px = (double)me->x / 65536.0, py = (double)me->y / 65536.0;
    double rx = cos(ang), ry = sin(ang), best = max;
    int i;
    if (hit) *hit = NULL;
    for (i = 0; i < numlines; i++) {
        const line_t *l = &lines[i];
        double x1 = (double)l->v1->x / 65536.0, y1 = (double)l->v1->y / 65536.0;
        double ex = (double)l->dx / 65536.0, ey = (double)l->dy / 65536.0;
        double wx = x1 - px, wy = y1 - py;
        double denom = rx * ey - ry * ex, t, u;
        if (fabs(denom) < 1e-9) continue;
        t = (wx * ey - wy * ex) / denom;
        u = (wx * ry - wy * rx) / denom;
        if (t <= 0.0 || t >= best || u < 0.0 || u > 1.0) continue;
        if (mode == 0 ? !blocks_walk(me, l) : !stops_use(l)) continue;
        best = t;
        if (hit) *hit = l;
    }
    return best;
}

/* Distance from the player to the nearest point of a line, and the bearing of that point. */
static double line_distance(const mobj_t *me, const line_t *l, double *bearing)
{
    double px = (double)me->x / 65536.0, py = (double)me->y / 65536.0;
    double x1 = (double)l->v1->x / 65536.0, y1 = (double)l->v1->y / 65536.0;
    double ex = (double)l->dx / 65536.0, ey = (double)l->dy / 65536.0;
    double len2 = ex * ex + ey * ey, u = 0.0, cx, cy;
    if (len2 > 0.0) u = ((px - x1) * ex + (py - y1) * ey) / len2;
    if (u < 0.0) u = 0.0;
    if (u > 1.0) u = 1.0;
    cx = x1 + u * ex;
    cy = y1 + u * ey;
    *bearing = bearing_to(me, cx, cy);
    return dist_to(me, cx, cy);
}

typedef struct {
    const line_t *line;
    double distance;
    double bearing;
} near_line_t;

static const double WALL_BEARINGS[8] = {0, 45, 90, 135, 180, -135, -90, -45};
static const char *WALL_NAMES[8] = {"ahead",  "ahead_right", "right",      "behind_right",
                                    "behind", "behind_left", "left",       "ahead_left"};

/* ---- the route ----------------------------------------------------------------------------
 *
 * The map is laid over a grid of 32-unit cells. A cell is walkable if no wall, closed window or
 * high step is within a player's radius of its centre. A step between neighbouring cells is allowed
 * unless it crosses something that blocks walking; crossing a door costs extra and needs the key
 * for a key door. A cheapest-path field is solved outwards from the exit switch for the keys held
 * (and cached until they change), and the player follows it downhill to the furthest cell it can
 * see in a straight line, stopping at the first door. Unlike a graph of sectors this does not flip
 * between two openings into the same room, and it finds a way round pits and pillars.
 */

#define CELL 32.0
#define MAX_CELLS 200000
#define WALK_RADIUS 16.0

static int g_w = 0, g_h = 0, g_ok = 0, g_lines = 0, g_field_keys = -1;
static double g_ox = 0.0, g_oy = 0.0;
static unsigned char g_walk[MAX_CELLS];
static unsigned char g_edge[MAX_CELLS][8]; /* 255 blocked, 0 free, 4 door, 5..7 key door (4 + key) */
static int g_dist[MAX_CELLS];
static int g_queue[MAX_CELLS + 1];
static unsigned char g_inq[MAX_CELLS];
static const int DX[8] = {1, 1, 0, -1, -1, -1, 0, 1};
static const int DY[8] = {0, 1, 1, 1, 0, -1, -1, -1};

static double lx1(const line_t *l) { return (double)l->v1->x / 65536.0; }
static double ly1(const line_t *l) { return (double)l->v1->y / 65536.0; }
static double lx2(const line_t *l) { return (double)l->v2->x / 65536.0; }
static double ly2(const line_t *l) { return (double)l->v2->y / 65536.0; }
static double hypot2(double ax, double ay, double bx, double by) { return sqrt((ax - bx) * (ax - bx) + (ay - by) * (ay - by)); }
static int is_exit_line(const line_t *l) { return l->special == 11 || l->special == 51 || l->special == 52 || l->special == 124; }

static double seg_distance(double px, double py, const line_t *l, double *cx, double *cy)
{
    double x1 = lx1(l), y1 = ly1(l), ex = lx2(l) - x1, ey = ly2(l) - y1;
    double len2 = ex * ex + ey * ey, u = 0.0;
    if (len2 > 0.0) u = ((px - x1) * ex + (py - y1) * ey) / len2;
    if (u < 0.0) u = 0.0;
    if (u > 1.0) u = 1.0;
    *cx = x1 + u * ex;
    *cy = y1 + u * ey;
    return hypot2(px, py, *cx, *cy);
}

static fixed_t to_fixed(double v) { return (fixed_t)(v * 65536.0); }
static const sector_t *sector_at(double x, double y) { return R_PointInSubsector(to_fixed(x), to_fixed(y))->sector; }
static double cell_x(int ix) { return g_ox + (ix + 0.5) * CELL; }
static double cell_y(int iy) { return g_oy + (iy + 0.5) * CELL; }

static int cell_walkable(double x, double y)
{
    double z = (double)sector_at(x, y)->floorheight / 65536.0;
    int i;
    for (i = 0; i < numlines; i++) {
        const line_t *l = &lines[i];
        double cx, cy, hi;
        if (fmin(lx1(l), lx2(l)) > x + WALK_RADIUS || fmax(lx1(l), lx2(l)) < x - WALK_RADIUS ||
            fmin(ly1(l), ly2(l)) > y + WALK_RADIUS || fmax(ly1(l), ly2(l)) < y - WALK_RADIUS)
            continue;
        if (seg_distance(x, y, l, &cx, &cy) >= WALK_RADIUS) continue;
        if (l->backsector == NULL || (l->flags & ML_BLOCKING)) return 0;
        if (is_door(l->special)) continue;
        if (!gap_open(l)) return 0;
        hi = (double)max_fixed(l->frontsector->floorheight, l->backsector->floorheight) / 65536.0;
        if (hi - z > 24.0) return 0;
    }
    return 1;
}

/* 255 if the step between two cell centres is blocked, else the kind of door it crosses (0 none). */
static int edge_kind(double ax, double ay, double bx, double by)
{
    double d1x = bx - ax, d1y = by - ay;
    int i, need = 0;
    for (i = 0; i < numlines; i++) {
        const line_t *l = &lines[i];
        double ex = lx2(l) - lx1(l), ey = ly2(l) - ly1(l), denom, t, u;
        if (fmin(lx1(l), lx2(l)) > fmax(ax, bx) || fmax(lx1(l), lx2(l)) < fmin(ax, bx) ||
            fmin(ly1(l), ly2(l)) > fmax(ay, by) || fmax(ly1(l), ly2(l)) < fmin(ay, by))
            continue;
        denom = d1x * ey - d1y * ex;
        if (fabs(denom) < 1e-9) continue;
        t = ((lx1(l) - ax) * ey - (ly1(l) - ay) * ex) / denom;
        u = ((lx1(l) - ax) * d1y - (ly1(l) - ay) * d1x) / denom;
        if (t < 0.0 || t > 1.0 || u < 0.0 || u > 1.0) continue;
        if (l->backsector == NULL || (l->flags & ML_BLOCKING)) return 255;
        if (is_door(l->special)) {
            int k = 4 + door_key(l->special);
            if (k > need) need = k;
            continue;
        }
        if (!gap_open(l)) return 255;
        {
            int side = P_PointOnLineSide(to_fixed(ax), to_fixed(ay), (line_t *)l);
            const sector_t *near_s = side == 0 ? l->frontsector : l->backsector;
            const sector_t *far_s = side == 0 ? l->backsector : l->frontsector;
            if (far_s->floorheight - near_s->floorheight > STEP_HEIGHT) return 255;
        }
    }
    return need;
}

static int build_grid(void)
{
    double minx = 1e30, miny = 1e30, maxx = -1e30, maxy = -1e30;
    int i, ix, iy, d;
    if (numlines <= 0) return 0;
    for (i = 0; i < numlines; i++) {
        minx = fmin(minx, fmin(lx1(&lines[i]), lx2(&lines[i])));
        maxx = fmax(maxx, fmax(lx1(&lines[i]), lx2(&lines[i])));
        miny = fmin(miny, fmin(ly1(&lines[i]), ly2(&lines[i])));
        maxy = fmax(maxy, fmax(ly1(&lines[i]), ly2(&lines[i])));
    }
    g_ox = minx - CELL;
    g_oy = miny - CELL;
    g_w = (int)((maxx - g_ox) / CELL) + 2;
    g_h = (int)((maxy - g_oy) / CELL) + 2;
    if ((long)g_w * g_h > MAX_CELLS) return 0;
    for (ix = 0; ix < g_w; ix++)
        for (iy = 0; iy < g_h; iy++) g_walk[iy * g_w + ix] = (unsigned char)cell_walkable(cell_x(ix), cell_y(iy));
    for (ix = 0; ix < g_w; ix++)
        for (iy = 0; iy < g_h; iy++) {
            int c = iy * g_w + ix;
            for (d = 0; d < 8; d++) {
                int jx = ix + DX[d], jy = iy + DY[d];
                g_edge[c][d] = 255;
                if (!g_walk[c] || jx < 0 || jy < 0 || jx >= g_w || jy >= g_h || !g_walk[jy * g_w + jx]) continue;
                if (DX[d] && DY[d] && !(g_walk[iy * g_w + jx] && g_walk[jy * g_w + ix])) continue;
                g_edge[c][d] = (unsigned char)edge_kind(cell_x(ix), cell_y(iy), cell_x(jx), cell_y(jy));
            }
        }
    return 1;
}

static int keys_mask(const player_t *p)
{
    return (has_key(p, 1) ? 1 : 0) | (has_key(p, 2) ? 2 : 0) | (has_key(p, 3) ? 4 : 0);
}

static int edge_open(int kind, int keys)
{
    if (kind == 255) return 0;
    if (kind >= 5 && !(keys & (1 << (kind - 5)))) return 0;
    return 1;
}

static void solve_field(int keys)
{
    int n = g_w * g_h, i, head = 0, tail = 0;
    for (i = 0; i < n; i++) {
        g_dist[i] = 1 << 28;
        g_inq[i] = 0;
    }
    for (i = 0; i < numlines; i++) {
        const line_t *e = &lines[i];
        double mx, my;
        int ix, iy;
        if (!is_exit_line(e) || e->frontsector == NULL) continue;
        mx = (lx1(e) + lx2(e)) / 2.0;
        my = (ly1(e) + ly2(e)) / 2.0;
        for (ix = (int)((mx - 400.0 - g_ox) / CELL); ix <= (int)((mx + 400.0 - g_ox) / CELL); ix++)
            for (iy = (int)((my - 400.0 - g_oy) / CELL); iy <= (int)((my + 400.0 - g_oy) / CELL); iy++) {
                double x, y, cx, cy;
                int c;
                if (ix < 0 || iy < 0 || ix >= g_w || iy >= g_h) continue;
                c = iy * g_w + ix;
                if (!g_walk[c]) continue;
                x = cell_x(ix);
                y = cell_y(iy);
                if (seg_distance(x, y, e, &cx, &cy) <= 40.0 &&
                    P_PointOnLineSide(to_fixed(x), to_fixed(y), (line_t *)e) == 0 && !g_inq[c]) {
                    g_dist[c] = 0;
                    g_inq[c] = 1;
                    g_queue[tail++] = c;
                    if (tail > MAX_CELLS) tail = 0;
                }
            }
    }
    while (head != tail) {
        int c = g_queue[head++], ix, iy, d;
        if (head > MAX_CELLS) head = 0;
        g_inq[c] = 0;
        ix = c % g_w;
        iy = c / g_w;
        for (d = 0; d < 8; d++) {
            int jx = ix + DX[d], jy = iy + DY[d], j, kind, nd;
            if (jx < 0 || jy < 0 || jx >= g_w || jy >= g_h) continue;
            j = jy * g_w + jx;
            kind = g_edge[j][(d + 4) % 8]; /* the step from j back to c */
            if (!edge_open(kind, keys)) continue;
            nd = g_dist[c] + (DX[d] && DY[d] ? 45 : 32) + (kind >= 4 && kind != 255 ? 64 : 0);
            if (nd < g_dist[j]) {
                g_dist[j] = nd;
                if (!g_inq[j]) {
                    g_inq[j] = 1;
                    g_queue[tail++] = j;
                    if (tail > MAX_CELLS) tail = 0;
                }
            }
        }
    }
}

/* Is the way to a point clear for a body, not just a line? Three rays: the middle and one a little
 * to each side, so the waypoint does not graze a corner the player would catch. */
static int clear_for_body(const mobj_t *me, double ang, double d)
{
    static const double OFFSETS[3] = {0.0, -16.0, 16.0};
    int k;
    for (k = 0; k < 3; k++) {
        mobj_t tmp = *me;
        tmp.x += to_fixed(OFFSETS[k] * -sin(ang));
        tmp.y += to_fixed(OFFSETS[k] * cos(ang));
        if (cast_ray(&tmp, ang, d, 0, NULL) < d - 0.5) return 0;
    }
    return 1;
}

/* Where to walk to reach the exit: a point on the way, whether it is a door, and what remains. */
static int find_route(const player_t *p, const mobj_t *me, int *via_door, double *remaining,
                      int *at_exit, double *wx, double *wy)
{
    double px = (double)me->x / 65536.0, py = (double)me->y / 65536.0;
    int keys = keys_mask(p), ix, iy, dx, dy, i, best_c = -1, door_at = -1, plen = 1, target, limit;
    int path[26];
    double best_cost = 1e30;

    if (g_ok == 0 || g_lines != numlines) {
        g_ok = build_grid() ? 1 : 2; /* 2: too big or empty, do not try again this level */
        g_lines = numlines;
        g_field_keys = -1;
    }
    if (g_ok != 1) return 0;
    if (g_field_keys != keys) {
        solve_field(keys);
        g_field_keys = keys;
    }
    ix = (int)((px - g_ox) / CELL);
    iy = (int)((py - g_oy) / CELL);
    for (dx = -1; dx <= 1; dx++)
        for (dy = -1; dy <= 1; dy++) {
            int jx = ix + dx, jy = iy + dy, c;
            double cost;
            if (jx < 0 || jy < 0 || jx >= g_w || jy >= g_h) continue;
            c = jy * g_w + jx;
            if (g_dist[c] >= (1 << 27)) continue;
            cost = g_dist[c] + hypot2(cell_x(jx), cell_y(jy), px, py);
            if (cost < best_cost) {
                best_cost = cost;
                best_c = c;
            }
        }
    if (best_c < 0) return 0;
    *remaining = best_cost;
    *via_door = 0;
    *at_exit = 0;
    if (g_dist[best_c] == 0) {
        *at_exit = 1;
        return 1;
    }
    path[0] = best_c;
    for (i = 0; i < 24; i++) {
        int c = path[plen - 1], d, nxt = -1, nkind = 0, cix = c % g_w, ciy = c / g_w;
        for (d = 0; d < 8; d++) {
            int jx = cix + DX[d], jy = ciy + DY[d], j;
            if (jx < 0 || jy < 0 || jx >= g_w || jy >= g_h) continue;
            j = jy * g_w + jx;
            if (!edge_open(g_edge[c][d], keys)) continue;
            if (g_dist[j] < g_dist[c] && (nxt < 0 || g_dist[j] < g_dist[nxt])) {
                nxt = j;
                nkind = g_edge[c][d];
            }
        }
        if (nxt < 0) break;
        if (nkind >= 4 && nkind != 255 && door_at < 0) door_at = plen;
        path[plen++] = nxt;
        if (door_at >= 0 || g_dist[nxt] == 0) break;
    }
    target = 0;
    limit = door_at >= 0 ? door_at : plen - 1;
    for (i = 0; i <= limit; i++) {
        double tx = cell_x(path[i] % g_w), ty = cell_y(path[i] / g_w);
        double d = hypot2(tx, ty, px, py);
        if (d < 1.0) {
            target = i;
            continue;
        }
        if (clear_for_body(me, atan2(ty - py, tx - px), d))
            target = i;
        else if (i > 1)
            break;
    }
    *wx = cell_x(path[target] % g_w);
    *wy = cell_y(path[target] / g_w);
    *via_door = door_at >= 0 && target == door_at;
    return 1;
}

/* Appends the scenery fields to the JSON in out. Returns the new length. */
static int write_scenery(const player_t *p, const mobj_t *me, int n)
{
    near_line_t near_lines[MAX_INTERACT];
    int nn = 0, i, j, any_key = 0;
    const line_t *hit = NULL;
    int via_door = 0, at_exit = 0;
    double remaining = 0.0, wx = 0.0, wy = 0.0;
    double face_rad = (double)me->angle * 2.0 * M_PI / 4294967296.0;

    n += snprintf(out + n, OUT_SIZE - n, ",\"walls\":{");
    for (i = 0; i < 8; i++) {
        double d = cast_ray(me, face_rad - WALL_BEARINGS[i] * M_PI / 180.0, RAY_MAX, 0, NULL);
        n += snprintf(out + n, OUT_SIZE - n, "%s\"%s\":%.0f", i ? "," : "", WALL_NAMES[i], d);
    }
    n += snprintf(out + n, OUT_SIZE - n, "}");

    n += snprintf(out + n, OUT_SIZE - n, ",\"keys\":[");
    for (i = 1; i <= 3; i++)
        if (has_key(p, i)) n += snprintf(out + n, OUT_SIZE - n, "%s\"%s\"", any_key++ ? "," : "", key_name(i));
    n += snprintf(out + n, OUT_SIZE - n, "]");

    for (i = 0; i < numlines; i++) {
        near_line_t item;
        double b;
        if (line_kind(&lines[i]) == KIND_NONE) continue;
        item.line = &lines[i];
        item.distance = line_distance(me, &lines[i], &b);
        item.bearing = b;
        if (item.distance > 256.0) continue;
        j = nn;
        if (nn == MAX_INTERACT) {
            if (item.distance >= near_lines[MAX_INTERACT - 1].distance) continue;
            j = MAX_INTERACT - 1;
        } else {
            nn++;
        }
        while (j > 0 && near_lines[j - 1].distance > item.distance) {
            near_lines[j] = near_lines[j - 1];
            j--;
        }
        near_lines[j] = item;
    }
    n += snprintf(out + n, OUT_SIZE - n, ",\"interactables\":[");
    for (i = 0; i < nn; i++) {
        const line_t *l = near_lines[i].line;
        int key = door_key(l->special);
        n += snprintf(out + n, OUT_SIZE - n,
                      "%s{\"kind\":\"%s\",\"distance\":%.1f,\"bearing\":%.2f,\"facing\":%s,"
                      "\"open\":%s,\"locked\":%s}",
                      i ? "," : "", kind_name(line_kind(l)), near_lines[i].distance,
                      near_lines[i].bearing,
                      P_PointOnLineSide(me->x, me->y, (line_t *)l) == 0 ? "true" : "false",
                      gap_open(l) ? "true" : "false", key && !has_key(p, key) ? "true" : "false");
    }
    n += snprintf(out + n, OUT_SIZE - n, "]");

    /* What the use key would do now: the first line within reach straight ahead. */
    cast_ray(me, face_rad, 64.0, 1, &hit);
    if (hit != NULL && line_kind(hit) != KIND_NONE &&
        P_PointOnLineSide(me->x, me->y, (line_t *)hit) == 0) {
        int key = door_key(hit->special);
        n += snprintf(out + n, OUT_SIZE - n,
                      ",\"use_target\":{\"kind\":\"%s\",\"open\":%s,\"locked\":%s}",
                      kind_name(line_kind(hit)), gap_open(hit) ? "true" : "false",
                      key && !has_key(p, key) ? "true" : "false");
    } else {
        n += snprintf(out + n, OUT_SIZE - n, ",\"use_target\":null");
    }

    if (find_route(p, me, &via_door, &remaining, &at_exit, &wx, &wy)) {
        double b = 0.0, d = 0.0;
        const char *via = "portal";
        if (at_exit) {
            exit_bearing(me, &b, &d);
            via = "exit";
        } else {
            b = bearing_to(me, wx, wy);
            d = dist_to(me, wx, wy);
            if (via_door) via = "door";
        }
        n += snprintf(out + n, OUT_SIZE - n,
                      ",\"route\":{\"bearing\":%.2f,\"distance\":%.1f,\"via\":\"%s\",\"remaining\":%.0f}",
                      b, d, via, remaining);
    } else {
        n += snprintf(out + n, OUT_SIZE - n, ",\"route\":null");
    }
    return n;
}

/* mode bits: 1 = scan thinkers, 2 = find the exit, 4 = line-of-sight checks, 8 = scenery. 15 is the
 * real thing;
 * the smaller modes exist so a hang can be traced to one part of the scan. */
EMSCRIPTEN_KEEPALIVE
const char *pdoom_get_state_ex(int mode)
{
    player_t *p = &players[consoleplayer];
    mobj_t *me = p->mo;
    listed_t enemies[MAX_LISTED], pickups[MAX_LISTED];
    int ne = 0, np = 0, i, ammo = 0, n;
    double eb = 0.0, ed = 0.0;
    const char *status = "running";
    thinker_t *th;

    if (me == NULL) {
        snprintf(out, OUT_SIZE, "{\"status\":\"running\",\"loading\":true}");
        return out;
    }

    for (th = thinkercap.next; (mode & 1) && th != &thinkercap; th = th->next) {
        mobj_t *m;
        listed_t item;
        if (th->function.acp1 != (actionf_p1)P_MobjThinker) continue;
        m = (mobj_t *)th;
        if (m == me) continue;
        item.distance = dist_to(me, (double)m->x / 65536.0, (double)m->y / 65536.0);
        item.bearing = bearing_to(me, (double)m->x / 65536.0, (double)m->y / 65536.0);
        if ((m->flags & MF_COUNTKILL) && m->health > 0 && !(m->flags & MF_CORPSE)) {
            item.kind = (int)m->type;
            item.los = (mode & 4) && P_CheckSight(me, m) ? 1 : 0;
            keep_nearest(enemies, &ne, &item);
        } else if (m->flags & MF_SPECIAL) {
            item.kind = (int)m->sprite;
            item.los = 0;
            keep_nearest(pickups, &np, &item);
        }
    }

    for (i = 0; i < NUMAMMO; i++) ammo += p->ammo[i] > 0 ? p->ammo[i] : 0;

    if (p->playerstate == PST_DEAD) status = "died";
    else if (gamestate == GS_INTERMISSION || gameaction == ga_completed) status = "cleared";

    n = snprintf(out, OUT_SIZE,
                 "{\"tic\":%d,\"level\":\"E%dM%d\",\"seed\":%d,\"demo\":%d,\"gs\":%d,\"status\":\"%s\",\"kills\":%d,"
                 "\"player\":{\"health\":%d,\"armor\":%d,\"weapon\":\"%s\","
                 "\"ammo\":{\"total\":%d},\"x\":%.1f,\"y\":%.1f,\"angle\":%.2f},\"enemies\":[",
                 leveltime, gameepisode, gamemap, pending_seed, demoplayback ? 1 : 0, (int)gamestate, status, p->killcount,
                 p->health, p->armorpoints, weapon_name(p->readyweapon), ammo,
                 (double)me->x / 65536.0, (double)me->y / 65536.0, facing_deg(me));

    for (i = 0; i < ne && n < OUT_SIZE - 512; i++)
        n += snprintf(out + n, OUT_SIZE - n,
                      "%s{\"type\":\"%s\",\"distance\":%.1f,\"bearing\":%.2f,\"line_of_sight\":%s}",
                      i ? "," : "", enemy_name((mobjtype_t)enemies[i].kind), enemies[i].distance,
                      enemies[i].bearing, enemies[i].los ? "true" : "false");

    n += snprintf(out + n, OUT_SIZE - n, "],\"pickups\":[");
    for (i = 0; i < np && n < OUT_SIZE - 256; i++)
        n += snprintf(out + n, OUT_SIZE - n, "%s{\"type\":\"%s\",\"distance\":%.1f,\"bearing\":%.2f}",
                      i ? "," : "", pickup_name((spritenum_t)pickups[i].kind), pickups[i].distance,
                      pickups[i].bearing);

    if ((mode & 2) && exit_bearing(me, &eb, &ed))
        n += snprintf(out + n, OUT_SIZE - n, "],\"exit_bearing\":%.2f,\"exit_distance\":%.1f", eb, ed);
    else
        n += snprintf(out + n, OUT_SIZE - n, "],\"exit_bearing\":null,\"exit_distance\":null");
    if (mode & 8) n = write_scenery(p, me, n);
    snprintf(out + n, OUT_SIZE - n, "}");
    return out;
}

EMSCRIPTEN_KEEPALIVE
const char *pdoom_get_state(void)
{
    return pdoom_get_state_ex(15);
}

/* Called from G_InitNew right after M_ClearRandom, so the seed survives level start. */
void pdoom_apply_seed(void)
{
    prndindex = pending_seed & 0xff;
    rndindex = (pending_seed >> 8) & 0xff;
}

EMSCRIPTEN_KEEPALIVE
void pdoom_new_game(int episode, int map, int seed)
{
    pending_seed = seed;
    g_ok = 0;
    paused = false;
    G_DeferedInitNew(sk_medium, episode, map);
}

/* Freeze and unfreeze the world with Doom's own pause flag. P_Ticker returns early while it is
 * set, so leveltime and every thinker stop, while rendering and input keep running. Pausing the
 * Emscripten main loop instead does not resume reliably next to the fork's asyncify sleeps. */
EMSCRIPTEN_KEEPALIVE
void pdoom_pause(void)
{
    paused = true;
}

EMSCRIPTEN_KEEPALIVE
void pdoom_resume(void)
{
    paused = false;
}
