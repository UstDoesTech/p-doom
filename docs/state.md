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
- `route`: the next point to head for on the way to the exit, from a shortest-path search over a 32-unit grid laid on the map, going through doors the player can open. `bearing`, `distance` and `via` (`portal`, `door`, `exit`, `switch` or `walk`) for the next point to head for, and `remaining`, the walking cost still to go to the current goal. Where the exit cannot be reached yet (a key to fetch, a lift or bridge a switch moves, stairs a line builds, a teleporter) the route heads for whatever unlocks it: a key (`portal`), a switch (`switch`: stand within reach and press use) or a line to walk over (`walk`). It re-plans as keys are taken and sectors move, and leaves a trigger it has tried for 20 seconds. On a boss map with no exit line (E1M8, E2M8, E3M8, E4M6, E4M8) the goal is the nearest boss (Baron, Cyberdemon or Spider Mastermind). `null` only when the map has no way through.
- `keys`: key colours held. Key pickups show in `pickups` with type `key`.

Bearings are degrees in -180..180. Positive means to the right (clockwise). Doom's own angles are
counter-clockwise from east, so the engine shim must flip the sign when it converts.

Outcomes: `cleared`, `died`, `timeout`, `aborted` (Stop pressed) and `abandoned` (page closed mid-game). The last two write a session row so abandonment can be monitored, but they are not attempts: the views leave them out of `attempts` and p(doom) and count them separately.
