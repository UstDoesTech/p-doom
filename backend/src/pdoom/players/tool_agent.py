"""tool_agent: a language model that looks at the game through tools, then acts.

Unlike ai_decide, which gets the state in one prompt, the agent chooses what to inspect. It calls
scan tools over a few turns and ends with one `act` call. The transcript of tool calls goes into
raw_output so a run can be read afterwards. Needs a chat endpoint that supports tool calling
(PDOOM_AGENT_ENDPOINT). Tested with a fake client; not yet run against a real endpoint.
"""

from __future__ import annotations

import json
import time
from importlib import resources
from typing import Any

from ..models import Decision, GameState, Intent, PlayerType

MAX_TURNS = 5
DEADLINE_S = 20.0  # for the whole loop
CALL_TIMEOUT_S = 10.0  # for one model call

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "scan_enemies",
            "description": "Enemies, nearest first: type, distance, bearing, line_of_sight.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scan_pickups",
            "description": "Visible pickups, nearest first: type, distance, bearing.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "exit_info",
            "description": "Bearing in degrees to the level exit, if known.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "self_status",
            "description": "Your health, armor, weapon, ammo and facing.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "act",
            "description": "Choose the intent to hold for the next second. Ends the turn.",
            "parameters": {
                "type": "object",
                "properties": {
                    "intent": {"type": "string", "enum": [i.value for i in Intent]},
                    "reason": {"type": "string"},
                },
                "required": ["intent"],
            },
        },
    },
]


def load_system_prompt() -> str:
    return resources.files("pdoom.players").joinpath("tool_agent_system.txt").read_text("utf-8")


def run_tool(name: str, state: GameState) -> dict[str, Any]:
    if name == "scan_enemies":
        return {"enemies": [e.model_dump(mode="json") for e in state.enemies]}
    if name == "scan_pickups":
        return {"pickups": [p.model_dump(mode="json") for p in state.pickups]}
    if name == "exit_info":
        return {"exit_bearing": state.exit_bearing}
    if name == "self_status":
        return state.player.model_dump(mode="json")
    return {"error": f"unknown tool {name}"}


class ToolAgent:
    player_type = PlayerType.tool_agent

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
        t0 = time.perf_counter()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system},
            {"role": "user", "content": f"Tic {state.tic}, level {state.level}. Decide."},
        ]
        trace: list[dict[str, Any]] = []

        def done(**kw: Any) -> Decision:
            kw.setdefault("raw_output", {"calls": trace})
            return Decision(latency_ms=int((time.perf_counter() - t0) * 1000), **kw)

        deadline = t0 + DEADLINE_S
        for _ in range(MAX_TURNS):
            remaining = deadline - time.perf_counter()
            if remaining <= 0.5:
                return done(error=f"Agent ran out of time after {DEADLINE_S:.0f} s")
            reply = self.client.chat.completions.create(
                timeout=min(CALL_TIMEOUT_S, remaining),
                model=self.endpoint,
                temperature=0,
                max_tokens=300,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
            )
            msg = reply.choices[0].message
            calls = getattr(msg, "tool_calls", None) or []
            if not calls:
                text = (msg.content or "").strip().lower()
                for intent in Intent:
                    if text == intent.value:
                        trace.append({"tool": "text_reply", "intent": intent.value})
                        return done(intent=intent)
                return done(
                    error="Agent ended without calling act",
                    raw_output={"calls": trace, "text": text[:500]},
                )
            messages.append(
                {
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {
                                "name": c.function.name,
                                "arguments": c.function.arguments,
                            },
                        }
                        for c in calls
                    ],
                }
            )
            for c in calls:
                name = c.function.name
                try:
                    args = json.loads(c.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                if name == "act":
                    intent = args.get("intent")
                    trace.append({"tool": "act", **{k: args.get(k) for k in ("intent", "reason")}})
                    if intent in Intent.__members__:
                        return done(intent=Intent(intent))
                    result = {"error": f"intent must be one of {[i.value for i in Intent]}"}
                else:
                    result = run_tool(name, state)
                    trace.append({"tool": name})
                messages.append(
                    {"role": "tool", "tool_call_id": c.id, "content": json.dumps(result)}
                )
        return done(error=f"No act call within {MAX_TURNS} turns")
