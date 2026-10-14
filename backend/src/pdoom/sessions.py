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
    pending_decisions: list[DecisionRow] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def touch(self) -> None:
        self.last_seen = time.monotonic()


class SessionStore:
    def __init__(self, model_version: str, ttl_s: int = 3600) -> None:
        self.model_version = model_version
        self.ttl_s = ttl_s
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self, req: CreateSession) -> Session:
        self._evict_stale()
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

    def remove(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def _evict_stale(self) -> None:
        cutoff = time.monotonic() - self.ttl_s
        with self._lock:
            for sid in [s.id for s in self._sessions.values() if s.last_seen < cutoff]:
                del self._sessions[sid]

    def __len__(self) -> int:
        return len(self._sessions)
