"""Runs the AdapterFSM live, in SHADOW mode (Phase 5.2).

Feeds the FSM with every stored adapter reading and with the robot state reported by
ClienteRobot, records every transition/action in adapter_events, and pushes them to admin
pages (WebSocket adapter.event / adapter.status).

SHADOW MODE SENDS NOTHING TO THE ROBOT. This module has no reference to ClienteRobot.retirado();
a PICKUP_CONFIRMED event is only recorded as "this is where RETIRADO would have been sent".
The settings don't even accept mode "on" yet.

Time: readings use the PC reception time (received_at); robot changes and ticks use this backend's
clock. Both are the same Windows PC (bridge-to-DB delay measured at ~5 ms). Readings older or newer
than adapter_fresh_max_age_s (backlog after a backend outage, or a clock problem) are stored by
the ingestion endpoint but NOT fed to the FSM: stale data must never drive a live decision.
"""
import asyncio
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..api.orders import order_json
from ..models import AdapterEvent, Order
from ..realtime.manager import admin_feed, publish_order
from ..services.trip import mark_delivered, set_delivery_weight, set_load_weight
from .fsm import AdapterConfig, AdapterFSM, Event

log = logging.getLogger("robocoffee.adapter")


class AdapterRunner:
    def __init__(self, mode: str, session_factory: Callable[[], Session], robot_link,
                 cfg: AdapterConfig | None = None, fresh_max_age_s: float = 2.0):
        self.mode = mode                                   # "off" | "shadow"
        self.session_factory = session_factory
        self.robot_link = robot_link
        self.fsm = AdapterFSM(cfg)
        self.fresh_max_age_s = fresh_max_age_s
        self.recent: deque[dict] = deque(maxlen=20)
        self.counters = {"readings_fed": 0, "readings_not_fed": 0, "events": 0,
                         "pickups_confirmed": 0, "pickups_blocked": 0, "incidents": 0}
        self._lock = asyncio.Lock()
        self._last_load_g: float | None = None      # last LOAD_WEIGHT (cup verified at base)
        self._pending_since: dict[int, datetime] = {}   # trip -> first frame < m_off of its window
        self._task: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        return self.mode == "shadow"

    # ------------------------------------------------------------ inputs
    def _robot(self, now: datetime) -> list[Event]:
        s = self.robot_link.status()
        state = s["state"] if s["link"] == "connected" else None      # link down -> unknown
        return self.fsm.on_robot(now.timestamp(), state, s["robot_order"])

    async def on_readings(self, readings) -> None:
        """readings: objects with .received_at (aware datetime) and .grams, already stored."""
        if not self.enabled:
            return
        async with self._lock:
            now = datetime.now(timezone.utc)
            events = self._robot(now)
            for r in sorted(readings, key=lambda x: x.received_at):
                if abs((now - r.received_at).total_seconds()) > self.fresh_max_age_s:
                    self.counters["readings_not_fed"] += 1
                    continue
                self.counters["readings_fed"] += 1
                events += self.fsm.on_reading(r.received_at.timestamp(), r.grams)
            await self._emit(events)

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            try:
                async with self._lock:
                    now = datetime.now(timezone.utc)
                    await self._emit(self._robot(now) + self.fsm.on_tick(now.timestamp()))
            except Exception:
                log.exception("adapter tick failed")

    # ------------------------------------------------------------ outputs
    async def _emit(self, events: list[Event]) -> None:
        if not events:
            return
        rows, changed_orders = await asyncio.to_thread(self._store, events)
        for o in changed_orders:
            await publish_order("order.updated", o)
        for row in rows:
            self.recent.appendleft(row)
            self.counters["events"] += 1
            a = row["action"]
            if a == "PICKUP_CONFIRMED":
                self.counters["pickups_confirmed"] += 1
                log.warning("[SHADOW] pickup confirmed: RETIRADO would be sent now (robot order %s)",
                            row["robot_msg_id"])
            elif a == "PICKUP_BLOCKED":
                self.counters["pickups_blocked"] += 1
            elif a == "INCIDENT":
                self.counters["incidents"] += 1
            await admin_feed.broadcast("adapter.event", row)
        await admin_feed.broadcast("adapter.status", self.status())

    def _store(self, events: list[Event]) -> tuple[list[dict], list[dict]]:
        with self.session_factory() as db:
            ids = {e.robot_order for e in events if e.robot_order is not None}
            orders = {o.robot_msg_id: o for o in
                      db.query(Order).filter(Order.robot_msg_id.in_(ids)).with_for_update().all()} if ids else {}
            rows = [AdapterEvent(at=datetime.fromtimestamp(e.t, timezone.utc), mode=self.mode,
                                 from_state=e.from_state, to_state=e.to_state, cause=e.cause[:80],
                                 action=e.action, value=e.value, grams=e.grams, robot_state=e.robot_state,
                                 robot_msg_id=e.robot_order,
                                 order_id=orders[e.robot_order].id if e.robot_order in orders else None)
                    for e in events]
            db.add_all(rows)
            changed = {}
            for e, row in zip(events, rows):
                if self._apply_to_order(db, e, row.at, orders.get(e.robot_order)):
                    changed[row.order_id] = orders[e.robot_order]
            db.commit()
            return [self.row_json(r) for r in rows], [order_json(o) for o in changed.values()]

    def _apply_to_order(self, db: Session, e: Event, at: datetime, order: Order | None) -> bool:
        """Phase 5.4: adapter measurements on the order (weights, and HoraEntrega from the scale).
        A measurement, not a command: it applies in shadow mode too; nothing is sent to the robot."""
        if e.action == "LOAD_WEIGHT":
            self._last_load_g = e.value
        elif not e.action and e.to_state == "EMPTY":
            self._last_load_g = None                      # that cup is gone
        if not e.action and e.to_state == "PICKUP_PENDING" and e.robot_order is not None:
            self._pending_since[e.robot_order] = at       # first frame below m_off of this window
        if order is None:
            return False
        if not e.action and e.to_state == "IN_TRANSIT" and e.from_state in ("CUP_VERIFIED", "CUP_DETECTED"):
            return set_load_weight(order, self._last_load_g)
        if e.action == "DELIVERY_WEIGHT":
            return set_delivery_weight(order, e.value)
        if e.action == "PICKUP_CONFIRMED":
            since = self._pending_since.pop(e.robot_order, at)
            return mark_delivered(db, order, since, "scale", f"{self.mode}_pickup")
        return False

    @staticmethod
    def row_json(r: AdapterEvent) -> dict:
        return {"id": r.id, "at": r.at.isoformat(), "mode": r.mode, "from_state": r.from_state,
                "to_state": r.to_state, "cause": r.cause, "action": r.action, "value": r.value,
                "grams": r.grams, "robot_state": r.robot_state, "robot_msg_id": r.robot_msg_id,
                "order_id": r.order_id}

    def status(self) -> dict:
        return {"mode": self.mode, **self.fsm.snapshot(), "counters": dict(self.counters),
                "config": asdict(self.fsm.cfg), "fresh_max_age_s": self.fresh_max_age_s,
                "recent_events": list(self.recent)[:10]}

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self.enabled:
            self._task = asyncio.create_task(self._tick(), name="adapter-tick")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
