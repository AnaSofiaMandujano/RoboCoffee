"""Pedidos.csv for the Industrial Engineering dashboard (07_datos_dashboard.md).

Format required there: times as "2026-11-12 10:41:20" in the computer's local time, decimals with a
dot, UTF-8 with BOM (Excel keeps the accents). Column names are the exact ones the dashboard reads;
two extra columns go at the END: HoraLlegadaMesa (suggested in 07) and FuenteEntrega (scale/admin/robot).
HoraListo stays empty: the barista "ready" step doesn't exist in the platform yet.
"""
import csv
import io
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from sqlalchemy import select

from ..config import settings
from ..db import get_session_factory
from ..models import Order

router = APIRouter(tags=["export"])

COLUMNS = ["Folio", "HoraPedido", "Producto", "Mesa", "Distancia_m", "HoraListo", "HoraInicioCarga",
           "HoraEntrega", "HoraRegresoBase", "Incidencia", "PesoCarga_g", "PesoEntrega_g",
           "HoraLlegadaMesa", "FuenteEntrega"]


def _t(dt: datetime | None, tz: ZoneInfo) -> str:
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S") if dt else ""


def _g(x: float | None, nd: int = 1) -> str:
    return f"{x:.{nd}f}" if x is not None else ""


def pedidos_csv(orders: list[Order], tz: ZoneInfo) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(COLUMNS)
    for o in orders:
        w.writerow([o.folio, _t(o.created_at, tz), o.product_code, o.table_number, _g(o.distance_m, 1),
                    _t(o.ready_at, tz), _t(o.dispatched_at, tz), _t(o.delivered_at, tz),
                    _t(o.returned_at, tz), o.incident or "", _g(o.load_weight_g), _g(o.delivery_weight_g),
                    _t(o.arrived_at, tz), o.delivered_source or ""])
    return "﻿" + buf.getvalue()


@router.get("/export/pedidos.csv")
async def export_pedidos(dispatched_only: bool = Query(default=True, description="only orders sent to the robot"),
                         session_factory=Depends(get_session_factory)):
    tz = ZoneInfo(settings.export_tz)

    def build():
        with session_factory() as db:
            stmt = select(Order).order_by(Order.id)
            if dispatched_only:
                stmt = stmt.where(Order.dispatched_at.is_not(None))
            return pedidos_csv(list(db.scalars(stmt)), tz)

    body = await run_in_threadpool(build)
    name = f"Pedidos_{datetime.now(tz).strftime('%Y%m%d_%H%M')}.csv"
    return Response(content=body.encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})
