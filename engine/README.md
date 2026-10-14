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

## Limits

- Seeds only choose a starting point in Doom's 256-entry random table (the low byte sets the combat
  table, the next byte the cosmetic one). Two seeds 256 apart behave alike.
- The WAD is the shareware `doom1.wad` (episode 1). It is fetched by SHA-1, never committed.
- Licence: doom-wasm is a Chocolate Doom fork under GPL-2.0. The built engine is a GPL work, so
  publish the corresponding source for whatever you deploy.
