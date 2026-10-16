"""Plays rule_bot through the real E1M1 geometry in an offline simulator. Needs the shareware WAD:
set PDOOM_WAD to its path (the test is skipped otherwise)."""

import os

import pytest

from pdoom.players.rule_bot import RuleBot

WAD = os.environ.get("PDOOM_WAD")
pytestmark = pytest.mark.skipif(not WAD or not os.path.exists(WAD), reason="PDOOM_WAD not set")


def test_rule_bot_clears_e1m1_from_the_start():
    import navsim

    level = navsim.Level(WAD, "E1M1")
    result = navsim.play(level, RuleBot(), max_s=120)
    assert result["outcome"] == "cleared", result
    assert result["tic"] < 120 * 35
    assert result["rules"].get("open_door", 0) > 0  # it got through a door by pressing use


def _plan_chain(level):
    """Follow the planner from the start, pretending each stage it asks for is done: a key is
    picked up, a trigger is pressed and the sectors it moves open. Returns the stages."""
    import navsim

    sx, sy, _ = level.start
    keys, opened, visited, stages = set(), set(), set(), []
    for _ in range(16):
        plan = level.plan(keys, sx, sy, frozenset(opened), frozenset(visited))
        if plan is None:
            return stages + [None]
        stages.append(plan["goal"])
        if plan["goal"] in ("exit", "kill"):
            return stages
        kind, what = level.blockers[-1]
        if kind == "key":
            keys.add(what)
        else:
            opened.add(what)
            visited.update(
                ln.index for ln in level.lines if ln.tag == what and navsim.is_trigger(ln.special)
            )
    return stages + [None]


# Maps the planner cannot yet solve from the full Ultimate Doom WAD: a key or exit its model of
# the map says is cut off (a pit island, a door that only opens from inside). Everything else,
# including every shareware level, plans through to the exit.
KNOWN_UNSOLVED = {"E2M6", "E3M6", "E4M9"}
LEVELS = [f"E{e}M{m}" for e in range(1, 5) for m in range(1, 10)]


@pytest.mark.parametrize("name", LEVELS)
def test_every_level_has_a_route_from_the_start(name):
    import navsim

    try:
        level = navsim.Level(WAD, name)
    except ValueError:
        pytest.skip(f"{name} is not in this WAD")
    if name in KNOWN_UNSOLVED:
        pytest.xfail("known gap in the planner")
    stages = _plan_chain(level)
    assert None not in stages, (name, stages)
    assert stages[-1] in ("exit", "kill")
