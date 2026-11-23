// Admin: live order list over WebSocket + robot status + Dispatch (sends ORDEN via ClienteRobot).
import { useEffect, useState } from "react";
import { clockMs, dispatchOrder, getJson, localTime, sendRetirado } from "../api/http";
import { useOrdersFeed } from "../api/ws";
import RobotPanel from "../components/RobotPanel";
import ScalePanel from "../components/ScalePanel";
import AdapterPanel from "../components/AdapterPanel";
import type { AdapterEventRow, Catalog, FeedStatus, Order, RobotStatus } from "../types";

const BADGE: Record<FeedStatus, { text: string; color: string }> = {
  connecting: { text: "Connecting…", color: "var(--muted)" },
  live: { text: "● Live", color: "var(--ok)" },
  reconnecting: { text: "● Reconnecting…", color: "var(--bad)" },
};

// Robot reason codes (fsm.py / ClienteRobot) -> short text for the operator
const REASONS: Record<string, string> = {
  OCUPADO: "robot is busy with another order",
  FUERA_DE_BASE: "robot is not at the base",
  SIN_ENLACE_ROBOT: "robot software has no link to the OAK9",
  BATERIA_BAJA: "battery too low",
  EN_ERROR: "robot is in ERROR (move it to the base and RESET)",
  SIN_RESPUESTA: "no answer — if the robot starts moving it will be confirmed, otherwise dispatch again",
};

type Note = { ok: boolean; text: string };

