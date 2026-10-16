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
#include <stdlib.h>
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
#define MAX_LINES 4096
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
 * for a key door. A cheapest-path field is solved outwards from a goal and the player follows it
 * downhill to the furthest cell it can see in a straight line, stopping at the first door. Unlike
 * a graph of sectors this does not flip between two openings into the same room, and it finds a
 * way round pits and pillars.
 *
 * Most levels cannot be finished by walking and opening doors: there are keys to fetch, lifts and
 * bridges that a switch or a walk-over line moves, stairs that a line builds, and teleporters. A
 * step across a sector a trigger line moves (a closed door, a raised floor, a lowered lift) is
 * "gated". The plan works in stages. It solves the field from the exit with what is open now. If
 * the player is cut off it solves again assuming every key and every gate is available, follows
 * the cheapest such path from the player, and takes the first thing that blocks it: a key colour
 * becomes the goal "fetch that key", a gate tag the goal "reach a trigger line for that tag". It
 * repeats from the new goal (with the thing it is working on forbidden, so it cannot chase its
 * own tail) until the player can reach the goal, and heads there. Nothing is remembered between
 * calls except which triggers were already tried, so as keys are taken and lifts move the plan
 * moves on by itself. tools/navsim.py keeps the same planner in Python for offline tests.
 */

#define CELL 32.0
#define MAX_CELLS 320000
#define WALK_RADIUS 14.0
#define DOOR_RADIUS 9.0
#define MAX_HOPS 16000
#define MAX_JUMPS 256
#define MAX_GOALS 6000
#define MAX_TAGS 1024
#define MAX_GATES 1024
#define GATE_TABLE 32768
#define KEY_COST 300
#define GATE_COST 150
#define JUMP_COST 100
#define GATED 0x10

typedef struct { int a, b; short len; unsigned char kind; short gate[2]; } hop_t;
typedef struct { int src, dst; short line; } jump_t;
typedef struct { int key; short line[2]; } gate_entry_t;

static int g_w = 0, g_h = 0, g_ok = 0, g_lines = 0, g_nhops = 0, g_njumps = 0, g_ngates = 0;
static double g_ox = 0.0, g_oy = 0.0;
static unsigned char g_walk[MAX_CELLS];
static unsigned char g_edge[MAX_CELLS][8]; /* 255 blocked, else 0 free / 4 door / 5..7 key door (4 + key), | GATED */
static unsigned char g_flag[MAX_CELLS];    /* 1 hop source, 2 hop target, 4 jump target */
static int g_dist[MAX_CELLS];
static int g_queue[MAX_CELLS + 1];
static unsigned char g_inq[MAX_CELLS];
static hop_t g_hops[MAX_HOPS];
static jump_t g_jumps[MAX_JUMPS];
static gate_entry_t g_gate_tab[GATE_TABLE];  /* the lines gating a grid edge, keyed by cell * 8 + direction + 1 */
static unsigned char g_trig_tag[MAX_TAGS];   /* a trigger line moves sectors with this tag */
static unsigned char g_live_tag[MAX_TAGS];   /* ... and one of them has not been used up */
static short g_stair[MAX_SECTORS];           /* tag of the build-stairs trigger that raises this sector */
static short g_gate_lines[MAX_GATES];        /* lines a trigger has to open before they can be crossed */
static unsigned char g_line_open[MAX_GATES][2];
static int g_vis_until[MAX_LINES];           /* a trigger line tried recently is skipped until this tic */
static unsigned char g_ban_key[4], g_ban_tag[MAX_TAGS];
static const int DX[8] = {1, 1, 0, -1, -1, -1, 0, 1};
static const int DY[8] = {0, 1, 1, 1, 0, -1, -1, -1};

static int g_goal[MAX_GOALS], g_ngoal = 0;
static const line_t *g_glines[48];
static int g_nglines = 0;
static double g_gpts[16][2];
static int g_ngpts = 0;
static enum { GOAL_EXIT = 0, GOAL_TRIGGER, GOAL_PICKUP, GOAL_KILL } g_goal_kind = GOAL_EXIT;
static unsigned int g_plan_sig = 0;
static int g_plan_valid = 0, g_plan_goal = 0, g_plan_nglines = 0, g_at_goal_since = -1, g_at_goal_line = -1;
static const line_t *g_plan_glines[48];
static double g_plan_gpts[16][2];
static int g_plan_ngpts = 0;

static double lx1(const line_t *l) { return (double)l->v1->x / 65536.0; }
static double ly1(const line_t *l) { return (double)l->v1->y / 65536.0; }
static double lx2(const line_t *l) { return (double)l->v2->x / 65536.0; }
static double ly2(const line_t *l) { return (double)l->v2->y / 65536.0; }
static double hypot2(double ax, double ay, double bx, double by) { return sqrt((ax - bx) * (ax - bx) + (ay - by) * (ay - by)); }
static int is_exit_line(const line_t *l) { return l->special == 11 || l->special == 51 || l->special == 52 || l->special == 124; }

/* A line special that moves a tagged sector: doors, floors, lifts, stairs. Not lights, teleports
 * or exits. */
static int is_trigger(int sp)
{
    switch (sp) {
    case 0: case 11: case 51: case 52: case 124: case 48: case 39: case 97: case 125: case 126:
    case 12: case 13: case 17: case 35: case 79: case 80: case 81: case 104: case 138: case 139:
    case 156: case 157: case 158: case 159: case 169: case 170: case 171: case 172: case 173:
    case 174: case 175: case 176: case 177: case 178: case 179: case 180:
        return 0;
    default: return 1;
    }
}

