"""In-memory session store.

Sessions hold the decisions waiting to be flushed, so the container runs ONE replica
(see infra/main.bicep). Move this to Redis or Lakebase before scaling out.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .models import CreateSession, DecisionRow, Mode, PlayerType


@dataclass
class Session:
    id: str
    player_type: PlayerType
    mode: Mode
    engine: str
    level: str
    seed: int
    model_version: str
    started_at: datetime
    last_seen: float = field(default_factory=time.monotonic)
    last_seen_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    decisions: int = 0
    decision_errors: int = 0
    pending_decisions: list[DecisionRow] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def touch(self) -> None:
        self.last_seen = time.monotonic()
        self.last_seen_at = datetime.now(UTC)

    def count_decision(self, failed: bool) -> None:
        with self.lock:
            self.decisions += 1
            self.decision_errors += 1 if failed else 0


class SessionStore:
    def __init__(self, model_version: str, ttl_s: int = 900, tombstone_s: int = 3600) -> None:
        self.model_version = model_version
        self.ttl_s = ttl_s
        self.tombstone_s = tombstone_s
        self._sessions: dict[str, Session] = {}
        self._ended: dict[str, tuple[str, float]] = {}  # id -> (outcome, when)
        self._lock = threading.Lock()

    def create(self, req: CreateSession) -> Session:
        session = Session(
            id=str(uuid.uuid4()),
            player_type=req.player_type,
            mode=req.mode,
            engine=req.engine,
            level=req.level,
            seed=req.seed,
            model_version=self.model_version,
            started_at=datetime.now(UTC),
        )
        with self._lock:
            self._sessions[session.id] = session
        return session

    def get(self, session_id: str) -> Session:
        with self._lock:
            session = self._sessions[session_id]  # KeyError -> 404 in the API layer
        session.touch()
        return session

    def remove(self, session_id: str, outcome: str | None = None) -> None:
        """Forget a session. With an outcome, remember it for a while so a retried end call
        (whose first reply was lost) can be answered instead of failing."""
        with self._lock:
            self._sessions.pop(session_id, None)
            if outcome is not None:
                now = time.monotonic()
                self._ended[session_id] = (outcome, now)
                for sid in [k for k, (_, at) in self._ended.items() if now - at > self.tombstone_s]:
                    del self._ended[sid]

    def ended_outcome(self, session_id: str) -> str | None:
        with self._lock:
            found = self._ended.get(session_id)
        return found[0] if found else None

    def pop_stale(self) -> list[Session]:
        """Remove and return sessions nobody has touched for ttl_s."""
        cutoff = time.monotonic() - self.ttl_s
        with self._lock:
            stale = [s for s in self._sessions.values() if s.last_seen < cutoff]
            for s in stale:
                del self._sessions[s.id]
                self._ended[s.id] = ("abandoned", time.monotonic())
        return stale

    def __len__(self) -> int:
        return len(self._sessions)
