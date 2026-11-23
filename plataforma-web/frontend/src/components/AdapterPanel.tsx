// Cup detection (AdapterFSM) live view, 5.3. Display only: in shadow mode NOTHING is sent to the robot.
import { clockMs } from "../api/http";
import type { AdapterEventRow, AdapterStatus, Order } from "../types";

const STEPS: { states: string[]; label: string }[] = [
  { states: ["EMPTY"], label: "No cup" },
  { states: ["CUP_DETECTED", "CUP_VERIFIED"], label: "Cup on adapter" },
  { states: ["IN_TRANSIT"], label: "In transit" },
  { states: ["AT_TABLE"], label: "At table" },
  { states: ["PICKUP_PENDING"], label: "Pickup pending" },
  { states: ["PICKUP_CONFIRMED"], label: "Pickup confirmed" },
];
const PROBLEMS: Record<string, string> = {
  NO_DATA: "No adapter data: decisions paused",
  SENSOR_FAULT: "Implausible weight: decisions paused",
  CUP_LOST: "No cup on this trip: no automatic pickup",
};

function describe(e: AdapterEventRow): string {
  if (e.action === "LOAD_WEIGHT") return `Load weight ${e.value?.toFixed(1)} g`;
  if (e.action === "DELIVERY_WEIGHT") return `Delivery weight ${e.value?.toFixed(1)} g`;
  if (e.action === "PICKUP_CONFIRMED") return "PICKUP CONFIRMED (shadow)";
  if (e.action) return `${e.action}`;
  return `${e.from_state ?? "—"} → ${e.to_state}`;
}

export default function AdapterPanel({ adapter, events, orders, now }: {
  adapter: AdapterStatus | null; events: AdapterEventRow[]; orders: Order[]; now: number;
}) {
  if (!adapter) {
    return <div className="card adapter-panel"><strong>Cup detection</strong> <span className="sub">waiting for status…</span></div>;
  }
  const folio = (msgId: number | null, orderId: number | null = null) =>
    orders.find((o) => (orderId != null && o.id === orderId) || (msgId != null && o.robot_msg_id === msgId))?.folio;

  const state = adapter.state;
  const stepIndex = STEPS.findIndex((s) => s.states.includes(state));
  const problem = PROBLEMS[state];

  // current trip, from the recorded events (newest first)
  const tripEvents = adapter.trip_order != null ? events.filter((e) => e.robot_msg_id === adapter.trip_order) : [];
  const load = events.find((e) => e.action === "LOAD_WEIGHT");
  const delivery = tripEvents.find((e) => e.action === "DELIVERY_WEIGHT");
  const pendingSince = state === "PICKUP_PENDING" ? events.find((e) => e.to_state === "PICKUP_PENDING" && !e.action) : undefined;
  const lastConfirmed = events.find((e) => e.action === "PICKUP_CONFIRMED");
  const lastProblem = events.find((e) => e.action === "PICKUP_BLOCKED" || e.action === "INCIDENT");
  const c = adapter.counters;

  return (
    <div className="card adapter-panel" data-testid="adapter-panel">
      <div className="adapter-head">
        <strong>Cup detection</strong>
        {adapter.mode === "shadow" ? (
          <span className="mode-badge shadow" data-testid="adapter-mode">
            SHADOW · decides and records only: nothing is sent to the robot
          </span>
        ) : (
          <span className="mode-badge off" data-testid="adapter-mode">OFF</span>
        )}
      </div>

      <ol className="steps" data-testid="adapter-steps">
        {STEPS.map((s, i) => (
          <li key={s.label} className={i === stepIndex ? "current" : i < stepIndex ? "done" : ""}>{s.label}</li>
        ))}
      </ol>
      <div className="adapter-state">
        State <strong data-testid="adapter-state">{state}</strong>
        {problem && <span className="note bad"> · {problem}</span>}
        {pendingSince && (
          <span> · cup off for {((now - Date.parse(pendingSince.at)) / 1000).toFixed(1)} s
            (confirms after {adapter.config.min_frames} frames below {adapter.config.m_off} g, ≥ {adapter.config.t_confirm} s)</span>
        )}
      </div>

      <div className="robot-grid" style={{ marginTop: 12 }}>
        <div><small>Trip</small><strong>{adapter.trip_order != null ? folio(adapter.trip_order) ?? adapter.trip_order : "—"}</strong>
          <small>robot {adapter.robot_state ?? "unknown"}</small></div>
        <div><small>Load weight (base)</small><strong>{load ? `${load.value?.toFixed(1)} g` : "—"}</strong>
          <small>{load ? clockMs(load.at) : ""}</small></div>
        <div><small>Delivery weight (table)</small><strong>{delivery ? `${delivery.value?.toFixed(1)} g` : "—"}</strong>
          <small>{delivery ? clockMs(delivery.at) : ""}</small></div>
        <div><small>Cup seen at table</small><strong>{adapter.armed ? "yes" : "no"}</strong></div>
        <div><small>Counters</small>
          <span>{c.pickups_confirmed ?? 0} confirmed · {c.pickups_blocked ?? 0} blocked · {c.incidents ?? 0} incidents</span>
          <small>readings fed {c.readings_fed ?? 0}{c.readings_not_fed ? ` · not fed ${c.readings_not_fed}` : ""}</small></div>
      </div>

      {lastConfirmed && (
        <div className="shadow-decision" data-testid="shadow-decision">
          <strong>Pickup confirmed at {clockMs(lastConfirmed.at)}</strong>
          {" "}for {folio(lastConfirmed.robot_msg_id, lastConfirmed.order_id) ?? `robot order ${lastConfirmed.robot_msg_id}`}:
          {" "}this is when <code>RETIRADO</code> would have been sent. <strong>Not sent (shadow mode).</strong>
        </div>
      )}
      {lastProblem && (!lastConfirmed || lastProblem.id > lastConfirmed.id) && (
        <div className="note bad" style={{ marginTop: 8 }}>
          {clockMs(lastProblem.at)} · {lastProblem.action} · {lastProblem.cause}
        </div>
      )}

      <details style={{ marginTop: 12 }} open>
        <summary className="sub">Recent decisions</summary>
        <table className="data" data-testid="adapter-events">
          <tbody>
            {events.slice(0, 12).map((e) => (
              <tr key={e.id} className={e.action === "PICKUP_CONFIRMED" ? "confirmed-row" : undefined}>
                <td style={{ whiteSpace: "nowrap" }}>{clockMs(e.at)}</td>
                <td>{describe(e)}</td>
                <td className="sub">{e.cause}</td>
                <td>{e.grams != null ? `${e.grams.toFixed(1)} g` : ""}</td>
                <td>{folio(e.robot_msg_id, e.order_id) ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </div>
  );
}
