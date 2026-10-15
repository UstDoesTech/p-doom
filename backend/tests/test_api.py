from fastapi.testclient import TestClient

from pdoom.config import Settings
from pdoom.main import create_app
from pdoom.models import PlayerType
from pdoom.players import build_registry
from pdoom.telemetry import MemorySink

STATE = {
    "tic": 10,
    "level": "E1M1",
    "seed": 3,
    "player": {"health": 90, "x": 0, "y": 0, "angle": 90},
    "enemies": [{"type": "zombieman", "distance": 300, "bearing": 2, "line_of_sight": True}],
}


def make(sink=None, **settings):
    sink = sink or MemorySink()
    app = create_app(Settings(_env_file=None, sink="memory", **settings), sink=sink)
    return TestClient(app), sink


def start(client, player_type="rule_bot", **extra):
    body = {
        "player_type": player_type,
        "mode": "paused",
        "engine": "mock",
        "level": "E1M1",
        "seed": 3,
    }
    return client.post("/api/sessions", json={**body, **extra})


def test_health_and_player_list():
    client, _ = make()
    health = client.get("/api/health").json()
    assert health["status"] == "ok" and health["warehouse_configured"] is False
    assert health["ai_decide_transports"] == ["rest"] and health["sink_reachable"] is None
    players = {p["player_type"]: p for p in client.get("/api/players").json()}
    assert players["rule_bot"]["available"] is True
    assert players["ai_decide"]["available"] is True  # REST needs no warehouse
    assert len(players) == 8


def test_full_rule_bot_session_lands_in_the_sink():
    client, sink = make()
    sid = start(client).json()["session_id"]

    decision = client.post(f"/api/sessions/{sid}/decide", json={"state": STATE}).json()
    assert decision["intent"] == "fire"
    assert decision["latency_ms"] >= 0

    events = [
        {"tic": 10, "health": 90, "x": 0, "y": 0, "angle": 90, "action": "fire"},
        {"tic": 15, "health": 85, "x": 0, "y": 1, "angle": 90},
    ]
    assert client.post(f"/api/sessions/{sid}/events", json={"events": events}).status_code == 202
    assert len(sink.events) == 2
    assert len(sink.decisions) == 1  # flushed with the events

    ended = client.post(
        f"/api/sessions/{sid}/end", json={"outcome": "cleared", "duration_s": 42.5, "kills": 4}
    )
    assert ended.status_code == 200
    row = sink.sessions[0]
    got = (row.player_type, row.outcome, row.engine, row.kills)
    assert got == ("rule_bot", "cleared", "mock", 4)

    assert client.post(f"/api/sessions/{sid}/events", json={"events": events}).status_code == 404


def test_unavailable_players_say_why():
    client, _ = make(ai_decide_api=False)
    assert start(client, "ai_decide").status_code == 503
    assert start(client, "tool_agent").status_code == 503
    assert start(client, "human").status_code == 201


def test_humans_do_not_get_decisions():
    client, _ = make()
    sid = start(client, "human").json()["session_id"]
    assert client.post(f"/api/sessions/{sid}/decide", json={"state": STATE}).status_code == 400


def test_unknown_session_is_404():
    client, _ = make()
    assert client.post("/api/sessions/nope/decide", json={"state": STATE}).status_code == 404


def test_a_failing_sink_returns_502_and_keeps_decisions_for_retry():
    class Flaky(MemorySink):
        fail = True

        def write_events(self, rows):
            if self.fail:
                raise RuntimeError("warehouse down")
            super().write_events(rows)

    sink = Flaky()
    client, _ = make(sink)
    sid = start(client).json()["session_id"]
    client.post(f"/api/sessions/{sid}/decide", json={"state": STATE})
    events = {"events": [{"tic": 1, "health": 90, "x": 0, "y": 0, "angle": 0}]}

    assert client.post(f"/api/sessions/{sid}/events", json=events).status_code == 502
    assert sink.decisions == []

    sink.fail = False
    assert client.post(f"/api/sessions/{sid}/events", json=events).status_code == 202
    assert len(sink.decisions) == 1 and len(sink.events) == 1


def test_a_crashing_player_is_logged_as_a_failed_decision_not_a_500():
    from pdoom.models import PlayerType
    from pdoom.players import PlayerRegistry

    class Boom:
        player_type = PlayerType.rule_bot

        def decide(self, state, screenshot_b64=None):
            raise RuntimeError("boom")

    registry = PlayerRegistry()
    registry.register(Boom())
    sink = MemorySink()
    client = TestClient(create_app(Settings(_env_file=None), sink=sink, players=registry))
    sid = start(client).json()["session_id"]

    body = client.post(f"/api/sessions/{sid}/decide", json={"state": STATE})
    assert body.status_code == 200
    assert body.json()["intent"] is None and "boom" in body.json()["error"]


