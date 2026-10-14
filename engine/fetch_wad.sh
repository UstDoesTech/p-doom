#!/usr/bin/env bash
# Fetch the shareware doom1.wad (v1.9, freely distributable) and check its SHA-1.
#   engine/fetch_wad.sh <output-dir>
# Extra sources can be passed in WAD_URLS (space separated) and are tried first.
set -uo pipefail
OUT=$(mkdir -p "$1" && cd "$1" && pwd)
SHA1=5b2e249b9c5133ec987b3ea77596381dc0d6bc1d
for url in ${WAD_URLS:-} \
  https://raw.githubusercontent.com/Akbar30Bill/DOOM_wads/master/doom1.wad; do
  code=$(curl -sSL --retry 2 -m 120 -o "$OUT/doom1.wad" -w '%{http_code}' "$url" 2>&1 | tail -c 120)
  got=$(sha1sum "$OUT/doom1.wad" 2>/dev/null | cut -d' ' -f1)
  size=$(stat -c %s "$OUT/doom1.wad" 2>/dev/null || echo 0)
  echo "$url -> http=$code size=$size sha1=$got" >&2
  [ "$got" = "$SHA1" ] && exit 0
done
echo "Could not fetch a doom1.wad with the expected SHA-1 ($SHA1)" >&2
exit 1
