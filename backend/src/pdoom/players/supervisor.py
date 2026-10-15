"""supervisor: a router that decides who plays this decision.

rule_bot runs on every call. It keeps the memory the other players do not have (am I stuck, was I
just hurt) and its rule names the situation. The supervisor reads the situation and picks:

  stuck, pickup     -> rule_bot. Stuck needs its memory; the learned workers have none.
  navigate, combat  -> the first available learned worker, in the order given. In the mock arena
                       rule_bot's own combat rules lose to the learned policy, so combat goes out.

If the worker errors or returns no intent, rule_bot's choice is used and the decision says so.
raw_output records the situation, the worker, whether it fell back, and the inner outputs.
"""

from __future__ import annotations

import time

from ..models import Decision, GameState, PlayerType
from .base import Player
from .rule_bot import RuleBot

COMBAT = {"low_health_retreat", "dodge", "enemy_in_cone", "face_enemy", "hit_by_unseen"}
DEFAULT_ROUTES = {"navigate": True, "combat": True, "pickup": False, "stuck": False}


def situation(rule: str | None) -> str:
    rule = rule or ""
    if rule.startswith("stuck"):
        return "stuck"
    if rule in COMBAT:
        return "combat"
    if rule.startswith("face_") and rule not in ("face_exit", "face_enemy"):
        return "pickup"
    if rule in ("health", "ammo", "weapon", "armor"):
        return "pickup"
    return "navigate"


class Supervisor:
    player_type = PlayerType.supervisor

    def __init__(
        self,
        workers: list[Player],
        rule_bot: RuleBot | None = None,
        routes: dict[str, bool] | None = None,
    ) -> None:
        self.workers = workers
        self.rule_bot = rule_bot or RuleBot()
        self.routes = {**DEFAULT_ROUTES, **(routes or {})}

    def decide(self, state: GameState, screenshot_b64: str | None = None) -> Decision:
        t0 = time.perf_counter()
        base = self.rule_bot.decide(state, screenshot_b64)
        rule = base.raw_output.get("rule") if isinstance(base.raw_output, dict) else None
        sit = situation(rule)
        out: dict = {"situation": sit, "rule": rule, "worker": None, "fallback_used": False}
        if not self.routes.get(sit) or not self.workers:
            out["routed_to"] = "rule_bot"
            return base.model_copy(update={"raw_output": out, "latency_ms": self._ms(t0)})
        worker = self.workers[0]
        got = worker.decide(state, screenshot_b64)
        if got.intent is None:
            out.update(worker=worker.player_type.value, routed_to="rule_bot", fallback_used=True,
                       worker_error=got.error)
            return base.model_copy(update={"raw_output": out, "latency_ms": self._ms(t0)})
        out.update(worker=worker.player_type.value, routed_to=worker.player_type.value,
                   inner=got.raw_output)
        return got.model_copy(update={"raw_output": out, "latency_ms": self._ms(t0)})

    @staticmethod
    def _ms(t0: float) -> int:
        return int((time.perf_counter() - t0) * 1000)
