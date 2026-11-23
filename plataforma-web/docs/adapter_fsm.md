# Adapter state machine (Phase 5.2, shadow mode)

`backend/app/adapter/fsm.py`. It is pure logic, written to the same rules as the robot's `fsm.py`: one state variable,
`cambiar()` is the only place it changes, nothing blocks, every transition is recorded, and failures are states.

**Shadow mode:** the FSM runs live and records every decision in `adapter_events`, but **sends nothing
to the robot**. No code in 5.2 calls `ClienteRobot.retirado()`, and the setting `ADAPTER_MODE` accepts only
`shadow` (default) or `off`. The robot still returns by its own `t_entrega`.

## Inputs

| Input | From | Time used |
|---|---|---|
| Each `PESO:<g>` reading | `scale_bridge` → `POST /api/scale/readings` (after storage) | PC reception time `received_at` |
| Robot state + current order | `ClienteRobot` (`GET /api/robot`), checked on every batch and every 1 s | backend time |
| Tick | every 1 s | backend time |

The only adapter input is the weight stream. Non-`PESO` lines never reach the backend.
Readings older or newer than **2 s** (backlog after a backend outage, clock problems) are stored but
**not** fed to the FSM. Robot link down means the robot state is **unknown**, not "left the table".

## States

| State | Meaning |
|---|---|
| `NO_DATA` | no reading yet, or none for 3 s |
| `SENSOR_FAULT` | value outside −100 … 1500 g |
| `EMPTY` | no cup |
| `CUP_DETECTED` | ≥ 150 g, not stable yet |
| `CUP_VERIFIED` | 3 frames within ±2 g (load weight recorded) |
| `IN_TRANSIT` | robot `RECORRIDO_MESA_n` (or returning with the cup) |
| `AT_TABLE` | robot `EN_MESA`, cup on the adapter |
| `PICKUP_PENDING` | cup off at the table, confirmation window running |
| `PICKUP_CONFIRMED` | confirmed once for this trip: **this is where `RETIRADO` would be sent** |
| `CUP_LOST` | no cup during this trip: no automatic pickup (incident) |

## Transitions

| # | From | Condition | Action | To |
|---|---|---|---|---|
| A1 | EMPTY | g ≥ 150 | | CUP_DETECTED |
| A2 | CUP_DETECTED | last 3 frames within 2 g, mean ≥ 150 | `LOAD_WEIGHT` | CUP_VERIFIED |
| A3 | CUP_DETECTED / CUP_VERIFIED | g < 50 | | EMPTY |
| A4 | CUP_DETECTED / CUP_VERIFIED | robot → `RECORRIDO_MESA_n` | remember the trip's order id | IN_TRANSIT |
| A5 | IN_TRANSIT | g < 50 for 3 frames / ≥ 1.1 s | `INCIDENT CUP_LOST_IN_TRANSIT` | CUP_LOST |
| A6 | EMPTY | robot → `RECORRIDO_MESA_n` | `INCIDENT NO_CUP_AT_DISPATCH` | CUP_LOST |
| A6b | AT_TABLE, cup never seen there | g < 50 for 3 frames | `INCIDENT NO_CUP_AT_TABLE` | CUP_LOST |
| A7 | IN_TRANSIT | robot → `EN_MESA` | armed if the cup is on board | AT_TABLE |
| A8 | AT_TABLE | 3 stable frames **at the table** | `DELIVERY_WEIGHT` | AT_TABLE |
| A9 | AT_TABLE, armed | g < 50 | start window | PICKUP_PENDING |
| A10 | PICKUP_PENDING | any frame ≥ 50 (cup back / partial lift) | | AT_TABLE |
| A11 | PICKUP_PENDING | 3 frames < 50 spanning ≥ 1.1 s, ≥ 1 s at the table, **interlocks OK** | `PICKUP_CONFIRMED` | PICKUP_CONFIRMED |
| A11b | PICKUP_PENDING | same, but an interlock fails | `PICKUP_BLOCKED <reason>` | re-derived |
| A12 | AT_TABLE / PICKUP_PENDING | robot leaves `EN_MESA` (timeout, manual) | | re-derived |
| A13 | PICKUP_CONFIRMED | robot leaves `EN_MESA` | | EMPTY |
| A14 | trip states | robot → `ESPERANDO_ORDEN` | trip ends | re-derived |
| A15 | any | no reading for 3 s / implausible value | | NO_DATA / SENSOR_FAULT |
| A16 | NO_DATA / SENSOR_FAULT | data back (3 valid frames after a fault) | | re-derived |

**Re-derived** means the state is rebuilt from the current weight and robot state. It **never** lands in
`PICKUP_PENDING` or `PICKUP_CONFIRMED`: a pickup always needs a fresh, uninterrupted window.

## Interlocks for A11 (all must hold)

1. 3 consecutive frames < 50 g spanning ≥ 1.1 s (3 frames at 0.6 s; 1.1 not 1.2 because real spacing jitters 0.598–0.602 s).
2. No gap > 1 s between them. A gap **restarts** the window (`WINDOW_RESTART`).
3. Robot state `EN_MESA` (unknown = blocked).
4. The robot's current order = the order of this trip.
5. The cup was seen on the adapter at the table (armed).
6. ≥ 1 s since the robot reached the table.
7. Not already confirmed in this trip (latched).

**Removal shape on the real adapter** (17 Nov): 349 g → intermediate value → one −17 to −50 g frame → 0 g.
The undershoot frame counts as "off". It is not a fault, since the plausible range starts at −100 g.

**Measured timing:** confirmation comes 1.2 s after the first frame below 50 g. From the physical lift that's
about 1.8–2.4 s, because the first frame after a lift is usually an intermediate value (≥ 50 g).

## Admin page (5.3)

`/admin` shows, live:
- **Cup detection** panel: the **SHADOW** badge (nothing is sent to the robot), the state as steps
  (No cup → Cup on adapter → In transit → At table → Pickup pending → Pickup confirmed), problem states
  (`NO_DATA`, `SENSOR_FAULT`, `CUP_LOST`) in red, the trip's folio, load and delivery weights, counters, and
  the last 12 decisions.
- After a confirmation: *"Pickup confirmed at HH:MM:SS.mmm for P-00xx: this is when `RETIRADO` would have
  been sent. Not sent (shadow mode)."*
- Weight chart markers: dashed line = pickup pending started, solid line = pickup confirmed.
- Order rows: *Shadow pickup ✓ HH:MM:SS.mmm* under the dispatch time.

Everything is restored after a page reload (`adapter.status` in the WebSocket handshake + `GET /api/adapter/events`).

## Validate in shadow mode

```bat
:: what the FSM is doing now
curl http://localhost:8000/api/adapter
:: what it decided (newest first)
curl "http://localhost:8000/api/adapter/events?minutes=30"
curl "http://localhost:8000/api/adapter/events?minutes=600&action=PICKUP_CONFIRMED"

:: replay stored real readings offline (bench policy: each verified cup = one delivery)
docker compose exec backend python -m app.adapter.replay --source COM5 --minutes 120
docker compose exec backend python -m app.adapter.replay --source COM5 --since "2026-11-17 14:00" --verbose
```

Before any "on" mode: the shadow log must show **one `PICKUP_CONFIRMED` per real pickup and none otherwise**
(touches, taps, Bluetooth drops, transit) over the bench and robot runs.
