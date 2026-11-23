# Trip lifecycle and dashboard export (Phase 5.4)

`backend/app/services/trip.py` fills the dashboard columns (`07_datos_dashboard.md`) from robot and adapter
events. Every value is set once: the first source wins and later duplicates change nothing.

| Dashboard column | Order field | Source |
|---|---|---|
| HoraPedido | `created_at` | order created |
| HoraInicioCarga | `dispatched_at` | robot accepted `ORDEN` |
| HoraLlegadaMesa *(extra)* | `arrived_at` | `EVT LLEGO` / `TRANSICION a=EN_MESA` |
| HoraEntrega | `delivered_at` + `delivered_source` | first of: **scale** (adapter pickup; time = first frame < 50 g of the confirmed window) · **admin** (manual Retirado button) · **robot** (`REGRESO_BASE causa=vaso_retirado`) |
| HoraRegresoBase | `returned_at` | `TRANSICION a=ESPERANDO_ORDEN` |
| PesoCarga_g | `load_weight_g` | adapter `LOAD_WEIGHT` when the trip starts |
| PesoEntrega_g | `delivery_weight_g` | adapter `DELIVERY_WEIGHT` (3 stable frames at the table) |
| Incidencia | `incident` | computed when the robot is back at base |
| HoraListo | `ready_at` | not set yet (no barista "ready" step) |

**The scale sets HoraEntrega in shadow mode too.** It is a measurement of when the customer took the cup,
not a command; nothing is sent to the robot.

## Incident rules

| Value | When |
|---|---|
| `Navegación` | robot `ERROR`, `DETENIDO` by `ATASCO`, or the order was cancelled |
| `Vaso` | the adapter reported the cup lost/missing, **or** the robot timed out at the table (`t_entrega`) and no pickup was detected |
| `Ninguna` | otherwise, including shadow mode: the scale saw the pickup but the robot left by timeout |

`DETENIDO` by `OBSTACULO`, `ENLACE` or `PAUSA` is not an incident.

## Order statuses (configurable, still provisional)

`order_config.json`: `arrived_status` (`EN_MESA`), `delivered_status` (`ENTREGADA`), `returned_status`
(`null` = keep). Rename or set to `null` without a migration.

## Manual Retirado

`POST /api/robot/retirado` → `ClienteRobot.retirado()` → `RETIRADO id=<n>`, the validated path.
The admin page shows the button only while the robot is `EN_MESA` and asks for confirmation. The backend refuses
(409) unless the robot is `EN_MESA`, and the robot checks again. This is the **only** command in Phase 5 that is
sent to the robot; the adapter state machine is still in shadow mode.

## Export

`GET /api/export/pedidos.csv` (button *Download Pedidos.csv* on `/admin`): UTF-8 with BOM, times
`YYYY-MM-DD HH:MM:SS` in the computer's local time (`EXPORT_TZ`, default `America/Mexico_City`), dot decimals.
Columns in the order of `07_datos_dashboard.md`, then two extra columns at the end: `HoraLlegadaMesa`, `FuenteEntrega`.
`?dispatched_only=false` includes orders never sent to the robot.
**Check the column order against `COL_PEDIDOS` in `registro_pruebas.py`**, which I don't have.
