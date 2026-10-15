import json
from types import SimpleNamespace

import pytest

from pdoom.config import Settings
from pdoom.features import N_FEATURES, featurize
from pdoom.models import Decision, GameState, Intent, PlayerType
from pdoom.players import PlayerUnavailable, build_registry
from pdoom.players.bc_policy import BcPolicy, action_to_intent, vector
from pdoom.players.rl_policy import ACTIONS, DEFAULT_WEIGHTS, RlPolicy
from pdoom.players.supervisor import Supervisor, situation
from pdoom.players.tool_agent import ToolAgent
from pdoom.sim import MockWorld, evaluate
from pdoom.train import bc as bc_train


def state(**over):
    base = {
        "tic": 0,
        "level": "E1M1",
        "seed": 1,
        "player": {"health": 100, "armor": 0, "weapon": "pistol", "ammo": {"bullets": 50},
                   "x": 0, "y": 0, "angle": 90},
        "enemies": [],
        "pickups": [],
        "exit_bearing": 0.0,
    }
    base.update(over)
    return GameState.model_validate(base)


ENEMY_AHEAD = [{"type": "zombieman", "distance": 400, "bearing": 2.0, "line_of_sight": True}]


def test_features_have_fixed_length_and_react_to_enemies():
    quiet = featurize(state())
    busy = featurize(state(enemies=ENEMY_AHEAD))
    assert len(quiet) == len(busy) == N_FEATURES
    assert quiet[7] == 0.0 and busy[7] == 1.0  # in-cone flag


def test_sim_is_deterministic_per_seed():
    a, b = MockWorld(5), MockWorld(5)
    for _ in range(100):
        a.step("forward")
        b.step("forward")
    assert (a.x, a.y, a.angle) == (b.x, b.y, b.angle)


# ---- rl_policy --------------------------------------------------------------------------


def test_shipped_rl_weights_beat_rule_bot_in_the_sim():
    rl = RlPolicy.from_file(DEFAULT_WEIGHTS)
    result = evaluate(rl, range(500, 540))
    assert result["clear_rate"] >= 0.7


def test_rl_policy_picks_the_most_likely_intent():
    weights = [[0.0] * N_FEATURES for _ in ACTIONS]
    fire = ACTIONS.index(Intent.fire)
    weights[fire][7] = 5.0  # in-cone feature
    d = RlPolicy(weights).decide(state(enemies=ENEMY_AHEAD))
    assert d.intent == Intent.fire and d.confidence > 0.5
    assert set(d.raw_output["probs"]) == {a.value for a in ACTIONS}


def test_rl_policy_rejects_weights_for_other_features(tmp_path):
    path = tmp_path / "w.json"
    path.write_text(json.dumps({"actions": [a.value for a in ACTIONS], "weights": [[0.0]]}))
    with pytest.raises(PlayerUnavailable):
        RlPolicy.from_file(path)
    with pytest.raises(PlayerUnavailable):
        RlPolicy.from_file(tmp_path / "missing.json")


# ---- bc_policy --------------------------------------------------------------------------


def test_action_strings_become_one_intent():
    assert action_to_intent("advance+fire") == Intent.fire
    assert action_to_intent("forward") == Intent.advance
    assert action_to_intent("strafe_left+turn_right") == Intent.strafe_left
    assert action_to_intent("") is None and action_to_intent("jump") is None


def test_bc_policy_copies_the_nearest_human_moves():
    rows = (
        [{"level": "E1M1", "x": 0, "y": 0, "angle": 90, "health": 100, "action": "advance"}] * 5
        + [{"level": "E1M1", "x": 5000, "y": 0, "angle": 0, "health": 100, "action": "use"}] * 5
    )
    model = bc_train.build(rows, "test")
    assert model["meta"]["rows"] == 10
    policy = BcPolicy({k: [(v, a) for v, a in r] for k, r in model["samples"].items()})
    assert policy.decide(state()).intent == Intent.advance
    far = state(player={"health": 100, "armor": 0, "weapon": "pistol", "ammo": {}, "x": 5000,
                        "y": 0, "angle": 0})
    assert policy.decide(far).intent == Intent.use
    assert policy.decide(state(level="E1M2")).intent is None  # no demonstrations


