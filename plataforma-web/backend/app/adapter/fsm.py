"""Adapter (cup) state machine: pure logic, no I/O, no clock of its own.

Same rules as the robot's fsm.py: ONE state variable, cambiar() is the only place it changes,
nothing blocks, every transition is recorded, failures are states. Each branch marked Ann is a
row of the transition table in docs/adapter_fsm.md.

Inputs (the caller provides the time t, in seconds):
  on_reading(t, grams)        every PESO:<g> from the adapter (t = PC reception time)
  on_robot(t, state, order)   robot FSM state + current order id from ClienteRobot
                              (state=None means "unknown": robot link down)
  on_tick(t)                  about once per second (silence has no event of its own)
Outputs: a list of Event per call. Actions are Events with `action` set:
  LOAD_WEIGHT       stable cup weight at base (value = grams)
  DELIVERY_WEIGHT   stable cup weight at the table, before pickup (value = grams)
  PICKUP_CONFIRMED  every safety condition held: this is where RETIRADO would be sent
  PICKUP_BLOCKED    the cup was removed but an interlock failed (reason in cause)
  INCIDENT          CUP_LOST_IN_TRANSIT / NO_CUP_AT_DISPATCH / NO_CUP_AT_TABLE
This module never talks to the robot. What the caller does with PICKUP_CONFIRMED depends on
the mode; in shadow mode (Phase 5.2) it is only recorded.

Thresholds come from the paper (m_on = 150 g, m_off = 50 g) and the real adapter log of
17 Nov: frames every ~0.6 s; 1-3 intermediate frames on placement (with bounces such as
50.1 -> 2.3 -> 109.4); on removal an intermediate value and/or one -17..-50 g undershoot frame.
"""
from collections import deque
from dataclasses import dataclass, field

# ------------------------------------------------------------------ states
NO_DATA = "NO_DATA"                    # no reading yet, or none for t_stale
SENSOR_FAULT = "SENSOR_FAULT"          # implausible value
EMPTY = "EMPTY"                        # no cup
CUP_DETECTED = "CUP_DETECTED"          # >= m_on, not stable yet
CUP_VERIFIED = "CUP_VERIFIED"          # stable cup (load weight recorded)
IN_TRANSIT = "IN_TRANSIT"              # robot carrying the cup to the table
AT_TABLE = "AT_TABLE"                  # robot EN_MESA, cup on the adapter
PICKUP_PENDING = "PICKUP_PENDING"      # cup off at the table, confirmation window running
PICKUP_CONFIRMED = "PICKUP_CONFIRMED"  # confirmed once for this trip (latched)
CUP_LOST = "CUP_LOST"                  # no cup during this trip: no automatic pickup

# robot FSM states (robocoffe_fsm/fsm.py)
R_BASE, R_TABLE = "ESPERANDO_ORDEN", "EN_MESA"


def _robot_trip_start(s):
    return s is not None and s.startswith("RECORRIDO_MESA")


@dataclass
class AdapterConfig:
    m_on: float = 150.0          # cup present (paper FR-01)
    m_off: float = 50.0          # cup absent (paper FR-02); hysteresis band 50-150 g
    stable_n: int = 3            # frames for a "stable" weight (~1.2 s)
    stable_band: float = 2.0     # max - min within those frames [g]
    min_frames: int = 3          # consecutive frames below m_off to confirm a pickup (~1.2 s at 0.6 s)...
    t_confirm: float = 1.1       # ...spanning at least this long. 1.1 not 1.2: real frame spacing
                                 # jitters (0.598-0.602 s measured), and 2 x 0.598 s < 1.2 s
    t_gap: float = 1.0           # a larger gap between frames restarts the window
    t_settle: float = 1.0        # no confirmation earlier than this after reaching the table
    t_stale: float = 3.0         # no reading for this long -> NO_DATA
    g_min: float = -100.0        # plausible range. NOT -20: removals undershoot to -50 g
    g_max: float = 1500.0
    recover_n: int = 3           # consecutive valid frames to leave SENSOR_FAULT


@dataclass
class Event:
    t: float
    from_state: str | None
    to_state: str
    cause: str
    grams: float | None = None
    robot_state: str | None = None
    robot_order: int | None = None
    action: str | None = None
    value: float | None = None


@dataclass
class _Window:
    since: float
    frames: int = 1