function waited(iso: string, now: number): string {
  const s = Math.max(0, Math.round((now - Date.parse(iso)) / 1000));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`;
}

function dispatchBlocker(o: Order, catalog: Catalog | null, robot: RobotStatus | null): string | null {
  if (o.dispatched_at) return "already dispatched";
  if (!catalog?.dispatch_from.includes(o.status)) return `status ${o.status}`;
  if (!robot || robot.link !== "connected") return "robot not connected";
  if (!robot.free) return `robot is ${robot.state ?? "busy"}`;
  return null;
}

const SOURCE: Record<string, string> = { scale: "scale", admin: "manual", robot: "robot" };

/** Trip timestamps and weights (Phase 5.4): the dashboard columns, as they fill in. */
function TripTimeline({ o }: { o: Order }) {
  const hm = (iso: string | null) => (iso ? localTime(iso).split(", ")[1] : null);
  const parts = [
    o.arrived_at && `Arrived ${hm(o.arrived_at)}`,
    o.delivered_at && `Delivered ${hm(o.delivered_at)} (${SOURCE[o.delivered_source ?? ""] ?? "?"})`,
    o.returned_at && `Back ${hm(o.returned_at)}`,
  ].filter(Boolean);
  const w = o.load_weight_g != null || o.delivery_weight_g != null
    ? `${o.load_weight_g?.toFixed(1) ?? "—"} → ${o.delivery_weight_g?.toFixed(1) ?? "—"} g` : null;
  return (
    <div className="trip" data-testid="trip-timeline">
      {parts.length > 0 && <div>{parts.join(" · ")}</div>}
      {(w || o.incident) && (
        <div>{w}{w && o.incident ? " · " : ""}
          {o.incident && <span className={o.incident === "Ninguna" ? "" : "note bad"}>Incident: {o.incident}</span>}
        </div>
      )}
    </div>
  );
}

export default function AdminPage() {
  const { orders, status, newIds, robot, scale, scaleReadings, adapter, adapterEvents, mergeAdapterEvents } = useOrdersFeed();
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [filter, setFilter] = useState<string>("ALL");
  const [now, setNow] = useState(Date.now());
  const [sending, setSending] = useState<number | null>(null);
  const [notes, setNotes] = useState<Record<number, Note>>({});
  const [retiradoNote, setRetiradoNote] = useState<Note | null>(null);

  async function retirado() {
    const trip = robot?.robot_order != null ? orders.find((o) => o.robot_msg_id === robot.robot_order) : undefined;
    if (!window.confirm(`Confirm the customer took the cup${trip ? ` (${trip.folio})` : ""}?\n` +
                        "This sends RETIRADO: the robot leaves the table and returns to base.")) return;
    const { status: code, body } = await sendRetirado().catch(() => ({ status: 0, body: null }));
    setRetiradoNote(code === 200
      ? { ok: true, text: `RETIRADO accepted: robot ${body.robot_state}` }
      : { ok: false, text: body?.reason ? `${body.reason}` : body?.detail ?? `request failed (${code || "network"})` });
  }

  useEffect(() => {
    getJson<Catalog>("/catalog").then(setCatalog).catch(() => setCatalog(null));
    // cup-detection history (shadow decisions) for markers and order rows
    getJson<AdapterEventRow[]>("/adapter/events?minutes=720&limit=2000").then(mergeAdapterEvents).catch(() => {});
    const t = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(t);
  }, []);

  async function dispatch(o: Order) {
    setSending(o.id);
    const { status: code, body } = await dispatchOrder(o.id).catch(() => ({ status: 0, body: null }));
    let note: Note;
    if (code === 200) note = { ok: true, text: `Sent · robot ${body.robot_state ?? ""}` };
    else if (body?.reason) note = { ok: false, text: `${body.reason}: ${REASONS[body.reason] ?? "rejected"}` };
    else note = { ok: false, text: body?.detail ?? `request failed (${code || "network"})` };
    setNotes((n) => ({ ...n, [o.id]: note }));
    setSending(null);
  }

  const productName = (code: string) => catalog?.products.find((p) => p.code === code)?.name ?? code;
  const shown = filter === "ALL" ? orders : orders.filter((o) => o.status === filter);
  const badge = BADGE[status];

  return (
    <main style={{ maxWidth: 1100 }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 16 }}>
        <h1>Orders</h1>
        <span style={{ color: badge.color, fontWeight: 600 }} data-testid="feed-status">{badge.text}</span>
      </div>
      <p className="sub">New orders appear automatically. Dispatch sends the order to the robot (ORDEN).</p>

      <RobotPanel robot={robot} orders={orders} now={now} onRetirado={retirado} retiradoNote={retiradoNote} />
      <AdapterPanel adapter={adapter} events={adapterEvents} orders={orders} now={now} />
      <ScalePanel scale={scale} live={scaleReadings} now={now} markers={adapterEvents} />

      <div style={{ display: "flex", gap: 12, alignItems: "center", marginBottom: 16 }}>
        <label htmlFor="filter" style={{ margin: 0 }}>Status</label>
        <select id="filter" value={filter} onChange={(e) => setFilter(e.target.value)}>
          <option value="ALL">All</option>
          {catalog?.statuses.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
        <span className="sub" style={{ margin: 0 }}>{shown.length} order(s)</span>
        <a href="/api/export/pedidos.csv" style={{ marginLeft: "auto" }} data-testid="export-csv">
          Download Pedidos.csv (dashboard)
        </a>
      </div>

      {shown.length === 0 ? (
        <p className="sub">{status === "live" ? "No orders." : "Waiting for the server…"}</p>
      ) : (
        <table className="data">
          <thead>
            <tr>
              <th>Folio</th><th>Product</th><th>Table</th><th>Name</th><th>Notes</th>
              <th>Status</th><th>Ordered at</th><th>Waiting</th><th>Robot</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((o) => {
              const blocker = dispatchBlocker(o, catalog, robot);
              const note = notes[o.id];
              return (
                <tr key={o.id} className={newIds.has(o.id) ? "new-row" : undefined}>
                  <td style={{ whiteSpace: "nowrap" }}><strong>{o.folio}</strong></td>
                  <td>{o.product_code} · {productName(o.product_code)}</td>
                  <td>{o.table_number}</td>
                  <td>{o.customer_name ?? "—"}</td>
                  <td>{o.notes ?? "—"}</td>
                  <td>{o.status}</td>
                  <td title={o.created_at}>{localTime(o.created_at)}</td>
                  <td>{o.dispatched_at ? "—" : waited(o.created_at, now)}</td>
                  <td style={{ minWidth: 190 }}>
                    {o.dispatched_at ? (
                      <>
                        <span className="note ok" title={o.dispatched_at}>Dispatched {localTime(o.dispatched_at).split(", ")[1]}</span>
                        <TripTimeline o={o} />
                      </>
                    ) : (
                      <button className="primary small" disabled={!!blocker || sending !== null}
                              title={blocker ?? "Send ORDEN to the robot"} onClick={() => dispatch(o)}>
                        {sending === o.id ? "Sending…" : "Dispatch"}
                      </button>
                    )}
                    {note && !o.dispatched_at && <div className={`note ${note.ok ? "ok" : "bad"}`}>{note.text}</div>}
                    {(() => {
                      const p = adapterEvents.find((e) => e.action === "PICKUP_CONFIRMED" &&
                        (e.order_id === o.id || (o.robot_msg_id != null && e.robot_msg_id === o.robot_msg_id)));
                      return p ? <div className="note shadow-note" data-testid="order-shadow-pickup"
                                      title="Recorded by the cup-detection state machine. Nothing was sent to the robot.">
                        Shadow pickup ✓ {clockMs(p.at)}</div> : null;
                    })()}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </main>
  );
}
