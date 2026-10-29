"""FastAPI app. Run with: uvicorn pdoom.main:create_app --factory"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import Settings, get_settings
from .databricks_sql import SqlRunner
from .lakebase import FanoutSink, PgSink, TokenSource
from .limits import RateLimiter, client_ip
from .models import (
    CreateSession,
    DecideRequest,
    DecideResponse,
    Decision,
    DecisionRow,
    EndRequest,
    EventRow,
    EventsRequest,
    PlayerType,
    SessionCreated,
    SessionRow,
)
from .players import PlayerRegistry, PlayerUnavailable, build_registry
from .sessions import SessionStore
from .telemetry import JsonlSink, MemorySink, SqlSink, TelemetrySink

log = logging.getLogger("pdoom")

# What a public, human-only app allows when the settings leave a limit at 0. A person at 35 tics a
# second sampled every 5 tics sends 7 events a second, so 60 000 is about two hours of play.
HUMAN_DEFAULTS = {
    "max_open_sessions": 300,
    "sessions_per_minute": 20,
    "max_events_per_session": 60_000,
}
LEVEL = re.compile(r"E[1-4]M[1-9]")
MAX_SEED = 2**31 - 1


def build_lakebase_sink(settings: Settings) -> PgSink:
    host = settings.pg_host or os.environ.get("PGHOST", "")
    database = settings.pg_database or os.environ.get("PGDATABASE", "")
    env = os.environ
    user = settings.pg_user or env.get("PGUSER") or env.get("DATABRICKS_CLIENT_ID", "")
    if not (host and database and user and settings.lakebase_endpoint):
        raise RuntimeError(
            "PDOOM_SINK=lakebase needs PDOOM_LAKEBASE_ENDPOINT plus a host, database and user "
            "(PDOOM_PG_HOST/DATABASE/USER, or PGHOST/PGDATABASE/PGUSER)"
        )
    return PgSink(
        host, database, user, TokenSource(settings.lakebase_endpoint), schema=settings.pg_schema
    )


def build_sink(settings: Settings, sql: SqlRunner | None) -> TelemetrySink:
    if settings.sink in ("sql", "both") and sql is None:
        raise RuntimeError(f"PDOOM_SINK={settings.sink} needs PDOOM_WAREHOUSE_ID")
    if settings.sink == "sql":
        return SqlSink(sql)
    if settings.sink == "lakebase":
        return build_lakebase_sink(settings)
    if settings.sink == "both":
        # Lakebase first: its writes are idempotent, so a retry after a warehouse failure is safe.
        return FanoutSink(build_lakebase_sink(settings), SqlSink(sql))
    if settings.sink == "jsonl":
        return JsonlSink(settings.jsonl_path)
    return MemorySink()


def human_registry() -> PlayerRegistry:
    """The registry of the human-only app: only people, so no model or warehouse is needed."""
    reg = PlayerRegistry()
    for pt in PlayerType:
        reason = (
            "Humans play in the browser; no decisions needed"
            if pt == PlayerType.human
            else "This app is for human players only"
        )
        reg.mark_unavailable(pt, 400 if pt == PlayerType.human else 403, reason)
    return reg


def check_human_session(body: CreateSession) -> None:
    """What the public app accepts: a person, on the real engine, on a real level. Anything else
    would put junk in the same tables the analysis reads."""
    if body.player_type != PlayerType.human:
        raise HTTPException(403, "This app is for human players only")
    if body.engine != "wasm":
        raise HTTPException(422, "Only the Doom engine is accepted here")
    if not LEVEL.fullmatch(body.level):
        raise HTTPException(422, "Unknown level")
    if abs(body.seed) > MAX_SEED:
        raise HTTPException(422, "Seed out of range")


def create_app(
    settings: Settings | None = None,
    sink: TelemetrySink | None = None,
    players: PlayerRegistry | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    sql = (
        SqlRunner(settings.warehouse_id, settings.uc_catalog, settings.uc_schema)
        if settings.warehouse_id
        else None
    )
    sink = sink or build_sink(settings, sql)
    if players is None:
        players = human_registry() if settings.human_only else build_registry(settings, sql)
    human_only = settings.human_only
    limit = {
        k: getattr(settings, k) or (HUMAN_DEFAULTS[k] if human_only else 0) for k in HUMAN_DEFAULTS
    }
    new_sessions = RateLimiter(limit["sessions_per_minute"])
    store = SessionStore(settings.model_version, settings.session_ttl_s)

    def sweep() -> int:
        """Record sessions nobody has touched for session_ttl_s as abandoned."""
        stale = store.pop_stale()
        for session in stale:
            try:
                _flush(session, sink)
                sink.write_sessions([_row(session, "abandoned", _idle_seconds(session), 0, 0)])
            except Exception:  # noqa: BLE001 - retried never; the open row still marks the game
                log.exception("Could not record abandoned session %s", session.id)
        return len(stale)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        stop = threading.Event()

        def loop() -> None:
            while not stop.wait(settings.sweep_interval_s):
                try:
                    sweep()
                except Exception:  # noqa: BLE001
                    log.exception("Session sweep failed")

        thread = threading.Thread(target=loop, name="session-sweep", daemon=True)
        thread.start()
        yield
        stop.set()

    app = FastAPI(title="p(doom)", version=__version__, lifespan=lifespan)
    app.state.sweep = sweep
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    api = APIRouter(prefix="/api")

    def session_or_404(session_id: str):
        try:
            return store.get(session_id)
        except KeyError:
            raise HTTPException(404, "Unknown or finished session") from None

    health_cache: dict[str, object] = {"at": 0.0, "ok": None}

    def sink_reachable() -> bool | None:
        """True or False for a sink that can be pinged (Lakebase), None for the rest. Cached for
        30 s so a health probe does not open a database connection every time."""
        ping = getattr(sink, "ping", None)
        if ping is None:
            return None
        if time.monotonic() - float(health_cache["at"]) > 30:
            try:
                health_cache["ok"] = bool(ping())
            except Exception:  # noqa: BLE001
                log.warning("Sink ping failed", exc_info=True)
                health_cache["ok"] = False
            health_cache["at"] = time.monotonic()
        return health_cache["ok"]  # type: ignore[return-value]

    @api.get("/health")
    def health() -> dict[str, object]:
        reachable = sink_reachable()
        return {
            "status": "ok" if reachable is not False else "degraded",
            "version": __version__,
            "sink": settings.sink,
            "sink_reachable": reachable,
            "warehouse_configured": sql is not None,
            "ai_decide_transports": [
                t for t, on in (("rest", settings.ai_decide_api), ("sql", sql is not None)) if on
            ],
            "open_sessions": len(store),
            "model_version": settings.model_version,
            "human_only": human_only,
        }

    @api.get("/players")
    def list_players() -> list[dict[str, object]]:
        return players.describe()

    @api.post("/sessions", response_model=SessionCreated, status_code=201)
    def create_session(body: CreateSession, request: Request) -> SessionCreated:
        if human_only:
            check_human_session(body)
            who = client_ip(
                request.headers.get("x-forwarded-for"),
                request.client.host if request.client else None,
                settings.trust_proxy,
            )
            if not new_sessions.allow(who):
                raise HTTPException(429, "Too many new games from this address. Wait a minute.")
            cap = limit["max_open_sessions"]
            if cap and len(store) >= cap:
                raise HTTPException(503, "The arena is full. Try again in a few minutes.")
        if body.player_type != PlayerType.human:
            try:
                players.get(body.player_type)
            except PlayerUnavailable as exc:
                raise HTTPException(exc.status_code, exc.reason) from None
        session = store.create(body)
        open_row = getattr(sink, "write_open_session", None)
        if open_row is not None:
            try:
                open_row([_row(session, None, None, None, 0)])
            except Exception:  # noqa: BLE001 - the end call writes the full row regardless
                log.warning("Could not write the open session row", exc_info=True)
        return SessionCreated(session_id=session.id, model_version=session.model_version)

    @api.post("/sessions/{session_id}/decide", response_model=DecideResponse)
    def decide(session_id: str, body: DecideRequest) -> DecideResponse:
        if human_only:
            raise HTTPException(403, "This app is for human players only")
        session = session_or_404(session_id)
        try:
            player = players.get(session.player_type)
        except PlayerUnavailable as exc:
            raise HTTPException(exc.status_code, exc.reason) from None

        started = time.perf_counter()
        try:
            if getattr(player, "tracks_sessions", False):
                decision = player.decide(body.state, body.screenshot, session_id=session.id)
            else:
                decision = player.decide(body.state, body.screenshot)
        except Exception as exc:  # a failed decision is data, not a crashed game
            log.exception("Player %s failed", session.player_type)
            decision = Decision(error=f"{type(exc).__name__}: {exc}"[:500])
        decision.latency_ms = int((time.perf_counter() - started) * 1000)
        session.count_decision(failed=decision.intent is None or decision.error is not None)

        row = DecisionRow(
            session_id=session.id,
            tic=body.state.tic,
            ts=datetime.now(UTC),
            player_type=session.player_type.value,
            intent=decision.intent.value if decision.intent else None,
            confidence=decision.confidence,
            threat_prob=decision.threat_prob,
            danger_score=decision.danger_score,
            latency_ms=decision.latency_ms,
            error=decision.error,
            raw_output=_json_text(decision.raw_output),
        )
        with session.lock:
            session.pending_decisions.append(row)
        return DecideResponse(**decision.model_dump(exclude={"raw_output"}))

    @api.post("/sessions/{session_id}/events", status_code=202)
    def events(session_id: str, body: EventsRequest) -> dict[str, int]:
        session = session_or_404(session_id)
        cap = limit["max_events_per_session"]
        if cap and session.events + len(body.events) > cap:
            raise HTTPException(429, "That is as many events as one game can send")
        now = datetime.now(UTC)
        rows = [
            EventRow(session_id=session.id, **{**e.model_dump(), "ts": e.ts or now})
            for e in body.events
        ]
        _flush(session, sink, events=rows)
        with session.lock:
            session.events += len(rows)
        return {"accepted": len(rows)}

    @api.post("/sessions/{session_id}/end")
    def end(session_id: str, body: EndRequest) -> dict[str, str]:
        try:
            session = store.get(session_id)
        except KeyError:
            # A retried end whose first reply was lost: the row is already written.
            earlier = store.ended_outcome(session_id)
            if earlier is None:
                raise HTTPException(404, "Unknown or finished session") from None
            return {"session_id": session_id, "outcome": earlier, "already_ended": "true"}
        _flush(session, sink)
        row = _row(session, body.outcome.value, body.duration_s, body.kills, body.decision_timeouts)
        try:
            sink.write_sessions([row])
        except Exception as exc:
            log.exception("Writing session row failed")
            raise HTTPException(502, f"Telemetry write failed: {exc}") from None
        store.remove(session.id, outcome=row.outcome)
        return {"session_id": session.id, "outcome": str(row.outcome)}

    app.include_router(api)

    static = Path(settings.static_dir) if settings.static_dir else None
    if static and static.is_dir():
        app.mount("/", StaticFiles(directory=static, html=True), name="web")
    return app


def _row(
    session, outcome: str | None, duration_s: float | None, kills: int | None, timeouts: int
) -> SessionRow:
    """The session row. With no outcome it is the open row written when the game starts."""
    closed = outcome is not None
    return SessionRow(
        session_id=session.id,
        player_type=session.player_type.value,
        mode=session.mode.value,
        engine=session.engine,
        level=session.level,
        seed=session.seed,
        model_version=session.model_version,
        started_at=session.started_at,
        ended_at=datetime.now(UTC) if closed else None,
        outcome=outcome,
        duration_s=duration_s,
        kills=kills,
        decisions=session.decisions if closed else None,
        decision_errors=session.decision_errors if closed else None,
        decision_timeouts=timeouts if closed else None,
    )


def _idle_seconds(session) -> float:
    return max(0.0, (session.last_seen_at - session.started_at).total_seconds())


def _json_text(value: object) -> str | None:
    import json

    if value is None:
        return None
    return value if isinstance(value, str) else json.dumps(value, default=str)


def _flush(session, sink: TelemetrySink, events: list[EventRow] | None = None) -> None:
    """Write events and any pending decisions. Decisions are dropped only after they land."""
    with session.lock:
        decisions = list(session.pending_decisions)
    try:
        if events:
            sink.write_events(events)
        if decisions:
            sink.write_decisions(decisions)
    except Exception as exc:
        log.exception("Telemetry write failed")
        raise HTTPException(502, f"Telemetry write failed: {exc}") from None
    if decisions:
        with session.lock:
            del session.pending_decisions[: len(decisions)]