static int is_stairs(int sp) { return sp == 7 || sp == 8 || sp == 100 || sp == 127; }

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
static int side_of(const line_t *l, double x, double y) { return P_PointOnLineSide(to_fixed(x), to_fixed(y), (line_t *)l); }

/* 4 + key for a line that is a door or the other face of one, else -1. A door sector starts shut
 * (floor = ceiling) and has its door special on only some of its lines. */
static int door_class(const line_t *l)
{
    const sector_t *s[2];
    int k, e, out = -1;
    if (is_door(l->special)) return 4 + door_key(l->special);
    if (l->backsector == NULL) return -1;
    s[0] = l->frontsector;
    s[1] = l->backsector;
    for (k = 0; k < 2; k++) {
        int key = -1;
        if (s[k]->floorheight != s[k]->ceilingheight) continue;
        for (e = 0; e < s[k]->linecount; e++)
            if (is_door(s[k]->lines[e]->special) && door_key(s[k]->lines[e]->special) > key) key = door_key(s[k]->lines[e]->special);
        if (key >= 0 && 4 + key > out) out = 4 + key;
    }
    return out;
}

/* The tag of the sector a trigger would have to move to make this line passable, or 0. */
static int gate_tag(const line_t *l)
{
    const sector_t *s[2];
    int k;
    s[0] = l->frontsector;
    s[1] = l->backsector;
    for (k = 0; k < 2; k++) {
        int idx;
        if (s[k] == NULL) continue;
        if (s[k]->tag > 0 && s[k]->tag < MAX_TAGS && g_trig_tag[s[k]->tag]) return s[k]->tag;
        idx = (int)(s[k] - sectors);
        if (idx >= 0 && idx < MAX_SECTORS && g_stair[idx]) return g_stair[idx];
    }
    return 0;
}

/* Is a two-sided line shut to walking as it stands (closed gap or too high a step either way)? */
static int shut_either_way(const line_t *l)
{
    if (!gap_open(l)) return 1;
    return abs(l->frontsector->floorheight - l->backsector->floorheight) > STEP_HEIGHT;
}

static int is_gate_line(const line_t *l)
{
    if (l->backsector == NULL || (l->flags & ML_BLOCKING)) return 0;
    if (door_class(l) >= 0) return 1;
    return shut_either_way(l) && gate_tag(l) != 0;
}

/* Does the segment of this line touch the square of half-width h around (x, y)? Doom's player is a
 * square 32 units wide for collision, so its corners catch on wall ends a circle would clear. */
static int line_touches_box(const line_t *l, double x, double y, double h)
{
    double x1 = lx1(l), y1 = ly1(l), dx = lx2(l) - x1, dy = ly2(l) - y1;
    double t0 = 0.0, t1 = 1.0;
    double p[4] = {-dx, dx, -dy, dy};
    double q[4] = {x1 - (x - h), (x + h) - x1, y1 - (y - h), (y + h) - y1};
    int i;
    for (i = 0; i < 4; i++) {
        if (fabs(p[i]) < 1e-12) {
            if (q[i] < 0.0) return 0;
        } else {
            double r = q[i] / p[i];
            if (p[i] < 0.0) {
                if (r > t1) return 0;
                if (r > t0) t0 = r;
            } else {
                if (r < t0) return 0;
                if (r < t1) t1 = r;
            }
        }
    }
    return 1;
}

static int cell_walkable(double x, double y)
{
    double z = (double)sector_at(x, y)->floorheight / 65536.0, box = WALK_RADIUS;
    int i;
    /* a throat beside a door or a trigger-moved sector may be too tight for the full box */
    for (i = 0; i < g_ngates; i++) {
        double cx, cy;
        if (seg_distance(x, y, &lines[g_gate_lines[i]], &cx, &cy) < 40.0) {
            box = DOOR_RADIUS;
            break;
        }
    }
    for (i = 0; i < numlines; i++) {
        const line_t *l = &lines[i];
        double hi;
        if (fmin(lx1(l), lx2(l)) > x + box || fmax(lx1(l), lx2(l)) < x - box ||
            fmin(ly1(l), ly2(l)) > y + box || fmax(ly1(l), ly2(l)) < y - box)
            continue;
        if (!line_touches_box(l, x, y, box)) continue;
        if (l->backsector == NULL || (l->flags & ML_BLOCKING)) return 0;
        if (door_class(l) >= 0) continue;
        hi = (double)max_fixed(l->frontsector->floorheight, l->backsector->floorheight) / 65536.0;
        if (!gap_open(l) || hi - z > 24.0) {
            if (gate_tag(l) == 0) return 0;
        }
    }
    return 1;
}

/* 255 if the step between two cell centres can never be made, else the kind of door it crosses
 * (0 none, 4 plain, 5..7 key door), with GATED set when a trigger has to move something first.
 * gates receives up to two of the gating lines. */
static int edge_info(double ax, double ay, double bx, double by, short *gates)
{
    double d1x = bx - ax, d1y = by - ay;
    int i, need = 0, ng = 0, first_class = -1;
    double first_t = 1e30;
    gates[0] = gates[1] = -1;
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
        if (door_class(l) >= 0) {
            /* the key is the one on the face you press from: a door sector can need a key from
             * one side and none from the other */
            int cls = is_door(l->special) && side_of(l, ax, ay) == 0 ? 4 + door_key(l->special) : 4;
            if (t < first_t) {
                first_t = t;
                first_class = cls;
            }
            continue;
        }
        {
            int side = side_of(l, ax, ay);
            const sector_t *near_s = side == 0 ? l->frontsector : l->backsector;
            const sector_t *far_s = side == 0 ? l->backsector : l->frontsector;
            if (!gap_open(l) || far_s->floorheight - near_s->floorheight > STEP_HEIGHT) {
                if (gate_tag(l) == 0) return 255;
                if (ng < 2) gates[ng++] = (short)i;
            }
        }
    }
    if (first_class >= 0) need = first_class;
    return need | (ng ? GATED : 0);
}

