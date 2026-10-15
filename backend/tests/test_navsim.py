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
