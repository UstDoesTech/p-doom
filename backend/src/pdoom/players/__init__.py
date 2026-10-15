"""Player registry. One entry per PlayerType, available or not, with the reason when not."""

from __future__ import annotations

from ..config import Settings
from ..databricks_sql import SqlRunner
from ..models import PlayerType
from .ai_decide import AiDecidePlayer
from .base import Player, PlayerUnavailable
from .bc_policy import DEFAULT_MODEL as BC_DEFAULT
from .bc_policy import BcPolicy
from .rl_policy import DEFAULT_WEIGHTS as RL_DEFAULT
from .rl_policy import RlPolicy
from .rule_bot import RuleBot
from .supervisor import Supervisor
from .tool_agent import ToolAgent
from .vision_llm import VisionPlayer

__all__ = ["PlayerRegistry", "PlayerUnavailable", "build_registry"]

class PlayerRegistry:
    def __init__(self) -> None:
        self._players: dict[PlayerType, Player] = {}
        self._unavailable: dict[PlayerType, tuple[int, str]] = {}

    def register(self, player: Player) -> None:
        self._players[player.player_type] = player

    def mark_unavailable(self, player_type: PlayerType, status_code: int, reason: str) -> None:
        self._unavailable[player_type] = (status_code, reason)

    def get(self, player_type: PlayerType) -> Player:
        if player_type in self._players:
            return self._players[player_type]
        status, reason = self._unavailable.get(player_type, (501, "Not built yet"))
        raise PlayerUnavailable(status, reason)

    def describe(self) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for pt in PlayerType:
            if pt in self._players:
                rows.append({"player_type": pt.value, "available": True, "reason": None})
            else:
                reason = self._unavailable.get(pt, (501, "Not built yet"))[1]
                rows.append({"player_type": pt.value, "available": False, "reason": reason})
        return rows


def build_registry(settings: Settings, sql: SqlRunner | None) -> PlayerRegistry:
    reg = PlayerRegistry()
    reg.mark_unavailable(PlayerType.human, 400, "Humans play in the browser; no decisions needed")
    reg.register(RuleBot())

    if sql is not None:
        reg.register(AiDecidePlayer(sql))
    else:
        reg.mark_unavailable(PlayerType.ai_decide, 503, "PDOOM_WAREHOUSE_ID is not set")

    if settings.vision_endpoint:
        reg.register(VisionPlayer(settings.vision_endpoint))
    else:
        reg.mark_unavailable(PlayerType.vision_llm, 503, "PDOOM_VISION_ENDPOINT is not set")

    for player_type, loader in (
        (PlayerType.rl_policy, lambda: RlPolicy.from_file(settings.rl_model or RL_DEFAULT)),
        (PlayerType.bc_policy, lambda: BcPolicy.from_file(settings.bc_model or BC_DEFAULT)),
    ):
        try:
            reg.register(loader())
        except PlayerUnavailable as exc:
            reg.mark_unavailable(player_type, exc.status_code, exc.reason)

    if settings.agent_endpoint:
        reg.register(ToolAgent(settings.agent_endpoint))
    else:
        reg.mark_unavailable(PlayerType.tool_agent, 503, "PDOOM_AGENT_ENDPOINT is not set")

    workers = [
        p for name in settings.supervisor_workers
        if (p := reg._players.get(PlayerType(name))) is not None  # noqa: SLF001
    ]
    if workers:
        reg.register(Supervisor(workers))
    else:
        reg.mark_unavailable(PlayerType.supervisor, 503, "No worker players to route to")
    return reg