static int g_gate_count = 0;

static int gate_put(int key, const short *gates)
{
    unsigned int h = ((unsigned int)key * 2654435761u) % GATE_TABLE;
    if (g_gate_count >= GATE_TABLE - GATE_TABLE / 8) return 0; /* full: treat the step as blocked */
    g_gate_count++;
    while (g_gate_tab[h].key != 0) h = (h + 1) % GATE_TABLE;
    g_gate_tab[h].key = key;
    g_gate_tab[h].line[0] = gates[0];
    g_gate_tab[h].line[1] = gates[1];
    return 1;
}

static const gate_entry_t *gate_get(int key)
{
    unsigned int h = ((unsigned int)key * 2654435761u) % GATE_TABLE;
    while (g_gate_tab[h].key != 0) {
        if (g_gate_tab[h].key == key) return &g_gate_tab[h];
        h = (h + 1) % GATE_TABLE;
    }
    return NULL;
}

static int nearest_walkable(double x, double y)
{
    int ix = (int)((x - g_ox) / CELL), iy = (int)((y - g_oy) / CELL), r, dx, dy, best = -1;
    double bd = 1e30;
    for (r = 0; r < 4 && best < 0; r++)
        for (dx = -r; dx <= r; dx++)
            for (dy = -r; dy <= r; dy++) {
                int jx = ix + dx, jy = iy + dy;
                double d;
                if (jx < 0 || jy < 0 || jx >= g_w || jy >= g_h || !g_walk[jy * g_w + jx]) continue;
                d = hypot2(cell_x(jx), cell_y(jy), x, y);
                if (d < bd) {
                    bd = d;
                    best = jy * g_w + jx;
                }
            }
    return best;
}

static void build_tags(void)
{
    int i;
    memset(g_trig_tag, 0, sizeof g_trig_tag);
    memset(g_stair, 0, sizeof g_stair);
    for (i = 0; i < numlines; i++)
        if (lines[i].tag > 0 && lines[i].tag < MAX_TAGS && is_trigger(lines[i].special)) g_trig_tag[lines[i].tag] = 1;
    /* build-stairs: only the first step is tagged, the others are the neighbours that start level */
    for (i = 0; i < numlines; i++) {
        int s, todo[64], nt = 0, tag = lines[i].tag;
        if (!is_stairs(lines[i].special) || tag <= 0 || tag >= MAX_TAGS) continue;
        for (s = 0; s < numsectors && s < MAX_SECTORS; s++)
            if (sectors[s].tag == tag && nt < 64) {
                g_stair[s] = (short)tag;
                todo[nt++] = s;
            }
        while (nt > 0) {
            int cur = todo[--nt], k;
            for (k = 0; k < sectors[cur].linecount; k++) {
                const line_t *e = sectors[cur].lines[k];
                const sector_t *o;
                int oi;
                if (e->backsector == NULL) continue;
                o = e->frontsector == &sectors[cur] ? e->backsector : e->frontsector;
                oi = (int)(o - sectors);
                if (oi < 0 || oi >= MAX_SECTORS || g_stair[oi] || o->floorheight != sectors[cur].floorheight) continue;
                g_stair[oi] = (short)tag;
                if (nt < 64) todo[nt++] = oi;
            }
        }
    }
    g_trig_tag[666] = g_trig_tag[667] = 1; /* moved when a map's bosses die (A_BossDeath), not by a line */
    g_ngates = 0;
    for (i = 0; i < numlines && g_ngates < MAX_GATES; i++)
        if (is_gate_line(&lines[i])) g_gate_lines[g_ngates++] = (short)i;
}

