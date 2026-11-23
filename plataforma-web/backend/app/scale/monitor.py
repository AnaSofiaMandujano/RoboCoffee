"""Live view of the adapter weight for admin pages (step 5.1c). Display only: no decisions.

link (GET /api/scale, WebSocket scale.status):
  never    no reading since the backend started
  live     the newest reading arrived less than scale_stale_s ago
  no_data  nothing new for scale_stale_s: adapter off/out of range OR bridge stopped.
           Silence is never shown as 0 g; the last weight is kept and marked old.

"Arrived" is measured with the backend's own monotonic clock when the batch is stored, so a clock
difference between Windows and the Docker VM can't make a live link look stale. The bridge's
received_at (PC time at COM reception) is still the timestamp of each reading.
"""
import asyncio
import time
from datetime import datetime, timezone

from ..realtime.manager import admin_feed

STATUS_HEARTBEAT_S = 5.0        # re-send scale.status at least this often
READING_MIN_INTERVAL_S = 0.2    # at most ~5 scale.reading messages per second


class ScaleMonitor:
    def __init__(self, stale_s: float = 3.0):
        self.stale_s = stale_s
        self.last: dict | None = None            # newest reading: {grams, raw, received_at, source}
        self.last_arrival_mono: float | None = None
        self.last_ingested_at: datetime | None = None
        self.readings_received = 0
        self._last_key = None
        self._last_status_sent = 0.0
        self._last_reading_sent = 0.0
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------ input
    def on_readings(self, source: str, readings: list[dict]) -> bool:
        """readings: [{grams, raw, received_at: datetime}]. Returns True if a NEWER reading arrived.
        Backlog sent after a backend outage never replaces a newer reading."""
        if not readings:
            return False
        self.readings_received += len(readings)
        newest = max(readings, key=lambda r: r["received_at"])
        if self.last is not None and newest["received_at"] <= self.last["received_at"]:
            return False
        self.last = {"grams": newest["grams"], "raw": newest["raw"],
                     "received_at": newest["received_at"], "source": source}
        self.last_arrival_mono = time.monotonic()
        self.last_ingested_at = datetime.now(timezone.utc)
        return True

    # ------------------------------------------------------------ output
    def status(self) -> dict:
        if self.last is None:
            link, age = "never", None
        else:
            age = time.monotonic() - self.last_arrival_mono
            link = "live" if age <= self.stale_s else "no_data"
        return {
            "link": link,
            "source": self.last["source"] if self.last else None,
            "last_reading": self.reading_json(),
            "age_s": round(age, 2) if age is not None else None,
            # diagnostic: backend time minus PC reception time of the newest reading (clock skew + delay)
            "ingest_delay_s": round((self.last_ingested_at - self.last["received_at"]).total_seconds(), 3)
            if self.last else None,
            "readings_received": self.readings_received,
            "stale_after_s": self.stale_s,
        }

    def reading_json(self) -> dict | None:
        if not self.last:
            return None
        return {**self.last, "received_at": self.last["received_at"].isoformat()}

    async def publish_reading(self) -> None:
        now = time.monotonic()
        if self.last and now - self._last_reading_sent >= READING_MIN_INTERVAL_S:
            self._last_reading_sent = now
            await admin_feed.broadcast("scale.reading", self.reading_json())
        await self.publish_status()

    async def publish_status(self, force: bool = False) -> None:
        s = self.status()
        key = (s["link"], s["source"])
        now = time.monotonic()
        if force or key != self._last_key or now - self._last_status_sent >= STATUS_HEARTBEAT_S:
            self._last_key, self._last_status_sent = key, now
            await admin_feed.broadcast("scale.status", s)

    # ------------------------------------------------------------ lifecycle
    async def _tick(self) -> None:
        """live -> no_data has no incoming event (it's silence), so check every second."""
        while True:
            await asyncio.sleep(1.0)
            await self.publish_status()

    def start(self) -> None:
        self._task = asyncio.create_task(self._tick(), name="scale-monitor")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
