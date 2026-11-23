"""Offline replay of stored adapter readings through the AdapterFSM (no robot, nothing sent).

Bench policy: every cup that is verified on the adapter counts as a new delivery: the replay
robot "drives" to the table instantly (RECORRIDO_MESA_1 -> EN_MESA) and, once the FSM confirms
the pickup, "returns" to base. That turns each place/lift cycle on the bench into one delivery,
so you can count detections, misses and false confirmations on real logs.

Inside the backend container:
    python -m app.adapter.replay --source COM5 --minutes 60
    python -m app.adapter.replay --source COM5 --since "2026-11-17 14:00" --verbose
    python -m app.adapter.replay --file /code/scale_samples/bench.txt       # raw PESO lines, 0.6 s apart
"""
import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .fsm import CUP_VERIFIED, AdapterConfig, AdapterFSM, Event


@dataclass
class BenchResult:
    events: list[Event] = field(default_factory=list)
    confirmations: list[float] = field(default_factory=list)        # t of each PICKUP_CONFIRMED
    latencies: list[float] = field(default_factory=list)            # first frame < m_off -> confirmation
    placements: int = 0
    readings: int = 0

    def summary(self) -> str:
        kinds = {}
        for e in self.events:
            if e.action:
                kinds[e.action] = kinds.get(e.action, 0) + 1
        lat = (f"latency from first frame < m_off: min {min(self.latencies):.2f} s, "
               f"max {max(self.latencies):.2f} s") if self.latencies else "no confirmations"
        return (f"{self.readings} readings · {self.placements} cup placements (verified) · "
                f"{len(self.confirmations)} pickups confirmed · {lat} · actions {kinds}")


def run_bench(readings: list[tuple[float, float]], cfg: AdapterConfig | None = None) -> BenchResult:
    """readings: [(t_seconds, grams)] in time order."""
    fsm, res = AdapterFSM(cfg), BenchResult()
    order, first_off = 0, None
    fsm.on_robot(readings[0][0] if readings else 0.0, "ESPERANDO_ORDEN", None)
    for t, g in readings:
        res.readings += 1
        fsm.on_tick(t)
        evs = fsm.on_reading(t, g)
        if g < fsm.cfg.m_off and first_off is None and fsm.state in ("AT_TABLE", "PICKUP_PENDING"):
            first_off = t
        if g >= fsm.cfg.m_off:
            first_off = None if fsm.state != "PICKUP_PENDING" else first_off
        for e in evs:
            if e.to_state == CUP_VERIFIED and e.from_state != CUP_VERIFIED and not e.action:
                res.placements += 1
                order += 1
                evs += fsm.on_robot(t, "RECORRIDO_MESA_1", order) + fsm.on_robot(t, "EN_MESA", order)
                first_off = None
            if e.action == "PICKUP_CONFIRMED":
                res.confirmations.append(t)
                res.latencies.append(t - (first_off if first_off is not None else t))
                evs += fsm.on_robot(t, "REGRESO_BASE", order) + fsm.on_robot(t, "ESPERANDO_ORDEN", None)
                first_off = None
        res.events.extend(evs)
    return res


def _load_db(source: str, since: datetime, until: datetime) -> list[tuple[float, float]]:
    from sqlalchemy import select

    from ..db import SessionLocal
    from ..models import ScaleReading

    with SessionLocal() as db:
        rows = db.execute(select(ScaleReading.received_at, ScaleReading.grams)
                          .where(ScaleReading.source == source, ScaleReading.received_at >= since,
                                 ScaleReading.received_at <= until)
                          .order_by(ScaleReading.received_at, ScaleReading.id)).all()
    return [(r.received_at.timestamp(), r.grams) for r in rows]


def _load_file(path: str, period: float) -> list[tuple[float, float]]:
    out = []
    for i, line in enumerate(l for l in open(path, encoding="utf-8").read().split() if l.startswith("PESO:")):
        out.append((i * period, float(line.split(":", 1)[1])))
    return out


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default="COM5")
    ap.add_argument("--minutes", type=float, help="last N minutes (default: everything stored)")
    ap.add_argument("--since", help='local time "YYYY-MM-DD HH:MM" (this PC\'s time zone)')
    ap.add_argument("--until", help='local time "YYYY-MM-DD HH:MM"')
    ap.add_argument("--file", help="raw PESO: lines instead of the database")
    ap.add_argument("--period", type=float, default=0.6, help="seconds between lines in --file")
    ap.add_argument("--verbose", action="store_true", help="print every transition")
    a = ap.parse_args(argv)

    if a.file:
        readings = _load_file(a.file, a.period)
    else:
        now = datetime.now(timezone.utc)
        local = datetime.now().astimezone().tzinfo
        since = (now - timedelta(minutes=a.minutes) if a.minutes else
                 datetime.fromisoformat(a.since).replace(tzinfo=local) if a.since else datetime(2000, 1, 1, tzinfo=timezone.utc))
        until = datetime.fromisoformat(a.until).replace(tzinfo=local) if a.until else now
        readings = _load_db(a.source, since, until)
    res = run_bench(readings)
    t0 = readings[0][0] if readings else 0
    for e in res.events:
        if a.verbose or e.action in ("PICKUP_CONFIRMED", "PICKUP_BLOCKED", "INCIDENT"):
            when = (datetime.fromtimestamp(e.t).astimezone().strftime("%H:%M:%S.%f")[:-3]
                    if not a.file else f"{e.t - t0:7.1f} s")
            what = e.action or f"{e.from_state} -> {e.to_state}"
            print(f"{when}  {what:<34} {e.cause:<32} {'' if e.grams is None else f'{e.grams:.1f} g'}")
    print(res.summary())


if __name__ == "__main__":
    main()