def test_bc_training_reads_jsonl_and_keeps_only_cleared_human_wasm(tmp_path):
    def sess(sid, ptype, outcome, engine="wasm"):
        return {"table": "doom_sessions", "session_id": sid, "player_type": ptype,
                "level": "E1M1", "outcome": outcome, "engine": engine}

    def ev(sid):
        return {"table": "doom_events", "session_id": sid, "tic": 1, "x": 1, "y": 2,
                "angle": 0, "health": 90, "action": "advance"}

    lines = [sess("a", "human", "cleared"), sess("b", "human", "died"),
             sess("c", "rule_bot", "cleared"), sess("d", "human", "cleared", "mock"),
             ev("a"), ev("b"), ev("c"), ev("d")]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in lines))
    rows = bc_train.rows_from_jsonl(path)
    assert {r["session_id"] for r in rows} == {"a", "b"}
    assert len(vector(1, 2, 0, 90)) == 5


# ---- tool_agent -------------------------------------------------------------------------


def call(name, args=None, cid="1"):
    return SimpleNamespace(
        id=cid, function=SimpleNamespace(name=name, arguments=json.dumps(args or {}))
    )


def reply(*calls, content=""):
    msg = SimpleNamespace(content=content, tool_calls=list(calls))
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


class FakeClient:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.requests.append(json.loads(json.dumps(kw["messages"], default=str)))
        return self.replies.pop(0)


def test_tool_agent_scans_then_acts():
    client = FakeClient(
        reply(call("scan_enemies"), call("self_status", cid="2")),
        reply(call("act", {"intent": "fire", "reason": "zombie ahead"}, cid="3")),
    )
    d = ToolAgent("ep", client).decide(state(enemies=ENEMY_AHEAD))
    assert d.intent == Intent.fire
    assert [c["tool"] for c in d.raw_output["calls"]] == ["scan_enemies", "self_status", "act"]
    tool_msgs = [m for m in client.requests[1] if m["role"] == "tool"]
    assert "zombieman" in tool_msgs[0]["content"]


def test_tool_agent_reports_bad_intents_and_runaway_loops():
    bad = FakeClient(reply(call("act", {"intent": "dance"})),
                     reply(call("act", {"intent": "advance"})))
    assert ToolAgent("ep", bad).decide(state()).intent == Intent.advance  # retried after the error

    loop = FakeClient(*[reply(call("exit_info")) for _ in range(5)])
    d = ToolAgent("ep", loop).decide(state())
    assert d.intent is None and "within" in d.error

    silent = FakeClient(reply(content="I think we should wait"))
    assert ToolAgent("ep", silent).decide(state()).error.startswith("Agent ended")


# ---- supervisor -------------------------------------------------------------------------


class Stub:
    def __init__(self, ptype, intent=Intent.turn_left, error=None):
        self.player_type = ptype
        self.intent, self.error, self.calls = intent, error, 0

    def decide(self, state, screenshot_b64=None):
        self.calls += 1
        return Decision(intent=None if self.error else self.intent, error=self.error,
                        raw_output={"stub": True})


def test_situations_come_from_the_rule_that_fired():
    assert situation("stuck_use") == "stuck"
    assert situation("enemy_in_cone") == "combat"
    assert situation("face_health") == "pickup"
    assert situation("face_exit") == "navigate"
    assert situation("advance") == "navigate"
    assert situation(None) == "navigate"


def test_supervisor_routes_navigation_to_the_worker_and_keeps_stuck_with_rule_bot():
    worker = Stub(PlayerType.rl_policy)
    sup = Supervisor([worker])
    d = sup.decide(state())  # nothing around: navigate
    assert d.intent == Intent.turn_left and worker.calls == 1
    assert d.raw_output["situation"] == "navigate" and d.raw_output["routed_to"] == "rl_policy"

    sup2 = Supervisor([worker], routes={"navigate": False})
    d2 = sup2.decide(state())
    assert d2.raw_output["routed_to"] == "rule_bot" and worker.calls == 1


def test_supervisor_falls_back_to_rule_bot_when_the_worker_fails():
    sup = Supervisor([Stub(PlayerType.tool_agent, error="endpoint down")])
    d = sup.decide(state())
    assert d.intent is not None
    assert d.raw_output["fallback_used"] is True and d.raw_output["worker_error"] == "endpoint down"


# ---- registry ---------------------------------------------------------------------------


def test_registry_wires_the_new_players():
    reg = build_registry(Settings(_env_file=None, agent_endpoint="chat-ep"), None)
    rows = {r["player_type"]: r for r in reg.describe()}
    assert rows["rl_policy"]["available"] and rows["tool_agent"]["available"]
    assert rows["supervisor"]["available"]
    assert not rows["bc_policy"]["available"] and "bc_policy" in rows["bc_policy"]["reason"]
    bare = build_registry(Settings(_env_file=None), None)
    no_agent = {r["player_type"]: r for r in bare.describe()}
    assert no_agent["tool_agent"]["reason"] == "PDOOM_AGENT_ENDPOINT is not set"
