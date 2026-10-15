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

static double sector_cx(const sector_t *s) { return (double)s->soundorg.x / 65536.0; }
static double sector_cy(const sector_t *s) { return (double)s->soundorg.y / 65536.0; }

static int can_cross(const player_t *p, const sector_t *from, const sector_t *to, const line_t *l)
{
    if (is_door(l->special)) return has_key(p, door_key(l->special));
    if (l->flags & ML_BLOCKING) return 0;
    if (min_fixed(from->ceilingheight, to->ceilingheight) -
            max_fixed(from->floorheight, to->floorheight) < PLAYER_HEIGHT)
        return 0;
    return to->floorheight - from->floorheight <= STEP_HEIGHT;
}

static double sdist[MAX_SECTORS];
static int sprev[MAX_SECTORS];
static const line_t *svia[MAX_SECTORS];
static char sdone[MAX_SECTORS];
static char sgoal[MAX_SECTORS];

/* Cheapest way from the player's sector to a sector that touches an exit line, going through
 * doors the player can open. Fills the first step's line and the number of sector hops. */
static int find_route(const player_t *p, const mobj_t *me, const line_t **first, int *hops,
                      int *at_exit)
{
    int i, start, goals = 0;
    const sector_t *here = R_PointInSubsector(me->x, me->y)->sector;
    if (numsectors > MAX_SECTORS || numsectors <= 0) return 0;
    start = (int)(here - sectors);
    memset(sgoal, 0, (size_t)numsectors);
    for (i = 0; i < numlines; i++) {
        const line_t *l = &lines[i];
        if ((l->special == 11 || l->special == 51 || l->special == 52 || l->special == 124) &&
            l->frontsector != NULL) {
            sgoal[l->frontsector - sectors] = 1;
            goals++;
        }
    }
    if (goals == 0) return 0;
    if (sgoal[start]) {
        *at_exit = 1;
        *first = NULL;
        *hops = 0;
        return 1;
    }
    for (i = 0; i < numsectors; i++) {
        sdist[i] = 1e30;
        sprev[i] = -1;
        svia[i] = NULL;
        sdone[i] = 0;
    }
    sdist[start] = 0.0;
    for (;;) {
        int u = -1, k;
        const sector_t *s;
        for (i = 0; i < numsectors; i++)
            if (!sdone[i] && sdist[i] < 1e29 && (u < 0 || sdist[i] < sdist[u])) u = i;
        if (u < 0) return 0;
        if (sgoal[u]) {
            int step = u, n = 0;
            while (sprev[step] >= 0) {
                *first = svia[step];
                step = sprev[step];
                n++;
            }
            *hops = n;
            *at_exit = 0;
            return 1;
        }
        sdone[u] = 1;
        s = &sectors[u];
        for (k = 0; k < s->linecount; k++) {
            const line_t *l = s->lines[k];
            const sector_t *o;
            int v;
            double cost;
            if (l->backsector == NULL || l->frontsector == l->backsector) continue;
            o = (l->frontsector == s) ? l->backsector : l->frontsector;
            v = (int)(o - sectors);
            if (sdone[v] || !can_cross(p, s, o, l)) continue;
            cost = sqrt((sector_cx(s) - sector_cx(o)) * (sector_cx(s) - sector_cx(o)) +
                        (sector_cy(s) - sector_cy(o)) * (sector_cy(s) - sector_cy(o))) + 16.0;
            if (is_door(l->special)) cost += 64.0;
            if (sdist[u] + cost < sdist[v]) {
                sdist[v] = sdist[u] + cost;
                sprev[v] = u;
                svia[v] = l;
            }
        }
    }
}

/* Appends the scenery fields to the JSON in out. Returns the new length. */
static int write_scenery(const player_t *p, const mobj_t *me, int n)
{
    near_line_t near_lines[MAX_INTERACT];
    int nn = 0, i, j, any_key = 0;
    const line_t *hit = NULL, *first = NULL;
    int hops = 0, at_exit = 0;
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

    if (find_route(p, me, &first, &hops, &at_exit)) {
        double b, d;
        const char *via = "portal";
        if (at_exit || first == NULL) {
            double eb = 0.0, ed = 0.0;
            if (exit_bearing(me, &eb, &ed)) {
                b = eb;
                d = ed;
            } else {
                b = 0.0;
                d = 0.0;
            }
            via = "exit";
        } else {
            double mx = ((double)first->v1->x + (double)first->v2->x) / 2.0 / 65536.0;
            double my = ((double)first->v1->y + (double)first->v2->y) / 2.0 / 65536.0;
            b = bearing_to(me, mx, my);
            d = dist_to(me, mx, my);
            if (is_door(first->special)) via = "door";
        }
        n += snprintf(out + n, OUT_SIZE - n,
                      ",\"route\":{\"bearing\":%.2f,\"distance\":%.1f,\"via\":\"%s\",\"hops\":%d}",
                      b, d, via, hops);
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