class AdapterFSM:
    def __init__(self, cfg: AdapterConfig | None = None):
        self.cfg = cfg or AdapterConfig()
        self.state = NO_DATA
        self.t_entry = 0.0
        self.last_t: float | None = None         # time of the last reading
        self.last_g: float | None = None
        self.recent: deque = deque(maxlen=self.cfg.stable_n)
        self.valid_run = 0
        self.robot_state: str | None = None
        self.robot_order: int | None = None
        self.trip_order: int | None = None        # order the robot is carrying this trip
        self.armed = False                         # cup seen on the adapter at the table
        self.delivery_recorded = False
        self.window: _Window | None = None         # confirmation window (cup off)
        self.table_since: float | None = None       # when the robot reached the table (t_settle)
        self.latched: set[int] = set()             # trips already confirmed
        self._out: list[Event] = []

    # ============================================================ cambiar()
    def cambiar(self, new: str, t: float, cause: str) -> None:
        """The ONLY place the state changes."""
        self._out.append(Event(t, self.state, new, cause, self.last_g, self.robot_state, self.robot_order))
        self.state, self.t_entry = new, t
        self.window = None                          # every state starts with a fresh window

    def _action(self, t: float, action: str, cause: str, value: float | None = None) -> None:
        self._out.append(Event(t, self.state, self.state, cause, self.last_g, self.robot_state,
                               self.robot_order, action, value))

    def _flush(self) -> list[Event]:
        out, self._out = self._out, []
        return out

    # ============================================================ helpers
    def _stable(self) -> float | None:
        """Mean of the last stable_n frames if they agree within stable_band, else None."""
        if len(self.recent) < self.cfg.stable_n:
            return None
        g = [x for _, x in self.recent]
        return sum(g) / len(g) if max(g) - min(g) <= self.cfg.stable_band else None

    def _rederive(self, t: float, cause: str) -> None:
        """After NO_DATA / SENSOR_FAULT / end of a trip: rebuild the state from the current weight
        and robot state. NEVER lands in PICKUP_PENDING or PICKUP_CONFIRMED (A16)."""
        g, c = self.last_g, self.cfg
        on_trip = self.trip_order is not None and self.trip_order not in self.latched
        if g is not None and g >= c.m_on:
            if self.robot_state == R_TABLE and on_trip:
                self.armed = True
                self.table_since = self.table_since if self.table_since is not None else t
                return self.cambiar(AT_TABLE, t, cause)
            if on_trip and self.robot_state not in (R_BASE, None):
                return self.cambiar(IN_TRANSIT, t, cause)
            return self.cambiar(CUP_DETECTED, t, cause)
        return self.cambiar(EMPTY, t, cause)

    # ============================================================ inputs
    def on_reading(self, t: float, grams: float) -> list[Event]:
        c = self.cfg
        gap = t - self.last_t if self.last_t is not None else None
        self.last_t, self.last_g = t, grams

        if not (c.g_min <= grams <= c.g_max):                                   # A15
            self.recent.clear()
            self.valid_run = 0
            if self.state != SENSOR_FAULT:
                self.cambiar(SENSOR_FAULT, t, f"implausible {grams:.1f} g")
            return self._flush()
        self.valid_run += 1
        self.recent.append((t, grams))

        if self.state == SENSOR_FAULT:
            if self.valid_run >= c.recover_n:
                self._rederive(t, "sensor_recovered")                          # A16
            return self._flush()
        if self.state == NO_DATA:
            self._rederive(t, "data_resumed")                                  # A16
            return self._flush()

        self._evaluate(t, grams, gap)
        return self._flush()

    def on_robot(self, t: float, state: str | None, order: int | None) -> list[Event]:
        prev = self.robot_state
        if state == prev and order == self.robot_order:
            return []
        self.robot_state, self.robot_order = state, order
        if state is None:                    # robot link down: unknown, not "left the table"
            return self._flush()

        if _robot_trip_start(state) and not _robot_trip_start(prev):
            self.trip_order, self.armed, self.delivery_recorded = order, False, False
            if self.state in (CUP_DETECTED, CUP_VERIFIED):
                self.cambiar(IN_TRANSIT, t, f"robot_left_base:{state}")             # A4
            elif self.state == EMPTY:
                self.cambiar(CUP_LOST, t, "no_cup_at_dispatch")                      # A6
                self._action(t, "INCIDENT", "NO_CUP_AT_DISPATCH")
        elif state == R_TABLE and prev != R_TABLE:
            self.table_since = t
            if self.state == IN_TRANSIT:
                self.armed = self.last_g is not None and self.last_g >= self.cfg.m_on
                self.recent.clear()            # delivery weight: only frames taken AT the table
                self.cambiar(AT_TABLE, t, "robot_at_table")                          # A7
        elif prev == R_TABLE and state != R_TABLE:
            if self.state in (AT_TABLE, PICKUP_PENDING):
                self._rederive(t, f"robot_left_table:{state}")                       # A12 (timeout/manual)
            elif self.state == PICKUP_CONFIRMED:
                self.cambiar(EMPTY, t, f"robot_left_table:{state}")                  # A13
        if state != R_TABLE:
            self.table_since = None
        if state == R_BASE:
            self.trip_order, self.armed = None, False
            if self.state in (IN_TRANSIT, AT_TABLE, PICKUP_PENDING, PICKUP_CONFIRMED, CUP_LOST):
                self._rederive(t, "robot_at_base")                                   # A14
        return self._flush()

    def on_tick(self, t: float) -> list[Event]:
        if (self.state != NO_DATA and self.last_t is not None
                and t - self.last_t > self.cfg.t_stale):
            self.recent.clear()
            self.cambiar(NO_DATA, t, f"no_reading_for_{t - self.last_t:.1f}s")      # A15
        return self._flush()

    # ============================================================ transitions
    def _evaluate(self, t: float, g: float, gap: float | None) -> None:
        c, s = self.cfg, self.state
        off, on = g < c.m_off, g >= c.m_on

        if s == EMPTY:
            if on:
                self.cambiar(CUP_DETECTED, t, f"weight_{g:.1f}g")                     # A1
        elif s in (CUP_DETECTED, CUP_VERIFIED):
            if off:
                self.cambiar(EMPTY, t, f"weight_{g:.1f}g")                            # A3
            elif s == CUP_DETECTED and (m := self._stable()) is not None and m >= c.m_on:
                self.cambiar(CUP_VERIFIED, t, "stable")                               # A2
                self._action(t, "LOAD_WEIGHT", "stable_at_base", round(m, 1))
        elif s == IN_TRANSIT:
            if self._window_step(t, off, gap):
                self.cambiar(CUP_LOST, t, "cup_off_in_transit")                        # A5
                self._action(t, "INCIDENT", "CUP_LOST_IN_TRANSIT")
        elif s == AT_TABLE:
            if on:
                self.armed = True
                if not self.delivery_recorded and (m := self._stable()) is not None and m >= c.m_on:
                    self.delivery_recorded = True
                    self._action(t, "DELIVERY_WEIGHT", "stable_at_table", round(m, 1))  # A8
            if off:
                if self.armed:
                    self.cambiar(PICKUP_PENDING, t, f"cup_off_{g:.1f}g")              # A9
                    self.window = _Window(t)
                elif self._window_step(t, True, gap):
                    self.cambiar(CUP_LOST, t, "no_cup_at_table")                        # A6b
                    self._action(t, "INCIDENT", "NO_CUP_AT_TABLE")
            else:
                self.window = None
        elif s == PICKUP_PENDING:
            if not off:
                self.cambiar(AT_TABLE, t, f"cup_back_{g:.1f}g")                       # A10
                return
            if gap is not None and gap > c.t_gap:
                self.window = _Window(t)                                                # window restarts
                self._action(t, "WINDOW_RESTART", f"gap_{gap:.1f}s")
                return
            self.window.frames += 1
            if (t - self.window.since >= c.t_confirm and self.window.frames >= c.min_frames
                    and self.table_since is not None and t - self.table_since >= c.t_settle):
                blocked = self._interlock_failure()
                if blocked:
                    self._action(t, "PICKUP_BLOCKED", blocked)                         # A11b
                    self._rederive(t, f"blocked:{blocked}")
                else:
                    self.latched.add(self.trip_order)
                    self.cambiar(PICKUP_CONFIRMED, t, f"cup_off_{t - self.window.since:.1f}s")   # A11
                    self._action(t, "PICKUP_CONFIRMED", "all_interlocks_ok")
        # PICKUP_CONFIRMED / CUP_LOST: wait for the robot (A13, A14)

    def _window_step(self, t: float, off: bool, gap: float | None) -> bool:
        """Generic 'cup off for t_confirm' window (transit, table without cup). True when confirmed."""
        if not off:
            self.window = None
            return False
        if self.window is None or (gap is not None and gap > self.cfg.t_gap):
            self.window = _Window(t)
            return False
        self.window.frames += 1
        return t - self.window.since >= self.cfg.t_confirm and self.window.frames >= self.cfg.min_frames

    def _interlock_failure(self) -> str | None:
        if self.robot_state != R_TABLE:
            return f"robot_not_at_table:{self.robot_state}"
        if self.trip_order is None or self.robot_order != self.trip_order:
            return f"order_mismatch:{self.robot_order}!={self.trip_order}"
        if self.trip_order in self.latched:
            return "already_confirmed_this_trip"
        if not self.armed:
            return "cup_never_seen_at_table"
        return None

    # ============================================================ view
    def snapshot(self) -> dict:
        return {"state": self.state, "armed": self.armed, "trip_order": self.trip_order,
                "robot_state": self.robot_state, "robot_order": self.robot_order,
                "last_grams": self.last_g, "window_frames": self.window.frames if self.window else 0}
