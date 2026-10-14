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
#include "r_state.h"

#define MAX_LISTED 16
#define OUT_SIZE 8192

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

/* Bearing to the nearest exit line (normal exit 11, walk-over exit 52, secret 51/124). */
static int exit_bearing(const mobj_t *me, double *bearing)
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
            found = 1;
        }
    }
    return found;
}

/* mode bits: 1 = scan thinkers, 2 = find the exit, 4 = line-of-sight checks. 7 is the real thing;
 * the smaller modes exist so a hang can be traced to one part of the scan. */
EMSCRIPTEN_KEEPALIVE
const char *pdoom_get_state_ex(int mode)
{
    player_t *p = &players[consoleplayer];
    mobj_t *me = p->mo;
    listed_t enemies[MAX_LISTED], pickups[MAX_LISTED];
    int ne = 0, np = 0, i, ammo = 0, n;
    double eb = 0.0;
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

    if ((mode & 2) && exit_bearing(me, &eb))
        snprintf(out + n, OUT_SIZE - n, "],\"exit_bearing\":%.2f}", eb);
    else
        snprintf(out + n, OUT_SIZE - n, "],\"exit_bearing\":null}");
    return out;
}

EMSCRIPTEN_KEEPALIVE
const char *pdoom_get_state(void)
{
    return pdoom_get_state_ex(7);
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
