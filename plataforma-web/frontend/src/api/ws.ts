// Live order feed over WebSocket /api/ws/admin, with automatic reconnection.
// Every (re)connection gets a fresh snapshot, so nothing missed while offline is lost.
import { useEffect, useRef, useState } from "react";
import type {
  AdapterEventRow, AdapterStatus, FeedStatus, Order, RobotStatus, ScaleReading, ScaleStatus, ServerMessage,
} from "../types";

const PING_MS = 25_000;
const RETRY_MIN_MS = 1_000;
const RETRY_MAX_MS = 10_000;

function feedUrl(): string {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  return `${scheme}://${location.host}/api/ws/admin`; // Vite proxies /api (ws: true)
}

/** Keep the most recent version of each order (by updated_at); duplicates are harmless. */
function upsert(map: Map<number, Order>, o: Order) {
  const prev = map.get(o.id);
  if (!prev || o.updated_at >= prev.updated_at) map.set(o.id, o);
}

export interface OrdersFeed {
  orders: Order[];            // newest first
  status: FeedStatus;
  newIds: Set<number>;        // arrived live since the page opened (for highlighting)
  lastMessageAt: string | null;
  robot: RobotStatus | null;  // latest robot.status
  scale: ScaleStatus | null;  // latest scale.status
  scaleReadings: ScaleReading[]; // live readings since the page opened (oldest first, capped)
  adapter: AdapterStatus | null; // latest adapter.status
  adapterEvents: AdapterEventRow[]; // newest first, deduplicated by id
  mergeAdapterEvents: (rows: AdapterEventRow[]) => void; // e.g. history from GET /api/adapter/events
}

const ADAPTER_HISTORY = 500;

const SCALE_HISTORY = 400; // ~4 min at 0.6 s

export function useOrdersFeed(): OrdersFeed {
  const [status, setStatus] = useState<FeedStatus>("connecting");
  const [orders, setOrders] = useState<Order[]>([]);
  const [newIds, setNewIds] = useState<Set<number>>(new Set());
  const [lastMessageAt, setLastMessageAt] = useState<string | null>(null);
  const [robot, setRobot] = useState<RobotStatus | null>(null);
  const [scale, setScale] = useState<ScaleStatus | null>(null);
  const [scaleReadings, setScaleReadings] = useState<ScaleReading[]>([]);
  const [adapter, setAdapter] = useState<AdapterStatus | null>(null);
  const [adapterEvents, setAdapterEvents] = useState<AdapterEventRow[]>([]);
  const mergeAdapterEvents = useRef((rows: AdapterEventRow[]) =>
    setAdapterEvents((prev) => {
      const byId = new Map(prev.map((e) => [e.id, e]));
      rows.forEach((e) => byId.set(e.id, e));
      return [...byId.values()].sort((a, b) => b.id - a.id).slice(0, ADAPTER_HISTORY);
    }),
  ).current;
  const store = useRef(new Map<number, Order>());

  useEffect(() => {
    let ws: WebSocket | null = null;
    let stopped = false;
    let retryMs = RETRY_MIN_MS;
    let retryTimer: number | undefined;
    let pingTimer: number | undefined;

    const publish = () =>
      setOrders(
        [...store.current.values()].sort((a, b) =>
          a.created_at === b.created_at ? b.id - a.id : a.created_at < b.created_at ? 1 : -1,
        ),
      );

    const connect = () => {
      ws = new WebSocket(feedUrl());
      ws.onopen = () => {
        retryMs = RETRY_MIN_MS;
        pingTimer = window.setInterval(() => ws?.send(JSON.stringify({ type: "ping" })), PING_MS);
      };
      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data) as ServerMessage;
        setLastMessageAt(msg.ts);
        switch (msg.type) {
          case "snapshot":
            msg.data.orders.forEach((o) => upsert(store.current, o));
            publish();
            setStatus("live");
            break;
          case "order.created":
            upsert(store.current, msg.data);
            setNewIds((s) => new Set(s).add(msg.data.id));
            publish();
            break;
          case "order.updated":
            upsert(store.current, msg.data);
            publish();
            break;
          case "robot.status":
            setRobot(msg.data);
            break;
          case "scale.status":
            setScale(msg.data);
            break;
          case "adapter.status":
            setAdapter(msg.data);
            mergeAdapterEvents(msg.data.recent_events);
            break;
          case "adapter.event":
            mergeAdapterEvents([msg.data]);
            break;
          case "scale.reading": {
            const r = msg.data;
            setScaleReadings((prev) =>
              prev.length && prev[prev.length - 1].received_at >= r.received_at
                ? prev
                : [...prev.slice(-(SCALE_HISTORY - 1)), r],
            );
            break;
          }
          case "error":
            console.warn("[ws] server error:", msg.data.detail);
            break;
        }
      };
      ws.onclose = () => {
        window.clearInterval(pingTimer);
        if (stopped) return;
        setStatus("reconnecting");
        setRobot(null); // unknown until the next robot.status
        setScale(null); // unknown until the next scale.status
        setAdapter(null);
        retryTimer = window.setTimeout(connect, retryMs);
        retryMs = Math.min(retryMs * 2, RETRY_MAX_MS);
      };
    };

    connect();
    return () => {
      stopped = true;
      window.clearTimeout(retryTimer);
      window.clearInterval(pingTimer);
      ws?.close();
    };
  }, []);

  return { orders, status, newIds, lastMessageAt, robot, scale, scaleReadings, adapter, adapterEvents, mergeAdapterEvents };
}
