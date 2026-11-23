// Adapter weight, live (5.1c). Display only: no pickup decisions are taken from it.
import { useEffect, useMemo, useState } from "react";
import { getJson } from "../api/http";
import type { AdapterEventRow, ScaleReading, ScaleStatus } from "../types";

const WINDOW_S = 60;
const LINK: Record<ScaleStatus["link"], { text: string; color: string }> = {
  live: { text: "● Live", color: "var(--ok)" },
  no_data: { text: "● No data", color: "var(--bad)" },
  never: { text: "● No readings yet", color: "var(--muted)" },
};

/** HH:MM:SS.mmm in this computer's local time. */
function clock(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleTimeString("es-MX", { hour12: false }) + "." + String(d.getMilliseconds()).padStart(3, "0");
}

export default function ScalePanel({ scale, live, now, markers = [] }: {
  scale: ScaleStatus | null; live: ScaleReading[]; now: number; markers?: AdapterEventRow[];
}) {
  const [history, setHistory] = useState<ScaleReading[]>([]);

  // Seed the chart with the last minute from the database; the WebSocket appends new readings.
  useEffect(() => {
    getJson<ScaleReading[]>(`/scale/readings?seconds=${WINDOW_S}`).then(setHistory).catch(() => setHistory([]));
  }, [scale?.source]);

  const points = useMemo(() => {
    const byTime = new Map<string, ScaleReading>();
    [...history, ...live].forEach((r) => byTime.set(r.received_at, r));
    const since = now - WINDOW_S * 1000;
    return [...byTime.values()]
      .filter((r) => Date.parse(r.received_at) >= since && (!scale?.source || r.source === scale.source))
      .sort((a, b) => (a.received_at < b.received_at ? -1 : 1));
  }, [history, live, now, scale?.source]);

  const link = scale ? LINK[scale.link] : { text: "waiting for server…", color: "var(--muted)" };
  // Newest reading from EITHER source: scale.reading arrives with every batch, while scale.status is only
  // re-sent on link changes / every 5 s. Using only the status made the big number lag up to 5 s.
  const liveLast = live.length ? live[live.length - 1] : null;
  const statusLast = scale?.last_reading ?? null;
  const last = liveLast && (!statusLast || liveLast.received_at > statusLast.received_at) ? liveLast : statusLast;
  const ageS = last ? Math.max(0, (now - Date.parse(last.received_at)) / 1000) : null;
  const fresh = scale?.link === "live";

  const W = 600, H = 150, PAD = 4;
  const vals = points.map((p) => p.grams);
  const yMax = Math.max(400, ...vals), yMin = Math.min(-60, ...vals);
  const x = (iso: string) => PAD + ((Date.parse(iso) - (now - WINDOW_S * 1000)) / (WINDOW_S * 1000)) * (W - 2 * PAD);
  const y = (g: number) => PAD + (1 - (g - yMin) / (yMax - yMin)) * (H - 2 * PAD);
  const path = points.map((p, i) => `${i ? "L" : "M"}${x(p.received_at).toFixed(1)},${y(p.grams).toFixed(1)}`).join(" ");

  return (
    <div className="card scale-panel" data-testid="scale-panel">
      <div className="scale-head">
        <div>
          <small>Adapter weight</small>
          <div className="scale-weight" data-testid="scale-weight" style={{ opacity: fresh ? 1 : 0.4 }}
               title={fresh ? "" : "Last known weight, not current"}>
            {last ? `${last.grams.toFixed(1)} g` : "— g"}
          </div>
        </div>
        <div className="scale-meta">
          <span style={{ color: link.color, fontWeight: 600 }} data-testid="scale-link">{link.text}</span>
          <small>{scale?.source ?? "—"}</small>
          <small>
            Last reading: {last ? `${clock(last.received_at)} (${ageS?.toFixed(1)} s ago)` : "never"}
          </small>
          <small>Received since backend start: {scale?.readings_received ?? 0}</small>
        </div>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} role="img" aria-label="Weight, last 60 seconds">
        <line x1={PAD} x2={W - PAD} y1={y(0)} y2={y(0)} stroke="var(--line)" />
        {markers
          .filter((m) => Date.parse(m.at) >= now - WINDOW_S * 1000 &&
            (m.action === "PICKUP_CONFIRMED" || (!m.action && m.to_state === "PICKUP_PENDING")))
          .map((m) => {
            const confirmed = m.action === "PICKUP_CONFIRMED";
            return (
              <g key={m.id} data-testid={confirmed ? "marker-confirmed" : "marker-pending"}>
                <line x1={x(m.at)} x2={x(m.at)} y1={PAD} y2={H - PAD} stroke={confirmed ? "#c99700" : "var(--muted)"}
                      strokeWidth={confirmed ? 2 : 1} strokeDasharray={confirmed ? undefined : "3 3"} />
                <text x={x(m.at) + 3} y={confirmed ? 14 : 26} fontSize="10" fill={confirmed ? "#7a5a00" : "var(--muted)"}>
                  {confirmed ? "pickup confirmed (shadow)" : "pickup pending"}
                </text>
              </g>
            );
          })}
        <path d={path} fill="none" stroke="var(--coffee)" strokeWidth={1.8} />
        {points.length > 0 && (
          <circle cx={x(points[points.length - 1].received_at)} cy={y(points[points.length - 1].grams)} r={3}
                  fill="var(--coffee)" />
        )}
      </svg>
      <small className="sub">Last {WINDOW_S} s · times are this PC's reception time · markers: cup-detection decisions</small>
    </div>
  );
}
