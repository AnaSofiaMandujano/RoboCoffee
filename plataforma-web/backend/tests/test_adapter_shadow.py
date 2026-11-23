"""Phase 5.2: AdapterFSM running live in SHADOW mode on real ingestion (no hardware)."""
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.config import Settings
from app.models import AdapterEvent, Order


class FakeRobotStatus:
    """Replaces RobotLink.status(): lets a test move the 'robot' between states."""
    def __init__(self):
        self.state, self.order = "ESPERANDO_ORDEN", None

    def __call__(self):
        return {"link": "connected", "state": self.state, "robot_order": self.order}


class SpyClient:
    """Stands in for ClienteRobot: any command that SENDS something fails the test."""
    conectado = True
    COMMANDS = {"enviar", "ordenar", "retirado", "cancelar", "pausa", "reanudar", "reset", "consultar_estado", "ping"}

    def __getattr__(self, name):
        if name in self.COMMANDS:
            raise AssertionError(f"shadow mode tried to send ClienteRobot.{name}()")
        raise AttributeError(name)

    async def cerrar(self):          # app shutdown closing the connection is fine
        pass


@pytest.fixture
def shadow(client):
    app = client.app
    robot = FakeRobotStatus()
    app.state.robot_link.status = robot
    app.state.robot_link.client = SpyClient()
    runner = app.state.adapter
    runner.fresh_max_age_s = 3600          # tests post 0.6 s-spaced readings faster than real time
    return client, robot, runner


class Feed:
    """Posts readings 0.6 s apart (timestamps slightly in the future, so robot changes made 'now'
    always come before the readings that follow them)."""
    def __init__(self, client):
        self.client, self.t = client, datetime.now(timezone.utc) + timedelta(seconds=2)

    def __call__(self, *grams):
        rs = []
        for g in grams:
            self.t += timedelta(seconds=0.6)
            rs.append({"raw": f"PESO:{g}", "grams": g, "received_at": self.t.isoformat()})
        r = self.client.post("/api/scale/readings", json={"source": "COM5", "readings": rs})
        assert r.status_code == 200, r.text


def _events(db, action=None):
    q = select(AdapterEvent).order_by(AdapterEvent.id)
    if action:
        q = q.where(AdapterEvent.action == action)
    return db.scalars(q).all()


def test_full_delivery_in_shadow_records_pickup_and_sends_nothing(shadow, db):
    client, robot, runner = shadow
    order = Order(product_code="C1", table_number=2, distance_m=2.0, status="DESPACHADA", robot_msg_id=17949107270)
    db.add(order)
    db.commit()
    feed = Feed(client)

    feed(0.0, 0.0, 8.4, 245.8, 349.4, 349.2, 349.2)                       # cup placed at base
    robot.state, robot.order = "RECORRIDO_MESA_2", 17949107270
    feed(349.3, 349.1, 349.4)
    robot.state = "EN_MESA"
    feed(349.3, 349.3, 349.2, 327.2, -40.2, 0.0, 0.0)                      # real removal shape

    confirmed = _events(db, "PICKUP_CONFIRMED")
    assert len(confirmed) == 1
    e = confirmed[0]
    assert (e.mode, e.robot_state, e.robot_msg_id, e.order_id) == ("shadow", "EN_MESA", 17949107270, order.id)
    assert [x.value for x in _events(db, "LOAD_WEIGHT")] == [pytest.approx(349.27, abs=0.1)]
    assert len(_events(db, "DELIVERY_WEIGHT")) == 1
    s = client.get("/api/adapter").json()
    assert s["mode"] == "shadow" and s["state"] == "PICKUP_CONFIRMED" and s["counters"]["pickups_confirmed"] == 1
    # SpyClient would have raised if anything had been sent to the robot

    # Phase 5.4: the measurements land on the order (a measurement, not a command)
    db.refresh(order)
    pending = [x for x in _events(db) if x.to_state == "PICKUP_PENDING" and not x.action][0]
    assert order.load_weight_g == pytest.approx(349.27, abs=0.1)
    assert order.delivery_weight_g == pytest.approx(349.27, abs=0.1)
    assert order.delivered_at == pending.at and pending.grams == -40.2   # HoraEntrega = first frame < 50 g
    assert (order.delivered_source, order.status) == ("scale", "ENTREGADA")


def test_transitions_are_recorded_in_order(shadow, db):
    client, robot, _ = shadow
    Feed(client)(0.0, 349.3, 349.3, 349.3)
    seq = [(e.from_state, e.to_state) for e in _events(db) if not e.action]
    assert seq == [("NO_DATA", "EMPTY"), ("EMPTY", "CUP_DETECTED"), ("CUP_DETECTED", "CUP_VERIFIED")]


def test_backlog_is_stored_but_not_fed(shadow, db):
    client, _, runner = shadow
    runner.fresh_max_age_s = 2.0
    old = datetime.now(timezone.utc) - timedelta(minutes=5)
    client.post("/api/scale/readings", json={"source": "COM5", "readings": [
        {"raw": "PESO:349.3", "grams": 349.3, "received_at": (old + timedelta(seconds=0.6 * i)).isoformat()}
        for i in range(5)]})
    assert runner.counters["readings_not_fed"] == 5 and runner.counters["readings_fed"] == 0
    assert _events(db) == []


def test_api_events_filter(shadow, db):
    client, robot, _ = shadow
    Feed(client)(0.0, 349.3, 349.3, 349.3)
    data = client.get("/api/adapter/events", params={"action": "LOAD_WEIGHT"}).json()
    assert len(data) == 1 and data[0]["action"] == "LOAD_WEIGHT" and data[0]["mode"] == "shadow"


def test_off_mode_records_nothing(client, db):
    client.app.state.adapter.mode = "off"
    Feed(client)(0.0, 349.3, 349.3, 349.3)
    assert _events(db) == [] and client.get("/api/adapter").json()["mode"] == "off"


def test_mode_on_is_not_accepted_yet():
    with pytest.raises(ValidationError):
        Settings(adapter_mode="on")


def test_admin_websocket_handshake_and_live_adapter_messages(shadow):
    client, robot, _ = shadow
    with client.websocket_connect("/api/ws/admin") as ws:
        first = [ws.receive_json() for _ in range(5)]
        assert first[4]["type"] == "adapter.status" and first[4]["data"]["mode"] == "shadow"
        Feed(client)(0.0, 349.3, 349.3, 349.3)
        seen = []
        for _ in range(15):
            m = ws.receive_json()
            if m["type"] == "adapter.event":
                seen.append(m["data"]["action"] or m["data"]["to_state"])
            if "LOAD_WEIGHT" in seen:
                break
        assert seen[:4] == ["EMPTY", "CUP_DETECTED", "CUP_VERIFIED", "LOAD_WEIGHT"]