static int build_grid(void)
{
    double minx = 1e30, miny = 1e30, maxx = -1e30, maxy = -1e30;
    int i, ix, iy, d;
    thinker_t *th;
    if (numlines <= 0 || numlines > MAX_LINES) return 0;
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
    build_tags();
    memset(g_gate_tab, 0, sizeof g_gate_tab);
    g_gate_count = 0;
    memset(g_flag, 0, (size_t)g_w * g_h);
    memset(g_vis_until, 0, sizeof g_vis_until);
    g_nhops = g_njumps = 0;
    for (ix = 0; ix < g_w; ix++)
        for (iy = 0; iy < g_h; iy++) g_walk[iy * g_w + ix] = (unsigned char)cell_walkable(cell_x(ix), cell_y(iy));
    for (ix = 0; ix < g_w; ix++)
        for (iy = 0; iy < g_h; iy++) {
            int c = iy * g_w + ix;
            for (d = 0; d < 8; d++) {
                int jx = ix + DX[d], jy = iy + DY[d], kind;
                short gates[2];
                g_edge[c][d] = 255;
                if (!g_walk[c] || jx < 0 || jy < 0 || jx >= g_w || jy >= g_h || !g_walk[jy * g_w + jx]) continue;
                if (DX[d] && DY[d] && !(g_walk[iy * g_w + jx] && g_walk[jy * g_w + ix])) continue;
                kind = edge_info(cell_x(ix), cell_y(iy), cell_x(jx), cell_y(jy), gates);
                g_edge[c][d] = (unsigned char)kind;
                if (kind != 255 && (kind & GATED) && !gate_put(c * 8 + d + 1, gates)) g_edge[c][d] = 255;
            }
        }
    /* a door or trigger-moved sector can be thinner than a cell, so no cell lies inside it: hop
     * from a cell on one side to a cell on the other */
    for (i = 0; i < g_ngates; i++) {
        const line_t *gl = &lines[g_gate_lines[i]];
        double mx = (lx1(gl) + lx2(gl)) / 2.0, my = (ly1(gl) + ly2(gl)) / 2.0;
        double span = hypot2(lx1(gl), ly1(gl), lx2(gl), ly2(gl)) / 2.0 + 48.0 + CELL;
        for (ix = (int)((mx - span - g_ox) / CELL); ix <= (int)((mx + span - g_ox) / CELL); ix++)
            for (iy = (int)((my - span - g_oy) / CELL); iy <= (int)((my + span - g_oy) / CELL); iy++) {
                int dx, dy, c;
                double cx, cy;
                if (ix < 0 || iy < 0 || ix >= g_w || iy >= g_h) continue;
                c = iy * g_w + ix;
                if (!g_walk[c] || (g_flag[c] & 8) || seg_distance(cell_x(ix), cell_y(iy), gl, &cx, &cy) > 48.0) continue;
                g_flag[c] |= 8; /* each cell once, whichever gate line found it */
                for (dx = -3; dx <= 3; dx++)
                    for (dy = -3; dy <= 3; dy++) {
                        int jx = ix + dx, jy = iy + dy, kind;
                        short gates[2];
                        if (abs(dx) < 2 && abs(dy) < 2) continue;
                        if (jx < 0 || jy < 0 || jx >= g_w || jy >= g_h || !g_walk[jy * g_w + jx]) continue;
                        kind = edge_info(cell_x(ix), cell_y(iy), cell_x(jx), cell_y(jy), gates);
                        if (kind == 255 || ((kind & 7) < 4 && !(kind & GATED)) || g_nhops >= MAX_HOPS) continue;
                        g_hops[g_nhops].a = c;
                        g_hops[g_nhops].b = jy * g_w + jx;
                        g_hops[g_nhops].len = (short)(hypot2(0, 0, dx, dy) * CELL);
                        g_hops[g_nhops].kind = (unsigned char)kind;
                        g_hops[g_nhops].gate[0] = gates[0];
                        g_hops[g_nhops].gate[1] = gates[1];
                        g_flag[c] |= 1;
                        g_flag[jy * g_w + jx] |= 2;
                        g_nhops++;
                    }
            }
    }
    /* teleporters: standing on the line sends the player to the destination */
    for (i = 0; i < numlines; i++) {
        const line_t *l = &lines[i];
        double mx, my, span;
        int dcell = -1, ix2, iy2;
        if ((l->special != 39 && l->special != 97) || l->tag == 0) continue;
        for (th = thinkercap.next; th != &thinkercap; th = th->next) {
            mobj_t *m;
            if (th->function.acp1 != (actionf_p1)P_MobjThinker) continue;
            m = (mobj_t *)th;
            if (m->type != MT_TELEPORTMAN || m->subsector->sector->tag != l->tag) continue;
            dcell = nearest_walkable((double)m->x / 65536.0, (double)m->y / 65536.0);
            break;
        }
        if (dcell < 0) continue;
        mx = (lx1(l) + lx2(l)) / 2.0;
        my = (ly1(l) + ly2(l)) / 2.0;
        span = hypot2(lx1(l), ly1(l), lx2(l), ly2(l)) / 2.0 + 24.0 + CELL;
        for (ix2 = (int)((mx - span - g_ox) / CELL); ix2 <= (int)((mx + span - g_ox) / CELL); ix2++)
            for (iy2 = (int)((my - span - g_oy) / CELL); iy2 <= (int)((my + span - g_oy) / CELL); iy2++) {
                double cx, cy;
                int c;
                if (ix2 < 0 || iy2 < 0 || ix2 >= g_w || iy2 >= g_h || g_njumps >= MAX_JUMPS) continue;
                c = iy2 * g_w + ix2;
                if (!g_walk[c] || seg_distance(cell_x(ix2), cell_y(iy2), l, &cx, &cy) > 24.0) continue;
                g_jumps[g_njumps].src = c;
                g_jumps[g_njumps].dst = dcell;
                g_jumps[g_njumps].line = (short)i;
                g_flag[dcell] |= 4;
                g_njumps++;
            }
    }
    return 1;
}

static int keys_mask(const player_t *p)
{
    return (has_key(p, 1) ? 1 : 0) | (has_key(p, 2) ? 2 : 0) | (has_key(p, 3) ? 4 : 0);
}

static int is_boss(const mobj_t *m)
{
    return (m->type == MT_BRUISER || m->type == MT_CYBORG || m->type == MT_SPIDER) && m->health > 0;
}

/* Which trigger tags still have a line to press, and whether each gate line is crossable now. */
static void refresh_live(void)
{
    int i;
    memset(g_live_tag, 0, sizeof g_live_tag);
    for (i = 0; i < numlines; i++)
        if (lines[i].tag > 0 && lines[i].tag < MAX_TAGS && is_trigger(lines[i].special)) g_live_tag[lines[i].tag] = 1;
    {
        thinker_t *th;
        for (th = thinkercap.next; th != &thinkercap; th = th->next)
            if (th->function.acp1 == (actionf_p1)P_MobjThinker && is_boss((mobj_t *)th)) g_live_tag[666] = g_live_tag[667] = 1;
    }
    for (i = 0; i < g_ngates; i++) {
        const line_t *l = &lines[g_gate_lines[i]];
        int tag = gate_tag(l), side;
        for (side = 0; side < 2; side++) {
            const sector_t *near_s = side == 0 ? l->frontsector : l->backsector;
            const sector_t *far_s = side == 0 ? l->backsector : l->frontsector;
            int open = gap_open(l) && far_s->floorheight - near_s->floorheight <= STEP_HEIGHT;
            /* a trigger that was used up and left the sector moving or moved counts as open */
            if (!open && tag && !g_live_tag[tag]) open = 1;
            g_line_open[i][side] = (unsigned char)open;
        }
    }
}

