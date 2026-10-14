#!/usr/bin/env bash
# Build the p(doom) Doom engine. Needs an activated Emscripten SDK (emcc on PATH), autotools,
# and a checkout of https://github.com/cloudflare/doom-wasm (GPL-2.0).
#   engine/build.sh <doom-wasm-checkout> <output-dir>
# Writes websockets-doom.js, websockets-doom.wasm and default.cfg into <output-dir>.
# The WAD is fetched separately by engine/fetch_wad.sh.
set -euo pipefail

SRC=$(cd "$1" && pwd)
OUT=$(mkdir -p "$2" && cd "$2" && pwd)
HERE=$(cd "$(dirname "$0")" && pwd)

if [ -z "${PDOOM_NO_SHIM:-}" ]; then
  cp "$HERE/pdoom_state.c" "$SRC/src/doom/pdoom_state.c"

  # 1. Compile the shim with the rest of the game.
  sed -i 's|^g_game\.c |pdoom_state.c  \\\ng_game.c |' "$SRC/src/doom/Makefile.am"
  [ "$(grep -c '^pdoom_state.c' "$SRC/src/doom/Makefile.am")" = 1 ] || { echo "Makefile.am patch failed"; exit 1; }

  # 2. Re-apply the seed after Doom clears its random-number index when a level starts.
  sed -i 's|^    M_ClearRandom();|    M_ClearRandom();\n    pdoom_apply_seed();|' "$SRC/src/doom/g_game.c"
  sed -i '1i extern void pdoom_apply_seed(void);' "$SRC/src/doom/g_game.c"
  [ "$(grep -c 'pdoom_apply_seed();' "$SRC/src/doom/g_game.c")" = 1 ] || { echo "g_game.c patch failed"; exit 1; }

else
  echo "PDOOM_NO_SHIM set: building doom-wasm without the p(doom) shim (control build)"
fi

# 3. SAFE_HEAP is a debug check that aborts on unaligned reads, which Doom does and WebAssembly allows.
sed -i 's/ -s SAFE_HEAP=1//' "$SRC/configure.ac"
[ "$(grep -c 'SAFE_HEAP' "$SRC/configure.ac")" = 0 ] || { echo "configure.ac patch failed"; exit 1; }

# 3b. Newer Emscripten defaults to a 64 KB stack (older ones used 5 MB). Doom keeps large buffers on
# the stack, and a live game overruns 64 KB, which corrupts memory and traps in P_PlayerThink.
sed -i 's/-s ASSERTIONS=0/-s ASSERTIONS=1 -s STACK_SIZE=5MB/' "$SRC/configure.ac"
[ "$(grep -c 'STACK_SIZE=5MB' "$SRC/configure.ac")" -ge 1 ] || { echo "configure.ac stack patch failed"; exit 1; }

# 4. Keep function names in the wasm so a frozen page can be diagnosed from a stack trace.
sed -i 's/-gsource-map/--profiling-funcs/; s| --source-map-base /||' "$SRC/configure.ac"
[ "$(grep -c 'profiling-funcs' "$SRC/configure.ac")" -ge 1 ] || { echo "configure.ac names patch failed"; exit 1; }

# 5. Build.
(cd "$SRC" && ./scripts/clean.sh >/dev/null 2>&1 || true; ./scripts/build.sh)

cp "$SRC/src/websockets-doom.js" "$SRC/src/websockets-doom.wasm" "$OUT/"
cp "$SRC/src/default.cfg" "$OUT/"
ls -la "$OUT"
