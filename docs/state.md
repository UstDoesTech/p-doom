# Game state contract

The engine returns a `GameState` (see `web/src/types.ts`, mirrored in `backend/src/pdoom/models.py`).

- `tic`: world tics (Doom's `leveltime`), 35 per second when the browser keeps up. It freezes while paused. Durations and timeouts use game time.
- `player`: health, armor, weapon, ammo, x, y, angle. The Doom engine reports ammo as `{"total": n}`; the mock arena reports it by type.
- `enemies`, `pickups`: things in view, with positions.
- `exit_bearing`: bearing to the level exit.

Bearings are degrees in -180..180. Positive means to the right (clockwise). Doom's own angles are
counter-clockwise from east, so the engine shim must flip the sign when it converts.

Outcomes: `cleared`, `died`, `timeout`, `aborted` (Stop pressed) and `abandoned` (page closed mid-game). The last two write a session row so abandonment can be monitored, but they are not attempts: the views leave them out of `attempts` and p(doom) and count them separately.
