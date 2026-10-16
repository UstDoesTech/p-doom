import json

from pdoom.databricks_sql import SqlRunner
from pdoom.models import Decision, Enemy, GameState, Intent, PlayerState
from pdoom.players.ai_decide import AiDecidePlayer, load_questions, parse_decision, sql_literal

# Shape taken from the ai_decide documentation.
GOOD = {
    "response": {
        "answers": {
            "intent": {
                "type": "choice",
                "choice": "fire",
                "probabilities": {"fire": 0.8, "advance": 0.2},
                "confidence": 0.9,
            },
            "threat": {"type": "noul", "probability": 0.7},
            "danger": {
                "type": "score",
                "score": 2.4,
                "legend": {"0": "Safe"},
                "probabilities": {"2": 1.0},
                "confidence": 0.8,
            },
        }
    },
    "metadata": {"version": "1.0"},
    "error_message": None,
}


class FakeSql(SqlRunner):
    def __init__(self, reply):
        super().__init__("wh", "main", "pdoom", client=object())
        self.reply = reply
        self.calls: list[tuple[str, dict]] = []

    def run(self, statement, params=None, max_wait_s=None):
        self.calls.append((statement, params or {}))
        self.max_wait_s_seen = max_wait_s
        return self.reply


def game_state() -> GameState:
    return GameState(
        tic=99,
        level="E1M1",
        seed=7,
        player=PlayerState(health=64, x=1, y=2, angle=90),
        enemies=[Enemy(type="zombieman", distance=420, bearing=-15, line_of_sight=True)],
    )


def test_parses_all_three_answers():
    d = parse_decision(json.dumps(GOOD))
    assert d.intent == Intent.fire
    assert d.confidence == 0.9
    assert d.threat_prob == 0.7
    assert d.danger_score == 2.4
    assert d.error is None


def test_failure_response_becomes_error_with_no_intent():
    d = parse_decision(json.dumps({"response": None, "error_message": "model unavailable"}))
    assert d.intent is None
    assert "model unavailable" in d.error


def test_unknown_intent_is_an_error():
    bad = json.loads(json.dumps(GOOD))
    bad["response"]["answers"]["intent"]["choice"] = "dance"
    assert parse_decision(json.dumps(bad)).intent is None


def test_garbage_does_not_raise():
    assert parse_decision("not json").error


def test_statement_embeds_constant_questions_and_passes_state_as_parameter():
    sql = FakeSql([[json.dumps(GOOD)]])
    player = AiDecidePlayer(sql)
    decision = player.decide(game_state())

    statement, params = sql.calls[0]
    assert statement.startswith("SELECT to_json(ai_decide(:state, '")
    assert "map('version', '1.0')" in statement
    assert '"type":"choice"' in statement
    state_sent = json.loads(params["state"])
    assert "tic" not in state_sent and "seed" not in state_sent
    assert state_sent["enemies"][0]["bearing"] == -15
    assert decision.intent == Intent.fire


def test_empty_result_is_an_error():
    assert AiDecidePlayer(FakeSql([])).decide(game_state()).error


def test_questions_cover_every_intent():
    criteria = load_questions()["intent"]["criteria"]
    assert set(criteria) == {i.value for i in Intent}


def test_sql_literal_escapes_quotes_and_backslashes():
    assert sql_literal("it's \\ here") == "it\\'s \\\\ here"


# ---- REST primary, SQL backup -----------------------------------------------------------


class FakeApi:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.states = []

    def run(self, state):
        self.states.append(state)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_rest_is_tried_first_and_sql_is_left_alone():
    api, sql = FakeApi(GOOD), FakeSql([[json.dumps(GOOD)]])
    d = AiDecidePlayer(sql, api=api).decide(game_state())
    assert d.intent == Intent.fire and d.raw_output["transport"] == "rest"
    assert sql.calls == [] and len(api.states) == 1


def test_sql_backs_up_a_failed_rest_call():
    sql = FakeSql([[json.dumps(GOOD)]])
    d = AiDecidePlayer(sql, api=FakeApi(RuntimeError("403 scope"))).decide(game_state())
    assert d.intent == Intent.fire
    assert d.raw_output["transport"] == "sql" and "403 scope" in d.raw_output["rest_error"]


def test_sql_backs_up_a_rest_reply_with_no_answer():
    bad = {"response": None, "error_message": "model unavailable"}
    d = AiDecidePlayer(FakeSql([[json.dumps(GOOD)]]), api=FakeApi(bad)).decide(game_state())
    assert d.raw_output["transport"] == "sql" and "model unavailable" in d.raw_output["rest_error"]


def test_rest_only_returns_the_error_when_there_is_no_backup():
    d = AiDecidePlayer(api=FakeApi(RuntimeError("down"))).decide(game_state())
    assert d.intent is None and "down" in d.error


def test_rest_pauses_after_repeated_failures_then_recovers():
    now = [0.0]
    api = FakeApi(RuntimeError("down"))
    sql = FakeSql([[json.dumps(GOOD)]])
    player = AiDecidePlayer(sql, api=api, clock=lambda: now[0])
    for _ in range(3):
        player.decide(game_state())
    assert len(api.states) == 3
    player.decide(game_state())  # paused: REST is not tried
    assert len(api.states) == 3 and len(sql.calls) == 4
    now[0] = 61.0
    api.replies = [GOOD]
    assert player.decide(game_state()).raw_output["transport"] == "rest"


