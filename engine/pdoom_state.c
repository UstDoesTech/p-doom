/*
 * UNTESTED SKETCH. Never compiled against doom-wasm.
 *
 * Exports what the web app's WasmEngine expects (see docs/state.md). The player, mobj and
 * thinker names below come from Doom's public source (p_local.h, d_player.h) and may differ
 * in the doom-wasm fork. Adjust after the first real build.
 */
#include <emscripten.h>
#include <stdio.h>
#include "doomstat.h"
#include "d_player.h"
#include "p_local.h"

static char buf[4096];

EMSCRIPTEN_KEEPALIVE
const char *pdoom_state(void) {
    player_t *p = &players[consoleplayer];
    mobj_t *m = p->mo;
    int ammo = 0;
    for (int i = 0; i < NUMAMMO; i++) ammo += p->ammo[i];
    /* Doom angles are counter-clockwise from east, in 2^32 units. Convert to degrees. */
    double angle = (double)m->angle * 360.0 / 4294967296.0;
    snprintf(buf, sizeof buf,
        "{\"tic\":%d,\"health\":%d,\"armor\":%d,\"ammo\":%d,\"x\":%d,\"y\":%d,\"angle\":%.2f,"
        "\"kills\":%d,\"dead\":%d}",
        gametic, p->health, p->armorpoints, ammo,
        m->x >> 16, m->y >> 16, angle, p->killcount, p->playerstate == PST_DEAD);
    return buf;
}

/* TODO: pdoom_new_game(skill, episode, map, seed): seed the RNG and start the map. */
/* TODO: enemy and pickup lists, exit bearing, level-end detection. */
