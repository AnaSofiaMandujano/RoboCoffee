"""Order trip lifecycle (Phase 5.4): fills the dashboard columns from robot + adapter events.

| Column (07_datos_dashboard) | Field              | Source                                                       |
|-----------------------------|--------------------|--------------------------------------------------------------|
| HoraInicioCarga             | dispatched_at      | ORDEN accepted (Phase 4)                                     |
| HoraLlegadaMesa             | arrived_at         | EVT LLEGO orden=<id>                                         |
| HoraEntrega                 | delivered_at       | FIRST of: adapter pickup (scale), manual Retirado (admin),   |
|                             | + delivered_source | robot REGRESO_BASE causa=vaso_retirado (robot)               |
| HoraRegresoBase             | returned_at        | EVT TRANSICION a=ESPERANDO_ORDEN orden=<id>                  |
| PesoCarga_g                 | load_weight_g      | adapter LOAD_WEIGHT when the trip starts                     |
| PesoEntrega_g               | delivery_weight_g  | adapter DELIVERY_WEIGHT at the table                         |
| Incidencia                  | incident           | computed when the robot is back at base (incident_for)       |

Every function is idempotent: the first value wins; later duplicates (e.g. the robot's own
vaso_retirado after a manual Retirado) change nothing.
"""
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AdapterEvent, Order, RobotEvent
from ..order_config import get_order_config
from .orders import record_event

NAVIGATION, CUP, NONE = "Navegación", "Vaso", "Ninguna"


def _status(db: Session, order: Order, new: str | None, cause: str, source: str, at: datetime) -> None:
    if new is not None and new != order.status:
        record_event(db, order, new, cause, source, at=at)


def mark_arrived(db: Session, order: Order, at: datetime) -> bool:
    if order.arrived_at is not None:
        return False
    order.arrived_at = at
    _status(db, order, get_order_config().arrived_status, "robot_at_table", "robot", at)
    return True


def mark_delivered(db: Session, order: Order, at: datetime, source: str, cause: str) -> bool:
    """source: scale | admin | robot. First one wins."""
    if order.delivered_at is not None:
        return False
    order.delivered_at, order.delivered_source = at, source
    new = get_order_config().delivered_status
    if new is not None and new != order.status:
        record_event(db, order, new, f"delivered:{source}:{cause}", source, at=at)
    else:                                   # keep an audit row even without a status change
        record_event(db, order, order.status, f"delivered:{source}:{cause}", source, at=at)
    return True


def set_load_weight(order: Order, grams: float) -> bool:
    if order.load_weight_g is not None or grams is None:
        return False
    order.load_weight_g = grams
    return True


def set_delivery_weight(order: Order, grams: float) -> bool:
    if order.delivery_weight_g is not None or grams is None:
        return False
    order.delivery_weight_g = grams
    return True


def incident_for(db: Session, order: Order) -> str:
    """Dashboard rules (07_datos_dashboard.md), adapted for the cup sensor:
    Navegación: robot ERROR, DETENIDO by ATASCO, or cancelled during the trip.
    Vaso:       the adapter reported the cup lost/missing, OR the robot timed out at the table
                (t_entrega) and no pickup was detected.
    Ninguna:    otherwise (incl. shadow mode: pickup detected by the scale, robot left by timeout).
    DETENIDO by OBSTACULO, ENLACE or PAUSA is not an incident."""
    mid = order.robot_msg_id
    evs = db.scalars(select(RobotEvent).where(RobotEvent.robot_msg_id == mid)).all() if mid else []
    for e in evs:
        f = e.fields or {}
        if e.event_type == "ERROR":
            return NAVIGATION
        if e.event_type == "TRANSICION" and (
                (f.get("a") == "DETENIDO" and str(f.get("causa", "")).startswith("ATASCO"))
                or f.get("causa") == "cancelada"):
            return NAVIGATION
    cup_incident = db.scalar(select(AdapterEvent.id).where(
        AdapterEvent.robot_msg_id == mid, AdapterEvent.action == "INCIDENT").limit(1)) if mid else None
    if cup_incident is not None:
        return CUP
    timed_out = any(e.event_type == "TRANSICION" and (e.fields or {}).get("a") == "REGRESO_BASE"
                    and (e.fields or {}).get("causa") == "t_entrega" for e in evs)
    if timed_out and order.delivered_at is None:
        return CUP
    return NONE


def mark_returned(db: Session, order: Order, at: datetime) -> bool:
    if order.returned_at is not None:
        return False
    db.flush()                               # make this event's own row visible to incident_for
    order.returned_at = at
    order.incident = incident_for(db, order)
    _status(db, order, get_order_config().returned_status, f"robot_at_base:{order.incident}", "robot", at)
    return True


def apply_robot_event(db: Session, order: Order, name: str, fields: dict, at: datetime,
                      manual_retirado_pending: bool = False) -> bool:
    """One EVT line from the robot, already linked to its order. Returns True if the order changed."""
    a, causa = fields.get("a", ""), fields.get("causa", "")
    if name == "LLEGO":
        return mark_arrived(db, order, at)
    if name == "TRANSICION" and a == "EN_MESA":
        return mark_arrived(db, order, at)               # same moment as LLEGO; whichever comes first
    if name == "TRANSICION" and a == "REGRESO_BASE" and causa == "vaso_retirado":
        return mark_delivered(db, order, at, "admin" if manual_retirado_pending else "robot", "vaso_retirado")
    if name == "TRANSICION" and a == "ESPERANDO_ORDEN":
        return mark_returned(db, order, at)
    return False