static int gate_index(int line)
{
    int i;
    for (i = 0; i < g_ngates; i++)
        if (g_gate_lines[i] == line) return i;
    return -1;
}

/* Cost of a step of this length, or -1. optimistic: missing keys and shut gates are allowed at a
 * price unless forbidden. *miss_key / *miss_tag report the first thing that is missing. */
static int step_cost(int kind, int len, const short *gates, double ax, double ay, int keys, int optimistic,
                     int *miss_key, int *miss_tag)
{
    int cost = len, base = kind & 7, k;
    if (kind == 255) return -1;
    if (base >= 4) cost += 64;
    if (base >= 5 && !(keys & (1 << (base - 5)))) {
        if (!optimistic || g_ban_key[base - 4]) return -1;
        cost += KEY_COST;
        if (miss_key && !*miss_key) *miss_key = base - 4;
    }
    if (kind & GATED) {
        for (k = 0; k < 2; k++) {
            int gi, side;
            const line_t *l;
            if (gates[k] < 0) continue;
            l = &lines[gates[k]];
            gi = gate_index(gates[k]);
            side = side_of(l, ax, ay);
            if (gi >= 0 && g_line_open[gi][side]) continue;
            {
                int tag = gate_tag(l);
                if (!optimistic || (tag > 0 && g_ban_tag[tag])) return -1;
                cost += GATE_COST;
                if (miss_tag && !*miss_tag) *miss_tag = tag;
            }
        }
    }
    return cost;
}

static void relax(int to, int nd, int *tail)
{
    if (nd < g_dist[to]) {
        g_dist[to] = nd;
        if (!g_inq[to]) {
            g_inq[to] = 1;
            g_queue[(*tail)++] = to;
            if (*tail > MAX_CELLS) *tail = 0;
        }
    }
}

/* The cheapest cost to the current goal cells from everywhere, into g_dist. */
static void solve_field(int keys, int optimistic)
{
    int n = g_w * g_h, i, head = 0, tail = 0;
    for (i = 0; i < n; i++) {
        g_dist[i] = 1 << 28;
        g_inq[i] = 0;
    }
    for (i = 0; i < g_ngoal; i++) {
        int c = g_goal[i];
        g_dist[c] = 0;
        if (!g_inq[c]) {
            g_inq[c] = 1;
            g_queue[tail++] = c;
        }
    }
    while (head != tail) {
        int c = g_queue[head++], ix, iy, d, k;
        if (head > MAX_CELLS) head = 0;
        g_inq[c] = 0;
        ix = c % g_w;
        iy = c / g_w;
        for (d = 0; d < 8; d++) {
            int jx = ix + DX[d], jy = iy + DY[d], j, cost, e = (d + 4) % 8;
            short gates[2] = {-1, -1};
            if (jx < 0 || jy < 0 || jx >= g_w || jy >= g_h) continue;
            j = jy * g_w + jx;
            if (g_edge[j][e] == 255) continue;
            if (g_edge[j][e] & GATED) {
                const gate_entry_t *ge = gate_get(j * 8 + e + 1);
                if (ge) {
                    gates[0] = ge->line[0];
                    gates[1] = ge->line[1];
                }
            }
            cost = step_cost(g_edge[j][e], DX[d] && DY[d] ? 45 : 32, gates, cell_x(jx), cell_y(jy), keys, optimistic, NULL, NULL);
            if (cost >= 0) relax(j, g_dist[c] + cost, &tail);
        }
        if (g_flag[c] & 2)
            for (k = 0; k < g_nhops; k++) {
                int cost;
                if (g_hops[k].b != c) continue;
                cost = step_cost(g_hops[k].kind, g_hops[k].len, g_hops[k].gate, cell_x(g_hops[k].a % g_w),
                                 cell_y(g_hops[k].a / g_w), keys, optimistic, NULL, NULL);
                if (cost >= 0) relax(g_hops[k].a, g_dist[c] + cost, &tail);
            }
        if (g_flag[c] & 4)
            for (k = 0; k < g_njumps; k++)
                if (g_jumps[k].dst == c) relax(g_jumps[k].src, g_dist[c] + JUMP_COST, &tail);
    }
}

/* Is the way to a point clear for a body, not just a line? Three rays: the middle and one a little
 * to each side, so the waypoint does not graze a corner the player would catch. */
static int clear_for_body(const mobj_t *me, double ang, double d)
{
    /* Doom's player is a 32-unit square, so across a diagonal it is wider than 32. */
    double half = 16.0 * (fabs(cos(ang)) + fabs(sin(ang))) + 1.0;
    double offsets[3];
    int k;
    offsets[0] = 0.0;
    offsets[1] = -half;
    offsets[2] = half;
    for (k = 0; k < 3; k++) {
        mobj_t tmp = *me;
        tmp.x += to_fixed(offsets[k] * -sin(ang));
        tmp.y += to_fixed(offsets[k] * cos(ang));
        if (cast_ray(&tmp, ang, d, 0, NULL) < d - 0.5) return 0;
    }
    return 1;
}

typedef struct { int cell; int how; int miss_key; int miss_tag; const line_t *jump_line; } step_t;
enum { HOW_WALK = 0, HOW_DOOR = 1, HOW_JUMP = 2 };

