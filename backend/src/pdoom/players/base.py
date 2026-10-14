from __future__ import annotations

from typing import Protocol

from ..models import Decision, GameState, PlayerType


class Player(Protocol):
    """A machine player. decide() is synchronous and is called from a thread-pool handler."""

    player_type: PlayerType

    def decide(self, state: GameState, screenshot_b64: str | None = None) -> Decision: ...


class PlayerUnavailable(Exception):
    def __init__(self, status_code: int, reason: str) -> None:
        super().__init__(reason)
        self.status_code = status_code
        self.reason = reason
