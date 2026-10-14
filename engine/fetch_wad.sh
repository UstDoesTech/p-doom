#!/usr/bin/env bash
# Fetch the shareware doom1.wad (v1.9, freely distributable) and check its SHA-1.
#   engine/fetch_wad.sh <output-dir>
set -euo pipefail
OUT=$(mkdir -p "$1" && cd "$1" && pwd)
SHA1=5b2e249b9c5133ec987b3ea77596381dc0d6bc1d
for url in \
  https://distro.ibiblio.org/slitaz/sources/packages/d/doom1.wad \
  https://archive.org/download/DoomsharewareEpisode/doom1.wad; do
  if curl -fsSL --retry 2 -o "$OUT/doom1.wad" "$url" && echo "$SHA1  $OUT/doom1.wad" | sha1sum -c -; then
    exit 0
  fi
done
echo "Could not fetch a doom1.wad with the expected SHA-1" >&2
exit 1