/* Walk downhill on g_dist from a cell. Fills path/steps, returns the number of cells. */
static int descend(int start, int keys, int optimistic, int max_steps, step_t *path)
{
    int n = 1, i;
    path[0].cell = start;
    path[0].how = HOW_WALK;
    path[0].miss_key = path[0].miss_tag = 0;
    path[0].jump_line = NULL;
    for (i = 0; i < max_steps; i++) {
        int c = path[n - 1].cell, cix = c % g_w, ciy = c / g_w, d, k, bestcost = 1 << 29;
        step_t best;
        best.cell = -1;
        if (g_dist[c] == 0) break;
        for (d = 0; d < 8; d++) {
            int jx = cix + DX[d], jy = ciy + DY[d], j, cost, mk = 0, mt = 0;
            short gates[2] = {-1, -1};
            if (jx < 0 || jy < 0 || jx >= g_w || jy >= g_h || g_edge[c][d] == 255) continue;
            j = jy * g_w + jx;
            if (g_dist[j] >= g_dist[c]) continue;
            if (g_edge[c][d] & GATED) {
                const gate_entry_t *ge = gate_get(c * 8 + d + 1);
                if (ge) {
                    gates[0] = ge->line[0];
                    gates[1] = ge->line[1];
                }
            }
            cost = step_cost(g_edge[c][d], DX[d] && DY[d] ? 45 : 32, gates, cell_x(cix), cell_y(ciy), keys, optimistic, &mk, &mt);
            if (cost < 0 || cost + g_dist[j] >= bestcost) continue;
            bestcost = cost + g_dist[j];
            best.cell = j;
            best.how = (g_edge[c][d] & 7) >= 4 ? HOW_DOOR : HOW_WALK;
            best.miss_key = mk;
            best.miss_tag = mt;
            best.jump_line = NULL;
        }
        if (g_flag[c] & 1)
            for (k = 0; k < g_nhops; k++) {
                int cost, mk = 0, mt = 0, j = g_hops[k].b;
                if (g_hops[k].a != c || g_dist[j] >= g_dist[c]) continue;
                cost = step_cost(g_hops[k].kind, g_hops[k].len, g_hops[k].gate, cell_x(cix), cell_y(ciy), keys, optimistic, &mk, &mt);
                if (cost < 0 || cost + g_dist[j] >= bestcost) continue;
                bestcost = cost + g_dist[j];
                best.cell = j;
                best.how = (g_hops[k].kind & 7) >= 4 ? HOW_DOOR : HOW_WALK;
                best.miss_key = mk;
                best.miss_tag = mt;
                best.jump_line = NULL;
            }
        for (k = 0; k < g_njumps; k++) {
            int j = g_jumps[k].dst;
            if (g_jumps[k].src != c || g_dist[j] + JUMP_COST >= bestcost + 1) continue;
            bestcost = g_dist[j] + JUMP_COST;
            best.cell = j;
            best.how = HOW_JUMP;
            best.miss_key = best.miss_tag = 0;
            best.jump_line = &lines[g_jumps[k].line];
        }
        if (best.cell < 0) break;
        path[n++] = best;
    }
    return n;
}

static int player_cell(const mobj_t *me, double *cost_out)
{
    double px = (double)me->x / 65536.0, py = (double)me->y / 65536.0, best_cost = 1e30;
    int ix = (int)((px - g_ox) / CELL), iy = (int)((py - g_oy) / CELL), dx, dy, best_c = -1;
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
    *cost_out = best_cost;
    return best_c;
}

static void add_goal(int c)
{
    int i;
    if (c < 0) return;
    for (i = 0; i < g_ngoal; i++)
        if (g_goal[i] == c) return;
    if (g_ngoal < MAX_GOALS) g_goal[g_ngoal++] = c;
}

static void goal_cells_near_line(const line_t *l, double reach, int any_side)
{
    double mx = (lx1(l) + lx2(l)) / 2.0, my = (ly1(l) + ly2(l)) / 2.0;
    double span = hypot2(lx1(l), ly1(l), lx2(l), ly2(l)) / 2.0 + reach + CELL;
    int ix, iy;
    for (ix = (int)((mx - span - g_ox) / CELL); ix <= (int)((mx + span - g_ox) / CELL); ix++)
        for (iy = (int)((my - span - g_oy) / CELL); iy <= (int)((my + span - g_oy) / CELL); iy++) {
            double cx, cy;
            if (ix < 0 || iy < 0 || ix >= g_w || iy >= g_h || !g_walk[iy * g_w + ix]) continue;
            if (seg_distance(cell_x(ix), cell_y(iy), l, &cx, &cy) <= reach &&
                (any_side || side_of(l, cell_x(ix), cell_y(iy)) == 0))
                add_goal(iy * g_w + ix);
        }
}

static void goal_cells_near_point(double x, double y, double reach)
{
    int ix = (int)((x - g_ox) / CELL), iy = (int)((y - g_oy) / CELL), dx, dy, before = g_ngoal;
    for (dx = -2; dx <= 2; dx++)
        for (dy = -2; dy <= 2; dy++) {
            int jx = ix + dx, jy = iy + dy;
            if (jx < 0 || jy < 0 || jx >= g_w || jy >= g_h || !g_walk[jy * g_w + jx]) continue;
            if (fabs(cell_x(jx) - x) <= reach && fabs(cell_y(jy) - y) <= reach) add_goal(jy * g_w + jx);
        }
    if (g_ngoal == before) add_goal(nearest_walkable(x, y));
}

static int key_of_sprite(spritenum_t s)
{
    switch (s) {
    case SPR_BKEY: case SPR_BSKU: return 1;
    case SPR_YKEY: case SPR_YSKU: return 2;
    case SPR_RKEY: case SPR_RSKU: return 3;
    default: return 0;
    }
}

