# Game state contract

The engine returns a `GameState` (see `web/src/types.ts`, mirrored in `backend/src/pdoom/models.py`).

- `tic`: world tics (Doom's `leveltime`), 35 per second when the browser keeps up. It freezes while paused. Durations and timeouts use game time.
- `player`: health, armor, weapon, ammo, x, y, angle. The Doom engine reports ammo as `{"total": n}`; the mock arena reports it by type.
- `enemies`, `pickups`: things in view, with positions.
- `exit_bearing`, `exit_distance`: bearing and distance to the nearest exit line, in a straight line. Walls are not considered.

Scenery (the Doom engine only; the mock arena sends none, and a player must cope with `null`):

- `walls`: distance in map units to the nearest thing that blocks walking, in eight directions (`ahead`, `ahead_right`, `right`, `behind_right`, `behind`, `behind_left`, `left`, `ahead_left`), capped at 512. A closed door counts as a wall. Under about 40 the player is pressed against it.
- `interactables`: up to six doors, switches and exits within 256 units, with `kind`, `distance`, `bearing`, `facing` (false when the player is on the unusable side), `open` and `locked` (a key the player does not hold).
- `use_target`: what pressing use does right now, the first door, switch or exit within 64 units straight ahead, or `null`. Pressing use on a door that is still opening closes it again.
- `route`: the next point to head for on the way to the exit, from a shortest-path search over a 32-unit grid laid on the map, going through doors the player can open. `bearing`, `distance` and `via` (`portal`, `door` or `exit`) for the next point to head for, and `remaining`, the walking distance still to go. `null` when the exit cannot be reached, for example behind a locked door.
- `keys`: key colours held. Key pickups show in `pickups` with type `key`.

Bearings are degrees in -180..180. Positive means to the right (clockwise). Doom's own angles are
counter-clockwise from east, so the engine shim must flip the sign when it converts.

Outcomes: `cleared`, `died`, `timeout`, `aborted` (Stop pressed) and `abandoned` (page closed mid-game). The last two write a session row so abandonment can be monitored, but they are not attempts: the views leave them out of `attempts` and p(doom) and count them separately.
