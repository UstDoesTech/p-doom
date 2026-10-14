"""Plays Doom from a screenshot with a vision-capable Databricks serving endpoint.

Uses the OpenAI-compatible client the Databricks SDK provides for serving endpoints. Not yet
run against a real endpoint: set PDOOM_VISION_ENDPOINT to a multimodal chat endpoint to try it.
"""

from __future__ import annotations

import json
import re
from importlib import resources
from typing import Any

from ..models import Decision, GameState, Intent, PlayerType

_JSON = re.compile(r"\{.*\}", re.DOTALL)


def load_system_prompt() -> str:
    return resources.files("pdoom.players").joinpath("vision_system.txt").read_text("utf-8")


def parse_action(text: str) -> Decision:
    match = _JSON.search(text or "")
    if not match:
        return Decision(error="No JSON in model reply", raw_output=(text or "")[:2000])
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        return Decision(error=f"Bad JSON: {exc}", raw_output=(text or "")[:2000])
    action = obj.get("action")
    if action not in Intent.__members__:
        return Decision(error=f"Unknown action {action!r}", raw_output=obj)
    return Decision(intent=Intent(action), raw_output=obj)


class VisionPlayer:
    player_type = PlayerType.vision_llm

    def __init__(self, endpoint: str, client: Any = None) -> None:
        self.endpoint = endpoint
        self._client = client
        self._system = load_system_prompt()

    @property
    def client(self) -> Any:
        if self._client is None:
            from databricks.sdk import WorkspaceClient

            self._client = WorkspaceClient().serving_endpoints.get_open_ai_client()
        return self._client

    def decide(self, state: GameState, screenshot_b64: str | None = None) -> Decision:
        if not screenshot_b64:
            return Decision(error="vision_llm needs a screenshot")
        reply = self.client.chat.completions.create(
            model=self.endpoint,
            temperature=0,
            max_tokens=120,
            messages=[
                {"role": "system", "content": self._system},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Frame attached."},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{screenshot_b64}"},
                        },
                    ],
                },
            ],
        )
        return parse_action(reply.choices[0].message.content or "")
