"""AdapterFSM in shadow mode (Phase 5.2): state and recorded decisions. Read-only."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select

from ..adapter.runner import AdapterRunner
from ..db import get_session_factory
from ..models import AdapterEvent

router = APIRouter(tags=["adapter"])


@router.get("/adapter")
def adapter_status(request: Request):
    """Mode (shadow/off), current FSM state, trip, counters, thresholds and the last 10 events."""
    return request.app.state.adapter.status()


@router.get("/adapter/events")
async def adapter_events(minutes: float = Query(default=60, gt=0, le=7 * 24 * 60),
                         action: str | None = Query(default=None, description="e.g. PICKUP_CONFIRMED"),
                         limit: int = Query(default=200, ge=1, le=5000),
                         session_factory=Depends(get_session_factory)):
    """Recorded transitions/actions, newest first."""
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes)

    def query():
        with session_factory() as db:
            stmt = (select(AdapterEvent).where(AdapterEvent.at >= since)
                    .order_by(AdapterEvent.at.desc(), AdapterEvent.id.desc()).limit(limit))
            if action:
                stmt = stmt.where(AdapterEvent.action == action)
            return [AdapterRunner.row_json(r) for r in db.scalars(stmt)]

    return await run_in_threadpool(query)
