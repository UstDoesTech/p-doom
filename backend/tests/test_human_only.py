"""The public app for human players: what it accepts, and what it refuses."""

from fastapi.testclient import TestClient

from pdoom.config import Settings
from pdoom.limits import RateLimiter, client_ip
from pdoom.main import create_app
from pdoom.telemetry import MemorySink

GOOD = {"player_type": "human", "mode": "realtime", "engine": "wasm", "level": "E1M1", "seed": 4}
EVENT = {"tic": 5, "health": 100, "armor": 0, "weapon": "pistol", "ammo": 50, "x": 1, "y": 2,
         "angle": 90, "action": "advance"}


def make(**settings):
    sink = MemorySink()
    app = create_app(
        Settings(_env_file=None, sink="memory", human_only=True, **settings), sink=sink
    )
    return TestClient(app), sink


def test_a_human_game_is_recorded_like_any_other():
    client, sink = make()
    sid = client.post("/api/sessions", json=GOOD).json()["session_id"]
    assert client.post(f"/api/sessions/{sid}/events", json={"events": [EVENT]}).status_code == 202
    end = {"outcome": "died", "duration_s": 31.5, "kills": 3}
    assert client.post(f"/api/sessions/{sid}/end", json=end).status_code == 200
    row = sink.sessions[-1]
    assert (row.player_type, row.engine, row.mode, row.outcome) == (
        "human", "wasm", "realtime", "died"
    )
    assert len(sink.events) == 1


def test_it_says_so_and_lists_only_people():
    client, _ = make()
    assert client.get("/api/health").json()["human_only"] is True
    players = {p["player_type"]: p for p in client.get("/api/players").json()}
    assert not any(p["available"] for p in players.values())
    assert players["rule_bot"]["reason"] == "This app is for human players only"


def test_other_players_are_refused_and_nothing_is_built_for_them():
    client, _ = make()
    for pt in ("rule_bot", "ai_decide", "vision_llm", "tool_agent", "supervisor"):
        r = client.post("/api/sessions", json={**GOOD, "player_type": pt})
        assert r.status_code == 403, pt


def test_decide_is_closed():
    client, _ = make()
    sid = client.post("/api/sessions", json=GOOD).json()["session_id"]
    player = {"health": 9, "x": 0, "y": 0, "angle": 0}
    state = {"tic": 1, "level": "E1M1", "seed": 4, "player": player}
    assert client.post(f"/api/sessions/{sid}/decide", json={"state": state}).status_code == 403


def test_the_mock_arena_and_junk_levels_and_seeds_are_refused():
    client, _ = make()
    assert client.post("/api/sessions", json={**GOOD, "engine": "mock"}).status_code == 422
    for level in ("E5M1", "E1M10", "MAP01", "E1M1; drop table", ""):
        assert client.post("/api/sessions", json={**GOOD, "level": level}).status_code == 422, level
    assert client.post("/api/sessions", json={**GOOD, "seed": 2**40}).status_code == 422


def test_new_games_are_limited_per_address():
    client, _ = make(sessions_per_minute=3)
    codes = [client.post("/api/sessions", json=GOOD).status_code for _ in range(5)]
    assert codes == [201, 201, 201, 429, 429]


def test_the_arena_can_be_full():
    client, _ = make(max_open_sessions=2, sessions_per_minute=100)
    codes = [client.post("/api/sessions", json=GOOD).status_code for _ in range(3)]
    assert codes == [201, 201, 503]


def test_one_game_cannot_send_unlimited_events():
    client, _ = make(max_events_per_session=3, sessions_per_minute=100)
    sid = client.post("/api/sessions", json=GOOD).json()["session_id"]
    ok = client.post(f"/api/sessions/{sid}/events", json={"events": [EVENT] * 3})
    assert ok.status_code == 202
    over = client.post(f"/api/sessions/{sid}/events", json={"events": [{**EVENT, "tic": 9}]})
    assert over.status_code == 429


def test_the_full_app_is_not_affected():
    sink = MemorySink()
    app = create_app(Settings(_env_file=None, sink="memory"), sink=sink)
    client = TestClient(app)
    mock = {**GOOD, "engine": "mock", "player_type": "rule_bot"}
    codes = [client.post("/api/sessions", json=mock).status_code for _ in range(30)]
    assert set(codes) == {201}
    assert client.get("/api/health").json()["human_only"] is False


def test_the_rate_limiter_forgets():
    now = [0.0]
    limiter = RateLimiter(2, window_s=60, clock=lambda: now[0])
    assert [limiter.allow("a"), limiter.allow("a"), limiter.allow("a")] == [True, True, False]
    assert limiter.allow("b")
    now[0] = 61
    assert limiter.allow("a")
    assert RateLimiter(0).allow("x")  # 0 is off


def test_behind_the_ingress_the_last_forwarded_address_counts():
    xff = "6.6.6.6, 203.0.113.9"  # the first entry is whatever the client claimed
    assert client_ip(xff, "10.0.0.4", trust_proxy=True) == "203.0.113.9"
    assert client_ip(xff, "10.0.0.4", trust_proxy=False) == "10.0.0.4"
    assert client_ip(None, "10.0.0.4", trust_proxy=True) == "10.0.0.4"
