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
    out = play(bot, *[state(tic=1 + i * 35) for i in range(8)])
    assert out == [
        Intent.advance,       # first look
        Intent.use,           # advanced, did not move: a door?
        Intent.strafe_left,
        Intent.strafe_right,
        Intent.use,
        Intent.turn_left,
        Intent.turn_right,
        Intent.use,
    ]


def test_moving_clears_the_stuck_counter():
    bot = RuleBot()
    out = play(bot, state(tic=1), state(tic=36), state(tic=71, x=60), state(tic=106, x=120))
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
    out = play(bot, state(health=100, tic=1), state(health=80, tic=36, x=50),
               state(health=60, tic=71, x=50), state(health=40, tic=106, x=50))
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
