# Doom engine

The engine is [doom-wasm](https://github.com/cloudflare/doom-wasm) (Chocolate Doom on Emscripten) plus a
small C shim, `pdoom_state.c`, that exports the game state the app needs.

## Build

The Emscripten SDK can't be downloaded in every environment, so the build runs in CI:
`.github/workflows/engine.yml` builds the engine, runs `test/smoke.mjs` in headless Chromium, and uploads
the files as the `pdoom-engine` artifact. To build locally with `emcc` on your PATH:

```
git clone https://github.com/cloudflare/doom-wasm && git -C doom-wasm checkout <DOOM_WASM_REF from the workflow>
engine/build.sh doom-wasm engine-out
engine/fetch_wad.sh engine-out
```

Copy `engine-out/*` into `web/public/doom/`. The app then offers the "Doom (WASM)" engine.

`build.sh` patches three doom-wasm files. It drops the debug-only SAFE_HEAP flag from `configure.ac`, adds the shim to `src/doom/Makefile.am`, and calls
`pdoom_apply_seed()` after Doom clears its random-number index when a level starts.

## What the shim exports

`pdoom_get_state`, `pdoom_new_game(episode, map, seed)`, `pdoom_pause`, `pdoom_resume`. The JSON is
described in `docs/state.md`.

## Verified in CI

`test/smoke.mjs` runs in full Chromium and checks: E1M1 loads at the right start, state fields, exit
bearing, arrow keys move the player, Ctrl fires, pause freezes the world and resume restarts it,
restarting with the same seed gives the same start, and turning right shrinks a positive bearing.

## Gotchas found while building

- Emscripten 3.1.74 miscompiles doom-wasm: a live game traps in `P_PlayerThink` (the title demo
  still runs). 2.0.34, 3.0.0 and 3.1.10 are fine. The build is pinned to 3.1.10.
- The fork's `default.cfg` binds fire to `9`. `build.sh` strips its `key_*` lines so Doom's
  standard bindings apply.
- `emscripten_pause_main_loop` does not resume reliably, so pause uses Doom's own `paused` flag.
- headless-shell Chromium freezes on the trap instead of reporting it. Test with full Chromium.
- In software-rendered CI the game runs at about 20 tics a second, not 35.
- `PDOOM_NO_SHIM=1 engine/build.sh ...` builds doom-wasm without the shim, for control tests.

## Limits

- Seeds only choose a starting point in Doom's 256-entry random table (the low byte sets the combat
  table, the next byte the cosmetic one). Two seeds 256 apart behave alike.
- The WAD is the shareware `doom1.wad` (episode 1). It is fetched by SHA-1, never committed. The engine and the route planner work on any episode's maps; the CI checks only cover episode 1. The offline planner test (`backend/tests/test_navsim.py`) runs all 36 maps when `PDOOM_WAD` points at the Ultimate Doom `DOOM.WAD`: 33 plan through to the exit, E2M6, E3M6 and E4M9 do not yet.
- Licence: doom-wasm is a Chocolate Doom fork under GPL-2.0. The built engine is a GPL work, so
  publish the corresponding source for whatever you deploy.