def test_rest_request_matches_the_documented_shape():
    class Client:
        def __init__(self):
            self.api_client = self
            self.sent = None

        def do(self, method, path, body=None):
            self.sent = (method, path, body)
            return GOOD

    client = Client()
    from pdoom.players.ai_decide import AiDecideApi

    AiDecidePlayer(api=AiDecideApi(load_questions(), client)).decide(game_state())
    method, path, body = client.sent
    assert (method, path) == ("POST", "/api/2.0/ai-functions/ai-decide")
    assert set(body) == {"state", "questions", "options"} and body["options"] == {"version": "1.0"}
    assert isinstance(body["state"], dict) and "seed" not in body["state"]
    assert set(body["questions"]) == {"intent", "threat", "danger"}


def test_needs_at_least_one_transport():
    import pytest

    with pytest.raises(ValueError):
        AiDecidePlayer()


def test_the_sql_backup_is_given_a_time_limit():
    sql = FakeSql([[json.dumps(GOOD)]])
    AiDecidePlayer(sql).decide(game_state())
    assert sql.max_wait_s_seen == 15.0


# ---- the route assist -------------------------------------------------------------------


def routed(intent, bearing, distance=300.0, via="portal", enemies=()):
    from pdoom.models import Route
    from pdoom.players.ai_decide import shape

    state = GameState(
        tic=5,
        level="E1M1",
        seed=1,
        player=PlayerState(health=100, x=0, y=0, angle=0),
        enemies=list(enemies),
        route=Route(bearing=bearing, distance=distance, via=via, remaining=10),
    )
    return shape(state, Decision(intent=intent))


def test_a_turn_is_held_only_as_long_as_the_route_bearing_needs():
    d = routed(Intent.turn_right, 40.0)
    assert d.intent == Intent.turn_right
    assert d.hold_tics is not None and d.hold_tics < 20


def test_a_turn_the_wrong_way_round_the_route_is_flipped_and_recorded():
    d = routed(Intent.turn_left, 90.0)
    assert d.intent == Intent.turn_right
    assert "assist" in d.raw_output and "note" in d.raw_output["assist"]


def test_a_small_bearing_is_left_to_the_model():
    d = routed(Intent.turn_left, 5.0)
    assert d.intent == Intent.turn_left and d.hold_tics is None


def test_advance_is_held_for_the_distance_to_the_next_point():
    near = routed(Intent.advance, 0.0, distance=60.0)
    far = routed(Intent.advance, 0.0, distance=900.0)
    assert near.hold_tics < far.hold_tics <= 14


def test_advance_to_a_door_stops_short_of_it():
    door = routed(Intent.advance, 0.0, distance=100.0, via="door")
    open_ = routed(Intent.advance, 0.0, distance=100.0, via="portal")
    assert door.hold_tics < open_.hold_tics


def test_an_enemy_in_sight_is_not_overridden_by_the_route():
    foe = Enemy(type="zombieman", distance=300, bearing=-60, line_of_sight=True)
    d = routed(Intent.turn_left, 90.0, enemies=[foe])
    assert d.intent == Intent.turn_left  # the enemy is on the left
    assert d.hold_tics is not None
    assert routed(Intent.advance, 0.0, enemies=[foe]).hold_tics is None


def test_no_route_means_no_change():
    from pdoom.players.ai_decide import shape

    d = Decision(intent=Intent.turn_left)
    assert shape(game_state().model_copy(update={"enemies": []}), d) == d


def test_the_assist_can_be_switched_off():
    class Api:
        def run(self, state):
            return GOOD

    on = AiDecidePlayer(api=Api())
    off = AiDecidePlayer(api=Api(), assist=False)
    assert off.assist is False and on.assist is True


def _at(tic, x, y):
    player = PlayerState(health=100, x=x, y=y, angle=0)
    return GameState(tic=tic, level="E1M1", seed=3, player=player)


def _model(intent):
    return Decision(intent=intent, raw_output={})


def test_unsticks_when_walking_into_a_barrel_and_clears_once_moving():
    from pdoom.players.ai_decide import AiDecidePlayer

    class Fixed(AiDecidePlayer):
        def _decide(self, state):
            return _model(Intent.advance)

    bot = Fixed(sql=FakeSql(None))
    # advancing but the position never changes
    d0 = bot.decide(_at(10, 100, 100))
    assert d0.intent == Intent.advance
    d1 = bot.decide(_at(20, 100, 100))
    assert d1.intent == Intent.advance  # one miss is not enough
    d2 = bot.decide(_at(30, 100, 100))
    assert d2.intent in (Intent.strafe_left, Intent.strafe_right)
    assert d2.raw_output["assist"]["unstick"]["step"] == 0
    d3 = bot.decide(_at(40, 100, 100))  # the slide did not move it either: next step
    assert d3.intent == Intent.advance
    assert d3.raw_output["assist"]["unstick"]["step"] == 1
    # it moved: back to the model
    d4 = bot.decide(_at(50, 100, 160))
    assert "unstick" not in (d4.raw_output.get("assist") or {})
    assert d4.intent == Intent.advance


def test_does_not_override_while_an_enemy_is_in_sight():
    from pdoom.players.ai_decide import AiDecidePlayer

    class Fixed(AiDecidePlayer):
        def _decide(self, state):
            return _model(Intent.advance)

    bot = Fixed(sql=FakeSql(None))
    for t in (10, 20, 30, 40):
        s = _at(t, 5, 5)
        s.enemies = [Enemy(type="imp", distance=300, bearing=0, line_of_sight=True)]
        d = bot.decide(s)
        assert d.intent == Intent.advance
