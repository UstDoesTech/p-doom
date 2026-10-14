from pdoom.models import Enemy, GameState, Intent, PlayerState
from pdoom.players.rule_bot import RuleBot


def state(health=100, enemies=(), exit_bearing=None) -> GameState:
    return GameState(
        tic=1,
        level="E1M1",
        seed=1,
        player=PlayerState(health=health, x=0, y=0, angle=0),
        enemies=list(enemies),
        exit_bearing=exit_bearing,
    )


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
