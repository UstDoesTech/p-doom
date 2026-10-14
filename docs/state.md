# Game state contract

The engine returns a `GameState` (see `web/src/types.ts`, mirrored in `backend/src/pdoom/models.py`).

- `tic`: game tics, 35 per second. Durations and timeouts use game time.
- `player`: health, armor, weapon, ammo (by type), x, y, angle.
- `enemies`, `pickups`: things in view, with positions.
- `exit_bearing`: bearing to the level exit.

Bearings are degrees in -180..180. Positive means to the right (clockwise). Doom's own angles are
counter-clockwise from east, so the engine shim must flip the sign when it converts.

Outcomes: `cleared`, `died`, `timeout`. An aborted attempt is discarded and writes no session row.
