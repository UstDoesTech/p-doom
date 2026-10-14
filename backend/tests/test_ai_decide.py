import json

from pdoom.databricks_sql import SqlRunner
from pdoom.models import Enemy, GameState, Intent, PlayerState
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

    def run(self, statement, params=None):
        self.calls.append((statement, params or {}))
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
