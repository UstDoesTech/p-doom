# Doom engine (untested)

Nothing here has been built. The web app runs against the mock arena until it is.

1. Clone [doom-wasm](https://github.com/cloudflare/doom-wasm) and follow its README to build with Emscripten.
2. Add `pdoom_state.c` to the build and export `pdoom_state` and `pdoom_new_game`. The sketch is incomplete:
   see its TODO lines for enemies, pickups, exit bearing and seeding.
3. Copy the output (loader, wasm, data) into `web/public/doom/` and set `VITE_DOOM_BASE` and `VITE_DOOM_LOADER`.
4. Use the shareware WAD (`doom1.wad`). Do not commit commercial WADs.

Licence: doom-wasm is a Chocolate Doom fork under GPL-2.0. Shipping the built engine means shipping
GPL code, so publish the corresponding source for whatever you deploy.
