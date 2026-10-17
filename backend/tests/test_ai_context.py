"""The memory ai_decide's player keeps between calls, and the context it makes from it."""

import json

from pdoom.databricks_sql import SqlRunner
from pdoom.models import Decision, Enemy, GameState, Intent, PlayerState, Route
from pdoom.players.ai_context import Memory, bearing_of
from pdoom.players.ai_decide import AiDecideApi, AiDecidePlayer, load_questions

T = 35  # tics a second


def at(tic, x=0.0, y=0.0, angle=0.0, health=100, ammo=50, enemies=(), to_go=None, level="E1M1"):
    route = Route(bearing=0, distance=to_go, via="portal", remaining=to_go) if to_go else None
    return GameState(
        tic=tic,
        level=level,
        seed=1,
        player=PlayerState(
            health=health, x=x, y=y, angle=angle, ammo={"bullets": ammo} if ammo else {}
        ),
        enemies=list(enemies),
        route=route,
    )


def imp(distance, bearing=0.0, los=True):
    return Enemy(type="imp", distance=distance, bearing=bearing, line_of_sight=los)


def walk(mem, seconds, step=14, x0=0.0, dx=0.0, **kw):
    """Feed one state every `step` tics for `seconds`, moving `dx` units each time."""
    n = seconds * T // step
    state = None
    for i in range(n + 1):
        state = at(i * step, x=x0 + i * dx, **kw)
        mem.observe(state)
    return state


# ---- frame ------------------------------------------------------------------------------


def test_bearing_is_positive_to_the_right_of_where_it_faces():
    assert bearing_of(0, 0, -50) == 90  # facing east, a point to the south is on the right
    assert bearing_of(0, 0, 50) == -90
    assert abs(bearing_of(0, -50, 0)) == 180


def test_the_trail_says_where_it_was_from_where_it_is_now():
    mem = Memory()
    # walked east, 10 units a tic: 2 s ago it was 700 units behind
    now = walk(mem, 12, step=7, dx=70.0)
    ctx = mem.context(now)
    ago2 = next(t for t in ctx["trail"] if t["seconds_ago"] == 2)
    assert 600 < ago2["distance"] < 800
    assert abs(ago2["bearing"]) == 180  # straight behind
    assert ctx["moved_5s"] > 1500


def test_progress_is_units_closer_to_the_exit():
    mem = Memory()
    for i in range(0, 20 * T, 14):
        mem.observe(at(i, x=i, to_go=2000 - i))  # 1 unit closer every tic
    ctx = mem.context(at(20 * T, x=20 * T, to_go=2000 - 20 * T))
    assert 450 < ctx["progress_15s"] < 560
    assert "closer to the exit" in ctx["summary"]


def test_going_nowhere_is_called_out():
    mem = Memory()
    for i in range(0, 25 * T, 14):
        mem.observe(at(i, x=500, to_go=900))
    ctx = mem.context(at(25 * T, x=500, to_go=900))
    assert ctx["progress_15s"] <= 10
    assert "No progress" in ctx["summary"]


def test_coming_back_to_an_area_counts_as_another_visit():
    mem = Memory()
    for t, x in ((0, 0), (14, 10), (200, 1000), (400, 1010), (600, 5), (614, 8)):
        mem.observe(at(t, x=x))
    ctx = mem.context(at(614, x=8))
    assert ctx["area_visits"] == 2
    assert ctx["explored_areas"] >= 2
    assert "2 times" in ctx["summary"]


def test_a_brief_wobble_over_a_border_is_not_a_new_visit():
    mem = Memory()
    for i, x in enumerate((120, 130, 120, 130, 120, 130)):  # the cell edge is at 128
        mem.observe(at(i * 14, x=x))
    assert mem.context(at(84, x=130))["area_visits"] == 1


# ---- what it did ------------------------------------------------------------------------


def test_a_walk_that_went_nowhere_is_marked_blocked():
    mem = Memory()
    mem.observe(at(0, x=100))
    mem.record(at(0, x=100), Intent.advance, 14, 0.0)
    mem.observe(at(20, x=100))
    ctx = mem.context(at(20, x=100))
    assert ctx["recent"] == [{"seconds_ago": 0.6, "intent": "advance", "moved": 0, "blocked": True}]
    assert "advance was just blocked" in ctx["summary"]


def test_a_walk_that_got_somewhere_is_not():
    mem = Memory()
    mem.observe(at(0, x=100))
    mem.record(at(0, x=100), Intent.advance, 14, 0.0)
    mem.observe(at(20, x=260))
    assert mem.context(at(20, x=260))["recent"][0]["blocked"] is False


def test_a_late_answer_is_filed_when_it_took_effect_not_when_it_was_asked():
    mem = Memory()
    mem.observe(at(0))
    mem.record(at(0), Intent.advance, 10, latency_s=1.0)  # lands at tic 35
    assert mem.moving_tics(0, 30) == 0
    assert mem.moving_tics(40, 60) == 5  # in force 35..45, so half of the 40..60 window
    assert mem.moving_tics(0, 100) == 10


def test_a_state_older_than_the_last_adds_nothing():
    mem = Memory()
    assert mem.observe(at(100)) is not None
    assert mem.observe(at(80)) is None
    assert mem.observe(at(100)) is None
    assert len(mem.samples) == 1


