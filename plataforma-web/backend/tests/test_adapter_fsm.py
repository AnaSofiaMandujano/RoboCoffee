"""AdapterFSM scenarios (pure logic, synthetic time). The dangerous ones are marked SAFETY:
a false PICKUP_CONFIRMED would make the robot leave with the cup still on it."""
from pathlib import Path

import pytest

from app.adapter import fsm as F
from app.adapter.replay import run_bench

P = 0.6   # adapter frame period


class Run:
    """Drives the FSM like the live runner: readings every 0.6 s, robot changes, ticks."""
    def __init__(self, cfg=None):
        self.fsm, self.t, self.events = F.AdapterFSM(cfg), 0.0, []

    def w(self, *grams, period=P):
        for g in grams:
            self.t += period
            self.events += self.fsm.on_tick(self.t)
            self.events += self.fsm.on_reading(self.t, g)
        return self

    def robot(self, state, order=None):
        self.events += self.fsm.on_robot(self.t, state, order)
        return self

    def silence(self, seconds):
        end = self.t + seconds
        while self.t < end:
            self.t += 1.0
            self.events += self.fsm.on_tick(self.t)
        return self

    def actions(self, kind=None):
        return [e for e in self.events if e.action and (kind is None or e.action == kind)]

    @property
    def state(self):
        return self.fsm.state

    def at_table_with_cup(self, order=17):
        """Base -> cup placed -> verified -> trip -> table (cup on, settled)."""
        self.robot("ESPERANDO_ORDEN").w(0.0, 0.0, 8.4, 245.8, 349.4, 349.2, 349.2)
        assert self.state == F.CUP_VERIFIED
        self.robot(f"RECORRIDO_MESA_2", order).w(349.3, 349.1, 349.4, 349.2).robot("EN_MESA", order)
        assert self.state == F.AT_TABLE
        return self.w(349.3, 349.3, 349.2)


# ---------------------------------------------------------------- normal path
def test_normal_delivery_and_pickup():
    r = Run().at_table_with_cup()
    assert [a.value for a in r.actions("LOAD_WEIGHT")] == [pytest.approx(349.3, abs=0.2)]
    assert len(r.actions("DELIVERY_WEIGHT")) == 1
    r.w(327.2, -40.2, 0.0)                       # real removal shape: intermediate, undershoot, 0
    assert r.state == F.PICKUP_PENDING and not r.actions("PICKUP_CONFIRMED")
    r.w(0.0)
    assert r.state == F.PICKUP_CONFIRMED and len(r.actions("PICKUP_CONFIRMED")) == 1
    r.robot("REGRESO_BASE", 17).robot("ESPERANDO_ORDEN")
    assert r.state == F.EMPTY


def test_delivery_weight_uses_only_frames_at_the_table():
    r = Run().robot("ESPERANDO_ORDEN").w(349.3, 349.3, 349.3).robot("RECORRIDO_MESA_1", 2).w(349.3, 349.3)
    r.robot("EN_MESA", 2).w(349.1, 349.0)
    assert not r.actions("DELIVERY_WEIGHT")           # only 2 frames at the table so far
    r.w(349.0)
    assert [a.value for a in r.actions("DELIVERY_WEIGHT")] == [349.0]           # mean of 349.1, 349.0, 349.0, rounded to 0.1 g


def test_placement_bounce_from_real_log_is_one_cup():
    r = Run().robot("ESPERANDO_ORDEN").w(0.0, 50.1, 2.3, 109.4, 349.5, 349.2, 349.2)
    states = [e.to_state for e in r.events if not e.action]
    assert states.count(F.CUP_DETECTED) == 1 and r.state == F.CUP_VERIFIED


def test_quick_grab_on_arrival_is_still_detected():
    """Customer takes the cup the moment the robot stops: cup was on board at arrival -> armed."""
    r = Run().robot("ESPERANDO_ORDEN").w(349.3, 349.3, 349.3)
    r.robot("RECORRIDO_MESA_1", 5).w(349.3, 349.2).robot("EN_MESA", 5)
    r.w(0.0, 0.0, 0.0)
    assert len(r.actions("PICKUP_CONFIRMED")) == 1


# ---------------------------------------------------------------- SAFETY: no false confirmation
def test_SAFETY_lift_and_put_back_is_not_a_pickup():
    r = Run().at_table_with_cup().w(170.0, 0.0, 0.0, 349.1, 349.3)     # off for 2 frames only
    assert not r.actions("PICKUP_CONFIRMED") and r.state == F.AT_TABLE


def test_SAFETY_single_glitch_frame_is_not_a_pickup():
    r = Run().at_table_with_cup().w(0.0, 349.3, 349.3, 0.0, 349.2)
    assert not r.actions("PICKUP_CONFIRMED")


def test_SAFETY_partial_lift_between_thresholds_resets_the_window():
    r = Run().at_table_with_cup().w(0.0, 0.0, 80.0, 0.0, 0.0)          # 80 g: not "off"
    assert not r.actions("PICKUP_CONFIRMED") and r.state == F.PICKUP_PENDING


def test_SAFETY_bluetooth_gap_during_pickup_restarts_the_window():
    r = Run().at_table_with_cup().w(0.0)
    r.w(0.0, period=2.0)                        # 2 s without frames inside the window
    assert not r.actions("PICKUP_CONFIRMED") and r.actions("WINDOW_RESTART")
    r.w(0.0, 0.0)                               # needs 3 fresh frames after the gap
    assert len(r.actions("PICKUP_CONFIRMED")) == 1


