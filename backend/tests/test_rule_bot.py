from pdoom.models import Enemy, GameState, Intent, Pickup, PlayerState
from pdoom.players.rule_bot import RuleBot


def state(health=100, enemies=(), exit_bearing=None, tic=1, x=0.0, y=0.0, pickups=(),
          ammo=None, armor=0) -> GameState:
    return GameState(
        tic=tic,
        level="E1M1",
        seed=1,
        player=PlayerState(health=health, armor=armor, x=x, y=y, angle=0,
                           ammo={"total": 50} if ammo is None else {"total": ammo}),
        enemies=list(enemies),
        pickups=list(pickups),
        exit_bearing=exit_bearing,
    )


def pickup(kind, bearing=0.0, distance=200.0) -> Pickup:
    return Pickup(type=kind, bearing=bearing, distance=distance)


def enemy(bearing, distance=300.0, los=True) -> Enemy:
    return Enemy(type="zombieman", distance=distance, bearing=bearing, line_of_sight=los)


def decide(**kw) -> Intent:
    return RuleBot().decide(state(**kw)).intent


def test_fires_when_enemy_in_cone():
    assert decide(enemies=[enemy(3)]) == Intent.fire


def test_turns_toward_enemy_outside_cone():
    assert decide(enemies=[enemy(-40)]) == Intent.turn_left
    assert decide(enemies=[enemy(40)]) == Intent.turn_right


def test_ignores_enemies_without_line_of_sight():
    assert decide(enemies=[enemy(0, los=False)]) == Intent.advance


def test_retreats_when_hurt_and_enemy_close():
    assert decide(health=20, enemies=[enemy(0, distance=200)]) == Intent.retreat


def test_keeps_fighting_when_hurt_but_enemy_far():
    assert decide(health=20, enemies=[enemy(0, distance=2000)]) == Intent.fire


def test_targets_nearest_visible_enemy():
    near, far = enemy(-30, distance=100), enemy(2, distance=900)
    assert decide(enemies=[far, near]) == Intent.turn_left


def test_faces_exit_then_advances():
    assert decide(exit_bearing=60) == Intent.turn_right
    assert decide(exit_bearing=-60) == Intent.turn_left
    assert decide(exit_bearing=5) == Intent.advance
    assert decide() == Intent.advance


# ---- the added behaviours ----------------------------------------------------------------


def play(bot, *states):
    return [bot.decide(s).intent for s in states]


def test_fetches_health_when_hurt_and_no_enemy_near():
    assert decide(health=40, pickups=[pickup("health", bearing=50)]) == Intent.turn_right
    assert decide(health=40, pickups=[pickup("health", bearing=5)]) == Intent.advance


def test_ignores_health_when_healthy_or_when_far():
    assert decide(health=90, pickups=[pickup("health", bearing=50)]) == Intent.advance
    assert decide(health=40, pickups=[pickup("health", 50, distance=2000)]) == Intent.advance


def test_no_pickup_detour_with_an_enemy_close():
    e = enemy(0, distance=200, los=False)  # not visible: detour allowed
    assert decide(health=40, enemies=[e], pickups=[pickup("health", 50)]) == Intent.turn_right
    # visible and close: fight, do not wander off
    assert decide(health=40, enemies=[enemy(0, 200)], pickups=[pickup("health", 50)]) == Intent.fire


def test_ammo_is_only_wanted_when_empty():
    assert decide(pickups=[pickup("ammo", 50)]) == Intent.advance
    assert decide(ammo=0, pickups=[pickup("ammo", 50)]) == Intent.turn_right


def test_out_of_ammo_does_not_pick_a_fight_at_range():
    assert decide(ammo=0, enemies=[enemy(0, 500)], exit_bearing=2) == Intent.advance
    assert decide(ammo=0, enemies=[enemy(0, 60)]) == Intent.fire  # fists at point blank


def test_stuck_walking_forward_tries_use_then_slides_then_turns():
    bot = RuleBot()
    out = play(bot, *[state(tic=1 + i * 15) for i in range(8)])
    assert out == [
        Intent.advance,       # first look
        Intent.use,           # advanced, did not move: a door?
        Intent.strafe_left,
        Intent.strafe_right,
        Intent.retreat,       # back out of a corner
        Intent.turn_left,
        Intent.turn_right,
        Intent.use,
    ]


def test_moving_clears_the_stuck_counter():
    bot = RuleBot()
    out = play(bot, state(tic=1), state(tic=36), state(tic=71, x=300), state(tic=106, x=600))
    assert out == [Intent.advance, Intent.use, Intent.advance, Intent.advance]


def test_dodges_when_hit_by_a_close_visible_enemy():
    bot = RuleBot()
    out = play(
        bot,
        state(health=100, tic=1, enemies=[enemy(0, 300)]),
        state(health=80, tic=36, enemies=[enemy(0, 300)]),
        state(health=80, tic=71, enemies=[enemy(0, 300)]),
    )
    assert out == [Intent.fire, Intent.strafe_left, Intent.fire]