def test_stopped_and_abandoned_games_write_a_session_row():
    for outcome in ("aborted", "abandoned"):
        client, sink = make()
        sid = start(client).json()["session_id"]
        ended = client.post(
            f"/api/sessions/{sid}/end", json={"outcome": outcome, "duration_s": 3.0, "kills": 1}
        )
        assert ended.status_code == 200
        assert [r.outcome for r in sink.sessions] == [outcome]


class OpenSink(MemorySink):
    """Like the Lakebase sink: the session row is written at the start and again at the end."""

    def __init__(self):
        super().__init__()
        self.by_id = {}
        self.reachable = True

    def write_open_session(self, rows):
        self.write_sessions(rows)

    def write_sessions(self, rows):
        for r in rows:
            self.by_id[r.session_id] = r
        super().write_sessions(rows)

    def ping(self):
        if not self.reachable:
            raise RuntimeError("down")
        return True


def test_a_game_writes_an_open_row_at_the_start_and_a_full_row_at_the_end():
    client, sink = make(OpenSink())
    sid = start(client).json()["session_id"]
    opened = sink.by_id[sid]
    assert opened.outcome is None and opened.ended_at is None and opened.decisions is None
    for tic in (10, 20, 30):
        client.post(f"/api/sessions/{sid}/decide", json={"state": {**STATE, "tic": tic}})
    client.post(
        f"/api/sessions/{sid}/end",
        json={"outcome": "died", "duration_s": 5, "kills": 0, "decision_timeouts": 2},
    )
    done = sink.by_id[sid]
    assert (done.outcome, done.decisions, done.decision_errors, done.decision_timeouts) == (
        "died", 3, 0, 2,
    )


def test_failed_decisions_are_counted_on_the_session_row():
    class Broken:
        player_type = PlayerType.rule_bot

        def decide(self, state, screenshot_b64=None):
            raise RuntimeError("boom")

    reg = build_registry(Settings(_env_file=None), None)
    reg.register(Broken())
    sink = MemorySink()
    client = TestClient(create_app(Settings(_env_file=None, sink="memory"), sink=sink, players=reg))
    sid = start(client).json()["session_id"]
    client.post(f"/api/sessions/{sid}/decide", json={"state": STATE})
    client.post(f"/api/sessions/{sid}/end", json={"outcome": "died", "duration_s": 1})
    row = sink.sessions[-1]
    assert (row.decisions, row.decision_errors) == (1, 1)


def test_a_retried_end_is_answered_not_rejected():
    client, sink = make()
    sid = start(client).json()["session_id"]
    body = {"outcome": "cleared", "duration_s": 9, "kills": 1}
    first = client.post(f"/api/sessions/{sid}/end", json=body)
    again = client.post(f"/api/sessions/{sid}/end", json=body)
    assert first.status_code == again.status_code == 200
    assert again.json()["already_ended"] == "true" and again.json()["outcome"] == "cleared"
    assert len(sink.sessions) == 1  # not written twice
    assert client.post("/api/sessions/never-existed/end", json=body).status_code == 404


def test_idle_sessions_are_swept_into_abandoned_rows():
    sink = OpenSink()
    app = create_app(Settings(_env_file=None, sink="memory", session_ttl_s=0), sink=sink)
    client = TestClient(app)
    sid = start(client).json()["session_id"]
    client.post(f"/api/sessions/{sid}/decide", json={"state": STATE})
    assert app.state.sweep() == 1
    row = sink.by_id[sid]
    assert row.outcome == "abandoned" and row.decisions == 1 and row.ended_at is not None
    assert len(sink.decisions) == 1  # pending decisions were flushed
    assert app.state.sweep() == 0
    late = client.post(f"/api/sessions/{sid}/end", json={"outcome": "died", "duration_s": 1})
    assert late.json()["outcome"] == "abandoned"  # does not rewrite the row


def test_health_reports_an_unreachable_sink_as_degraded():
    sink = OpenSink()
    sink.reachable = False
    client, _ = make(sink)
    health = client.get("/api/health").json()
    assert health["status"] == "degraded" and health["sink_reachable"] is False


def test_error_outcome_is_accepted():
    client, sink = make()
    sid = start(client).json()["session_id"]
    ended = client.post(f"/api/sessions/{sid}/end", json={"outcome": "error", "duration_s": 1})
    assert ended.status_code == 200 and sink.sessions[0].outcome == "error"
