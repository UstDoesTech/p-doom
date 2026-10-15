# Genie instructions for p(doom)

Paste into the Genie space's general instructions. Add these tables to the space:
`doom_metrics` (metric view), `doom_games`, `doom_leaderboard`, `doom_seed_outcomes`, `doom_sessions`, `doom_decisions`.

- A session is one attempt at one level. p(doom) = deaths / attempts for a player type.
- Always filter `engine = 'wasm'`. Rows with `engine = 'mock'` are pipeline tests and must never appear.
- Never mix `mode` values in one comparison. Group or filter by `mode`.
- Always show `attempts` beside any rate. A rate from a few attempts means little.
- Prefer `doom_metrics` for rates by player type, mode, level, seed or day. Use `MEASURE()`.
- `doom_leaderboard` has Wilson 95% intervals. Use it when asked who is best or whether a gap is real.
- The chance that at least one machine player type clears a seed is `cleared_any` in `doom_seed_outcomes`; the any-clear rate is `AVG(cleared_any)`.
  It cannot be a metric view measure. Humans are excluded from it.
- Roster p(doom) = 1 - product of (1 - clear_rate) over machine player types. Compute it from
  `doom_leaderboard`, and show it beside the observed any-clear rate.
- A timeout is neither a death nor a clear.
- Outcomes: cleared, died, timeout, aborted (player pressed Stop), abandoned (tab closed or idle for
  15 minutes), error (the game loop crashed). `doom_sessions` holds all of them. A session still
  open after an hour shows as status 'lost' in `doom_games`.
- Only `doom_games.counts_as_attempt = true` rows are attempts: outcome cleared, died or timeout,
  and, for machine players, no more than half the decisions failed or timed out. Use `doom_games`
  for anything about abandoned, aborted, lost or unreliable games; use `doom_metrics` or
  `doom_leaderboard` for rates. `doom_leaderboard.excluded_games` counts what was left out.
- `doom_decisions.latency_ms` is the model's thinking time. `error` is non-null when a decision failed.

- The tables are live views over the Lakebase database (catalog `pdoom_lakebase`). `doom_decisions.raw_output` is JSON text; use `from_json` or `get_json_object` on it.