def test_looks_around_when_hit_by_something_unseen():
    bot = RuleBot()
    out = play(bot, state(health=100, tic=1), state(health=80, tic=36, x=300),
               state(health=60, tic=71, x=600), state(health=40, tic=106, x=900))
    assert out[1] in (Intent.turn_left, Intent.turn_right)
    assert out[2] in (Intent.turn_left, Intent.turn_right)
    assert out[2] != out[1]
    assert out[3] == Intent.advance  # searched twice, carry on


def test_restart_forgets_the_previous_attempt():
    bot = RuleBot()
    play(bot, state(tic=1), state(tic=36), state(tic=71))
    assert bot.decide(state(tic=1)).intent == Intent.advance


def test_every_decision_says_which_rule_fired():
    d = RuleBot().decide(state(enemies=[enemy(3)]))
    assert d.raw_output["rule"] == "enemy_in_cone"


# ---- scenery -----------------------------------------------------------------------------

from pdoom.models import Route, UseTarget, Walls  # noqa: E402


def scene(tic=1, x=0.0, y=0.0, route=None, use_target=None, walls=None, **kw) -> GameState:
    s = state(tic=tic, x=x, y=y, **kw)
    s.walls = walls or Walls(
        ahead=512, ahead_right=512, right=512, behind_right=512, behind=512,
        behind_left=512, left=512, ahead_left=512,
    )
    s.route = route
    s.use_target = use_target
    return s


def test_steers_by_the_route_not_the_straight_line_to_the_exit():
    # The exit is dead ahead through a wall; the route goes round to the right.
    s = scene(exit_bearing=0.0, route=Route(bearing=70.0, distance=300, via="portal"))
    d = RuleBot().decide(s)
    assert d.intent == Intent.turn_right and d.raw_output["rule"] == "face_exit"


def test_presses_the_exit_switch_when_in_reach():
    s = scene(route=Route(bearing=0.0, distance=40, via="exit"),
              use_target=UseTarget(kind="exit"))
    d = RuleBot().decide(s)
    assert d.intent == Intent.use and d.raw_output["rule"] == "press_exit"


def test_opens_a_closed_door_on_the_route_once_then_walks_through():
    bot = RuleBot()
    door = UseTarget(kind="door", open=False)
    route = Route(bearing=0.0, distance=40, via="door")
    first = bot.decide(scene(tic=1, route=route, use_target=door))
    assert (first.intent, first.raw_output["rule"]) == (Intent.use, "open_door")
    # The door is still rising. Pressing use again would close it, so the bot walks.
    second = bot.decide(scene(tic=36, x=0.0, route=route, use_target=door))
    assert (second.intent, second.raw_output["rule"]) == (Intent.advance, "through_door")


def test_leaves_a_locked_door_alone():
    s = scene(route=Route(bearing=0.0, distance=40, via="door"),
              use_target=UseTarget(kind="door", open=False, locked=True))
    assert RuleBot().decide(s).raw_output["rule"] != "open_door"


def test_does_not_press_a_plain_switch():
    s = scene(exit_bearing=0.0, use_target=UseTarget(kind="switch"))
    assert RuleBot().decide(s).intent == Intent.advance


def test_turns_away_from_a_wall_towards_the_room():
    walls = Walls(ahead=20, ahead_right=300, right=300, behind_right=512, behind=512,
                  behind_left=512, left=30, ahead_left=25)
    d = RuleBot().decide(scene(walls=walls))
    assert d.intent == Intent.turn_right and d.raw_output["rule"] == "wall_turn"


def test_wall_turn_keeps_its_direction_until_the_way_is_clear():
    bot = RuleBot()
    blocked = Walls(ahead=20, ahead_right=300, right=300, behind_right=512, behind=512,
                    behind_left=512, left=30, ahead_left=25)
    flipped = Walls(ahead=20, ahead_right=40, right=40, behind_right=512, behind=512,
                    behind_left=512, left=300, ahead_left=300)
    first = bot.decide(scene(tic=1, walls=blocked)).intent
    second = bot.decide(scene(tic=2, x=0.0, walls=flipped)).intent  # would flip without memory
    assert first == second == Intent.turn_right


def test_wall_turn_leans_towards_the_goal():
    even = Walls(ahead=20, ahead_right=200, right=200, behind_right=512, behind=512,
                 behind_left=512, left=200, ahead_left=200)
    d = RuleBot().decide(scene(walls=even, exit_bearing=-10.0))  # no route: the wall rays decide
    assert d.intent == Intent.turn_left


def test_gives_up_on_a_pickup_it_cannot_reach():
    bot = RuleBot()
    seen = []
    for tic in range(1, 8):
        seen.append(bot.decide(scene(tic=tic, health=40, pickups=[pickup("health", 0.0, 200.0)],
                                     exit_bearing=0.0)).raw_output["rule"])
    assert seen[0] == "health"
    assert any(r != "health" for r in seen[4:])


def test_fetches_a_key_that_is_close():
    d = RuleBot().decide(scene(pickups=[pickup("key", 0.0, 200.0)], exit_bearing=0.0))
    assert d.raw_output["rule"] == "key"


def test_escapes_a_jam_it_cannot_explain():
    bot = RuleBot()
    seen = [bot.decide(state(tic=1 + i * 35)).raw_output["rule"] for i in range(14)]
    assert "escape_back" in seen and "escape_turn" in seen and "escape_walk" in seen