/* Walk to a boss. A map's bosses dying also moves the sectors tagged 666 or 667. */
static int goals_boss(void)
{
    thinker_t *th;
    g_ngoal = g_nglines = g_ngpts = 0;
    g_goal_kind = GOAL_KILL;
    for (th = thinkercap.next; th != &thinkercap; th = th->next) {
        mobj_t *m;
        if (th->function.acp1 != (actionf_p1)P_MobjThinker) continue;
        m = (mobj_t *)th;
        if (!is_boss(m)) continue;
        add_goal(nearest_walkable((double)m->x / 65536.0, (double)m->y / 65536.0));
        if (g_ngpts < 16) {
            g_gpts[g_ngpts][0] = (double)m->x / 65536.0;
            g_gpts[g_ngpts][1] = (double)m->y / 65536.0;
            g_ngpts++;
        }
    }
    return g_ngoal > 0;
}

static void goals_exit(void)
{
    int i, pass;
    g_ngoal = g_nglines = g_ngpts = 0;
    g_goal_kind = GOAL_EXIT;
    for (pass = 0; pass < 2 && g_ngoal == 0; pass++)
        for (i = 0; i < numlines; i++) {
            const line_t *e = &lines[i];
            if (!is_exit_line(e) || e->frontsector == NULL) continue;
            if (pass == 0 && e->special == 51) continue; /* the secret exit only if there is no other */
            {
                int before = g_ngoal;
                goal_cells_near_line(e, 40.0, 0);
                /* a small alcove may have no cell centre that close: stand anywhere in reach */
                if (g_ngoal == before) goal_cells_near_line(e, 72.0, 0);
            }
            if (g_nglines < 48) g_glines[g_nglines++] = e;
        }
    if (g_ngoal > 0) return;
    /* no exit line (the last map of each episode): it ends when the boss dies. Barons on E1M8,
     * the Cyberdemon on E2M8 and E4M6, the Spider Mastermind on E3M8 and E4M8. */
    goals_boss();
}

static int goals_key(int k)
{
    thinker_t *th;
    g_ngoal = g_nglines = g_ngpts = 0;
    g_goal_kind = GOAL_PICKUP;
    for (th = thinkercap.next; th != &thinkercap; th = th->next) {
        mobj_t *m;
        if (th->function.acp1 != (actionf_p1)P_MobjThinker) continue;
        m = (mobj_t *)th;
        if (!(m->flags & MF_SPECIAL) || key_of_sprite(m->sprite) != k) continue;
        goal_cells_near_point((double)m->x / 65536.0, (double)m->y / 65536.0, 48.0);
        if (g_ngpts < 16) {
            g_gpts[g_ngpts][0] = (double)m->x / 65536.0;
            g_gpts[g_ngpts][1] = (double)m->y / 65536.0;
            g_ngpts++;
        }
    }
    return g_ngoal > 0;
}

static int goals_trigger(int tag, int now)
{
    int i;
    if (tag == 666 || tag == 667) return goals_boss();
    g_ngoal = g_nglines = g_ngpts = 0;
    g_goal_kind = GOAL_TRIGGER;
    for (i = 0; i < numlines; i++) {
        const line_t *l = &lines[i];
        if (l->tag != tag || !is_trigger(l->special) || g_vis_until[i] > now) continue;
        {
            int use = is_switch(l->special), before = g_ngoal;
            goal_cells_near_line(l, use ? 40.0 : 24.0, !use);
            if (g_ngoal == before) goal_cells_near_line(l, use ? 72.0 : 48.0, !use);
        }
        if (g_nglines < 48) g_glines[g_nglines++] = l;
    }
    return g_ngoal > 0;
}

typedef struct { int via; double remaining, wx, wy; } plan_t;
enum { VIA_PORTAL = 0, VIA_DOOR, VIA_EXIT, VIA_SWITCH, VIA_WALK };

static unsigned int plan_signature(int keys, int now)
{
    unsigned int h = 2166136261u;
    int i;
    h = (h ^ (unsigned int)keys) * 16777619u;
    for (i = 0; i < g_ngates; i++) h = (h ^ (unsigned int)(g_line_open[i][0] | (g_line_open[i][1] << 1))) * 16777619u;
    for (i = 0; i < MAX_TAGS; i += 8) {
        int k;
        unsigned int b = 0;
        for (k = 0; k < 8; k++) b |= (unsigned int)g_live_tag[i + k] << k;
        h = (h ^ b) * 16777619u;
    }
    for (i = 0; i < numlines; i++)
        if (g_vis_until[i] > now) h = (h ^ (unsigned int)i) * 16777619u;
    {
        thinker_t *th; /* the goals themselves: which keys and Barons are still about */
        for (th = thinkercap.next; th != &thinkercap; th = th->next) {
            mobj_t *m;
            if (th->function.acp1 != (actionf_p1)P_MobjThinker) continue;
            m = (mobj_t *)th;
            if ((m->flags & MF_SPECIAL) && key_of_sprite(m->sprite)) h = (h ^ (unsigned int)(m->x >> 16)) * 16777619u;
            if ((m->type == MT_BRUISER || m->type == MT_CYBORG || m->type == MT_SPIDER) && m->health > 0) h = (h ^ (unsigned int)(now / 35)) * 16777619u;
        }
    }
    return h;
}

