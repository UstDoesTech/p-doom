"""The baseline: if-then rules over the state JSON. Every other player has to beat this."""

from __future__ import annotations

from ..models import Decision, GameState, Intent, PlayerType

FIRE_CONE_DEG = 8.0
ENGAGE_RANGE = 700.0
RETREAT_HEALTH = 30
EXIT_ALIGN_DEG = 15.0


class RuleBot:
    player_type = PlayerType.rule_bot

    def decide(self, state: GameState, screenshot_b64: str | None = None) -> Decision:
        visible = [e for e in state.enemies if e.line_of_sight]
        nearest = min(visible, key=lambda e: e.distance, default=None)

        if nearest and state.player.health < RETREAT_HEALTH and nearest.distance < ENGAGE_RANGE:
            return self._make(Intent.retreat, "low_health_retreat")

        if nearest:
            if abs(nearest.bearing) <= FIRE_CONE_DEG:
                return self._make(Intent.fire, "enemy_in_cone")
            turn = Intent.turn_left if nearest.bearing < 0 else Intent.turn_right
            return self._make(turn, "face_enemy")

        bearing = state.exit_bearing
        if bearing is not None and abs(bearing) > EXIT_ALIGN_DEG:
            turn = Intent.turn_left if bearing < 0 else Intent.turn_right
            return self._make(turn, "face_exit")

        return self._make(Intent.advance, "advance")

    @staticmethod
    def _make(intent: Intent, rule: str) -> Decision:
        return Decision(intent=intent, raw_output={"rule": rule})