# ---- what is around it ------------------------------------------------------------------


def test_damage_and_a_closing_enemy_are_reported():
    mem = Memory()
    mem.observe(at(0, health=100, enemies=[imp(500)]))
    mem.observe(at(35, health=100, enemies=[imp(400)]))
    now = at(70, health=80, ammo=47, enemies=[imp(300)])
    mem.observe(now)
    ctx = mem.context(now)
    assert ctx["threat"]["closing_per_s"] == 100
    assert ctx["threat"]["distance"] == 300 and ctx["enemies_in_sight"] == 1
    assert ctx["damage_3s"] == 20 and ctx["last_hit_s"] == 0
    assert "imp in sight 300 away, closing at 100 a second" in ctx["summary"]
    assert "Lost 20 health" in ctx["summary"]


def test_an_enemy_out_of_sight_is_not_a_threat():
    mem = Memory()
    now = at(0, enemies=[imp(200, los=False)])
    mem.observe(now)
    ctx = mem.context(now)
    assert ctx["enemies_in_sight"] == 0 and "threat" not in ctx


# ---- the player -------------------------------------------------------------------------


class Clocked(AiDecidePlayer):
    """Answers with a fixed intent and spends `latency_s` of fake clock doing it."""

    def __init__(self, intent, latency_s=0.0, **kw):
        self.now = 0.0
        super().__init__(sql=SqlRunner("wh", "main", "pdoom", client=object()),
                         clock=lambda: self.now, **kw)
        self._intent, self._latency = intent, latency_s
        self.seen = []

    def _decide(self, state):
        self.seen.append(state)
        self.now += self._latency
        return Decision(intent=self._intent, raw_output={})


def test_the_model_is_given_the_context_and_the_row_keeps_it():
    bot = Clocked(Intent.advance)
    d = bot.decide(at(0, x=5), session_id="s1")
    assert bot.seen[0].context is not None
    assert d.raw_output["context"]["seconds_played"] == 0
    assert d.raw_output["context"]["summary"] == "Nothing unusual."


def test_context_goes_into_the_rest_body_and_the_sql_parameter_but_no_other_dump():
    class Client:
        def __init__(self):
            self.api_client = self
            self.sent = None

        def do(self, method, path, body=None):
            self.sent = body
            return {"response": None, "error_message": "x"}

    client = Client()
    state = at(0)
    AiDecidePlayer(api=AiDecideApi(load_questions(), client)).decide(state)
    assert "context" in client.sent["state"]
    assert "context" not in state.model_dump()  # the browser's copy is untouched
    assert "context" not in json.loads(state.model_dump_json())

    sql = SqlRunner("wh", "main", "pdoom", client=object())
    seen = {}
    sql.run = lambda statement, params=None, max_wait_s=None: seen.update(params) or None
    AiDecidePlayer(sql).decide(at(0))
    assert "context" in json.loads(seen["state"])


def test_sessions_do_not_share_a_history():
    bot = Clocked(Intent.advance)
    bot.decide(at(0, x=0), session_id="a")
    bot.decide(at(14, x=300), session_id="a")
    d = bot.decide(at(14, x=0), session_id="b")  # same tic, other session: its first look
    assert d.raw_output["context"]["seconds_played"] == 0
    assert "trail" not in d.raw_output["context"]


def test_an_intent_with_no_length_is_given_one():
    bot = Clocked(Intent.strafe_left)
    assert bot.decide(at(0)).hold_tics == 10
    assert Clocked(Intent.fire).decide(at(0)).hold_tics == 12
    assert Clocked(Intent.turn_left).decide(at(0)).hold_tics is None  # turns are sized already


def _barrel(bot, tics, x_at):
    out = []
    for t in tics:
        out.append(bot.decide(at(t, x=x_at(t), y=100), session_id="s"))
    return out


def test_overlapping_calls_do_not_make_a_walking_player_look_stuck():
    # Calls every 14 tics, each taking 40 tics to come back, the player covering 8 units a tic.
    bot = Clocked(Intent.advance, latency_s=40 / T)
    out = _barrel(bot, range(0, 400, 14), lambda t: 8.0 * t)
    assert all("unstick" not in (d.raw_output.get("assist") or {}) for d in out)
    assert all(d.raw_output["context"]["stuck"] == 0 for d in out)


def test_overlapping_calls_still_catch_a_player_walking_into_a_barrel():
    bot = Clocked(Intent.advance, latency_s=40 / T)
    out = _barrel(bot, range(0, 400, 14), lambda t: 100.0)
    taken = [d for d in out if "unstick" in (d.raw_output.get("assist") or {})]
    assert taken, "never took over from the model"
    first = out.index(taken[0])
    # nothing could be judged until the first answer had landed (tic 40) and had time to show
    assert out[first].raw_output["context"]["stuck"] >= 2
    assert first >= 4
    assert out[first].intent in (Intent.strafe_left, Intent.strafe_right)


def test_the_model_is_told_when_it_is_blocked():
    bot = Clocked(Intent.advance, latency_s=0.0)
    out = _barrel(bot, (0, 12, 24, 36), lambda t: 100.0)
    ctx = out[-1].raw_output["context"]
    assert ctx["stuck"] >= 1
    assert any(r.get("blocked") for r in ctx["recent"])
