"""FastAPI app. Run with: uvicorn pdoom.main:create_app --factory"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import Settings, get_settings
from .databricks_sql import SqlRunner
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


def build_sink(settings: Settings, sql: SqlRunner | None) -> TelemetrySink:
    if settings.sink == "sql":
        if sql is None:
            raise RuntimeError("PDOOM_SINK=sql needs PDOOM_WAREHOUSE_ID")
        return SqlSink(sql)
    if settings.sink == "jsonl":
        return JsonlSink(settings.jsonl_path)
    return MemorySink()


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
    players = players or build_registry(settings, sql)
    store = SessionStore(settings.model_version, settings.session_ttl_s)

    app = FastAPI(title="p(doom)", version=__version__)
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

    @api.get("/health")
    def health() -> dict[str, object]:
        return {
            "status": "ok",
            "version": __version__,
            "sink": settings.sink,
            "databricks_configured": sql is not None,
            "model_version": settings.model_version,
        }

    @api.get("/players")
    def list_players() -> list[dict[str, object]]:
        return players.describe()

    @api.post("/sessions", response_model=SessionCreated, status_code=201)
    def create_session(body: CreateSession) -> SessionCreated:
        if body.player_type != PlayerType.human:
            try:
                players.get(body.player_type)
            except PlayerUnavailable as exc:
                raise HTTPException(exc.status_code, exc.reason) from None
        session = store.create(body)
        return SessionCreated(session_id=session.id, model_version=session.model_version)

    @api.post("/sessions/{session_id}/decide", response_model=DecideResponse)
    def decide(session_id: str, body: DecideRequest) -> DecideResponse:
        session = session_or_404(session_id)
        try:
            player = players.get(session.player_type)
        except PlayerUnavailable as exc:
            raise HTTPException(exc.status_code, exc.reason) from None

        started = time.perf_counter()
        try:
            decision = player.decide(body.state, body.screenshot)
        except Exception as exc:  # a failed decision is data, not a crashed game
            log.exception("Player %s failed", session.player_type)
            decision = Decision(error=f"{type(exc).__name__}: {exc}"[:500])
        decision.latency_ms = int((time.perf_counter() - started) * 1000)

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
        now = datetime.now(UTC)
        rows = [
            EventRow(session_id=session.id, **{**e.model_dump(), "ts": e.ts or now})
            for e in body.events
        ]
        _flush(session, sink, events=rows)
        return {"accepted": len(rows)}

    @api.post("/sessions/{session_id}/end")
    def end(session_id: str, body: EndRequest) -> dict[str, str]:
        session = session_or_404(session_id)
        _flush(session, sink)
        ended = datetime.now(UTC)
        row = SessionRow(
            session_id=session.id,
            player_type=session.player_type.value,
            mode=session.mode.value,
            engine=session.engine,
            level=session.level,
            seed=session.seed,
            model_version=session.model_version,
            started_at=session.started_at,
            ended_at=ended,
            outcome=body.outcome.value,
            duration_s=body.duration_s,
            kills=body.kills,
        )
        try:
            sink.write_sessions([row])
        except Exception as exc:
            log.exception("Writing session row failed")
            raise HTTPException(502, f"Telemetry write failed: {exc}") from None
        store.remove(session.id)
        return {"session_id": session.id, "outcome": row.outcome}

    app.include_router(api)

    static = Path(settings.static_dir) if settings.static_dir else None
    if static and static.is_dir():
        app.mount("/", StaticFiles(directory=static, html=True), name="web")
    return app


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