/* Where to head, given that g_dist is a strict field the player can use. */
static int follow(const mobj_t *me, int keys, int cell, double cost, plan_t *res, int now)
{
    double px = (double)me->x / 65536.0, py = (double)me->y / 65536.0;
    static step_t path[26];
    int n, i, door_at = -1, jump_at = -1, target = 0, limit;
    res->remaining = cost;
    if (g_dist[cell] == 0) {
        const line_t *nearest = NULL;
        double best = 1e30, bx = px, by = py;
        for (i = 0; i < g_plan_nglines; i++) {
            double cx, cy, d = seg_distance(px, py, g_plan_glines[i], &cx, &cy);
            if (d < best) {
                best = d;
                nearest = g_plan_glines[i];
                bx = cx;
                by = cy;
            }
        }
        for (i = 0; i < g_plan_ngpts; i++) {
            double d = hypot2(px, py, g_plan_gpts[i][0], g_plan_gpts[i][1]);
            if (d < best) {
                best = d;
                nearest = NULL;
                bx = g_plan_gpts[i][0];
                by = g_plan_gpts[i][1];
            }
        }
        res->wx = bx;
        res->wy = by;
        res->via = VIA_PORTAL;
        if (g_plan_goal == GOAL_EXIT) res->via = VIA_EXIT;
        else if (g_plan_goal == GOAL_TRIGGER && nearest != NULL) {
            res->via = is_switch(nearest->special) ? VIA_SWITCH : VIA_WALK;
            /* a repeatable trigger stays in the list once used: give up on it after a while */
            if (best < 48.0) {
                int li = (int)(nearest - lines);
                if (g_at_goal_line != li) {
                    g_at_goal_line = li;
                    g_at_goal_since = now;
                } else if (now - g_at_goal_since > 70 || (res->via == VIA_WALK && best < 12.0)) {
                    g_vis_until[li] = now + 700;
                    g_plan_valid = 0;
                }
            }
        }
        return 1;
    }
    n = descend(cell, keys, 0, 24, path);
    for (i = 1; i < n; i++) {
        if (path[i].how == HOW_JUMP) {
            jump_at = i;
            break;
        }
        if (path[i].how == HOW_DOOR) {
            door_at = i;
            break;
        }
    }
    if (jump_at >= 0) {
        double cx, cy;
        seg_distance(px, py, path[jump_at].jump_line, &cx, &cy);
        res->wx = cx;
        res->wy = cy;
        res->via = VIA_WALK;
        return 1;
    }
    limit = door_at >= 0 ? door_at : n - 1; /* door_at: the cell across the door */
    for (i = 0; i <= limit; i++) {
        double tx = cell_x(path[i].cell % g_w), ty = cell_y(path[i].cell / g_w);
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
    res->wx = cell_x(path[target].cell % g_w);
    res->wy = cell_y(path[target].cell / g_w);
    res->via = (door_at >= 0 && target == door_at) ? VIA_DOOR : VIA_PORTAL;
    return 1;
}

/* The next place to go. Returns 0 when no plan exists (the map has no way through). */
static int plan_route(const player_t *p, const mobj_t *me, plan_t *res)
{
    int keys = keys_mask(p), now = leveltime, stage, cell;
    double cost;
    unsigned int sig;

    if (g_ok == 0 || g_lines != numlines) {
        g_ok = build_grid() ? 1 : 2; /* 2: too big or empty, do not try again this level */
        g_lines = numlines;
        g_plan_valid = 0;
    }
    if (g_ok != 1) return 0;
    refresh_live();
    sig = plan_signature(keys, now);
    if (g_plan_valid && sig == g_plan_sig) {
        cell = player_cell(me, &cost);
        if (cell >= 0) return follow(me, keys, cell, cost, res, now);
    }
    g_plan_valid = 0;
    memset(g_ban_key, 0, sizeof g_ban_key);
    memset(g_ban_tag, 0, sizeof g_ban_tag);
    goals_exit();
    for (stage = 0; stage < 7; stage++) {
        static step_t path[800];
        int n, i, miss_key = 0, miss_tag = 0;
        if (g_ngoal == 0) return 0;
        solve_field(keys, 0);
        cell = player_cell(me, &cost);
        if (cell >= 0) {
            g_plan_sig = sig;
            g_plan_valid = 1;
            g_plan_goal = g_goal_kind;
            g_plan_nglines = g_nglines;
            memcpy(g_plan_glines, g_glines, sizeof(g_glines[0]) * (size_t)g_nglines);
            g_plan_ngpts = g_ngpts;
            memcpy(g_plan_gpts, g_gpts, sizeof g_gpts);
            return follow(me, keys, cell, cost, res, now);
        }
        solve_field(keys, 1);
        cell = player_cell(me, &cost);
        if (cell < 0) return 0;
        n = descend(cell, keys, 1, 799, path);
        for (i = 1; i < n && !miss_key && !miss_tag; i++) {
            miss_key = path[i].miss_key;
            miss_tag = path[i].miss_tag;
        }
        if (miss_key) {
            g_ban_key[miss_key] = 1;
            if (!goals_key(miss_key)) return 0;
        } else if (miss_tag > 0) {
            g_ban_tag[miss_tag] = 1;
            if (!goals_trigger(miss_tag, now)) return 0;
        } else {
            return 0;
        }
    }
    return 0;
}

/* Appends the scenery fields to the JSON in out. Returns the new length. */
static int write_scenery(const player_t *p, const mobj_t *me, int n)
{
    near_line_t near_lines[MAX_INTERACT];
    int nn = 0, i, j, any_key = 0;
    const line_t *hit = NULL;
    plan_t pl;
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

    if (plan_route(p, me, &pl)) {
        static const char *VIA[] = {"portal", "door", "exit", "switch", "walk"};
        n += snprintf(out + n, OUT_SIZE - n,
                      ",\"route\":{\"bearing\":%.2f,\"distance\":%.1f,\"via\":\"%s\",\"remaining\":%.0f}",
                      bearing_to(me, pl.wx, pl.wy), dist_to(me, pl.wx, pl.wy), VIA[pl.via], pl.remaining);
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