def test_SAFETY_silence_is_never_a_pickup():
    r = Run().at_table_with_cup().w(0.0)
    r.silence(5)
    assert r.state == F.NO_DATA and not r.actions("PICKUP_CONFIRMED")
    r.w(0.0)                                    # data comes back already at 0 g
    assert r.state == F.EMPTY and not r.actions("PICKUP_CONFIRMED")   # never re-derived into a pickup


def test_SAFETY_robot_not_at_table_blocks_confirmation():
    r = Run().at_table_with_cup()
    r.robot(None)                               # robot link lost: state unknown
    r.w(0.0, 0.0, 0.0)
    blocked = r.actions("PICKUP_BLOCKED")
    assert not r.actions("PICKUP_CONFIRMED") and "robot_not_at_table" in blocked[0].cause


def test_SAFETY_order_mismatch_blocks_confirmation():
    r = Run().at_table_with_cup(order=17)
    r.robot("EN_MESA", 99)                      # robot now reports a different order
    r.w(0.0, 0.0, 0.0)
    assert not r.actions("PICKUP_CONFIRMED") and "order_mismatch" in r.actions("PICKUP_BLOCKED")[0].cause


def test_SAFETY_confirmed_once_per_trip():
    r = Run().at_table_with_cup().w(0.0, 0.0, 0.0)
    r.w(349.3, 349.3, 0.0, 0.0, 0.0, 0.0)       # cup put back and taken again, same trip
    assert len(r.actions("PICKUP_CONFIRMED")) == 1


def test_SAFETY_no_confirmation_before_settling_at_table():
    cfg = F.AdapterConfig(t_settle=5.0)
    r = Run(cfg).robot("ESPERANDO_ORDEN").w(349.3, 349.3, 349.3)
    r.robot("RECORRIDO_MESA_1", 1).robot("EN_MESA", 1).w(0.0, 0.0, 0.0)
    assert not r.actions("PICKUP_CONFIRMED")
    r.w(*[0.0] * 6)                              # 9 frames = 5.4 s at the table > t_settle
    assert len(r.actions("PICKUP_CONFIRMED")) == 1


def test_SAFETY_implausible_value_is_a_sensor_fault_not_a_pickup():
    r = Run().at_table_with_cup().w(-500.0)
    assert r.state == F.SENSOR_FAULT
    r.w(0.0, 0.0, 0.0)                          # 3 valid frames to recover, then re-derive
    assert r.state == F.EMPTY and not r.actions("PICKUP_CONFIRMED")


def test_undershoot_frames_are_normal_not_faults():
    r = Run().at_table_with_cup().w(-50.0, 0.0, 0.0)
    assert len(r.actions("PICKUP_CONFIRMED")) == 1
    assert not any(e.to_state == F.SENSOR_FAULT for e in r.events)


# ---------------------------------------------------------------- incidents and robot paths
def test_cup_lost_in_transit():
    r = Run().robot("ESPERANDO_ORDEN").w(349.3, 349.3, 349.3).robot("RECORRIDO_MESA_3", 8)
    r.w(349.0, -30.0, 0.0, 0.0)
    assert r.state == F.CUP_LOST and r.actions("INCIDENT")[0].cause == "CUP_LOST_IN_TRANSIT"
    r.robot("EN_MESA", 8).w(0.0, 0.0, 0.0, 0.0)
    assert not r.actions("PICKUP_CONFIRMED")


def test_dispatched_without_cup():
    r = Run().robot("ESPERANDO_ORDEN").w(0.0, 0.0, 0.0).robot("RECORRIDO_MESA_1", 3)
    assert r.state == F.CUP_LOST and r.actions("INCIDENT")[0].cause == "NO_CUP_AT_DISPATCH"


def test_timeout_at_table_robot_returns_with_cup():
    r = Run().at_table_with_cup(order=4)
    r.robot("REGRESO_BASE", 4)                  # t_entrega expired, nobody took it
    assert r.state == F.IN_TRANSIT and not r.actions("PICKUP_CONFIRMED")
    r.w(349.2, 349.3).robot("ESPERANDO_ORDEN")
    assert r.state == F.CUP_DETECTED


def test_detenido_during_trip_keeps_transit():
    r = Run().robot("ESPERANDO_ORDEN").w(349.3, 349.3, 349.3).robot("RECORRIDO_MESA_2", 6)
    r.robot("DETENIDO", 6).w(349.2).robot("RECORRIDO_MESA_2", 6)
    assert r.state == F.IN_TRANSIT


def test_every_change_is_recorded_with_robot_context():
    r = Run().at_table_with_cup(order=17)
    e = [x for x in r.events if x.to_state == F.AT_TABLE][0]
    assert (e.from_state, e.cause, e.robot_state, e.robot_order) == (F.IN_TRANSIT, "robot_at_table", "EN_MESA", 17)


# ---------------------------------------------------------------- real data
SAMPLE = next((p for p in (Path(__file__).resolve().parents[2] / "scale_bridge" / "samples" / "bench_2026-11-17.txt",
                           Path("/code/scale_samples/bench_2026-11-17.txt")) if p.exists()), None)


@pytest.mark.skipif(SAMPLE is None, reason="scale_bridge/samples not available")
def test_real_bench_log_5_cups_5_pickups_no_false_confirmation():
    lines = [l for l in SAMPLE.read_text().split() if l.startswith("PESO:")]
    res = run_bench([(i * P, float(l.split(":")[1])) for i, l in enumerate(lines)])
    assert res.placements == 5 and len(res.confirmations) == 5
    assert all(lat == pytest.approx(1.2, abs=0.05) for lat in res.latencies)
    assert not [e for e in res.events if e.action in ("PICKUP_BLOCKED", "INCIDENT")]
