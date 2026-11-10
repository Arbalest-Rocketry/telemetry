from dataclasses import dataclass, field
from math import isfinite
from queue import Empty, Full, Queue
from threading import Event, Thread
from types import TracebackType

import httpx

from daq.live.downsample import Downsampler
from daq.models import CHANNELS, Channel, Reading


@dataclass
class LiveClient:
    ingest_url: str
    run_id: str
    sample_rate_hz: float
    live_rate_hz: float = 25.0
    channels: tuple[Channel, ...] = CHANNELS
    queue_size: int = 8
    batch_scans: int = 5
    timeout_s: float = 0.5
    transport: httpx.BaseTransport | None = field(default=None, repr=False)
    dropped_batches: int = field(default=0, init=False)
    failed_batches: int = field(default=0, init=False)
    sent_batches: int = field(default=0, init=False)
    _downsampler: Downsampler = field(init=False, repr=False)
    _queue: Queue[tuple[tuple[Reading, ...], ...]] = field(init=False, repr=False)
    _pending: list[tuple[Reading, ...]] = field(default_factory=list, init=False, repr=False)
    _closing: Event = field(default_factory=Event, init=False, repr=False)
    _stop: Event = field(default_factory=Event, init=False, repr=False)
    _thread: Thread = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.queue_size < 1 or self.batch_scans < 1:
            raise ValueError("queue_size and batch_scans must be positive")
        if not isfinite(self.timeout_s) or self.timeout_s <= 0:
            raise ValueError("timeout_s must be finite and positive")
        self._downsampler = Downsampler(self.sample_rate_hz, self.live_rate_hz, self.channels)
        self._queue = Queue(maxsize=self.queue_size)
        self._thread = Thread(target=self._run, name="daq-live-sender", daemon=True)
        self._thread.start()

    def write(self, scan: tuple[Reading, ...]) -> None:
        if self._closing.is_set():
            raise ValueError("Live client is closed")
        averaged = self._downsampler.add(scan)
        if averaged is not None:
            self._pending.append(averaged)
            if len(self._pending) == self.batch_scans:
                self._enqueue()

    def _enqueue(self) -> None:
        if self._pending:
            try:
                self._queue.put_nowait(tuple(self._pending))
            except Full:
                # Live view is best-effort: keep the newest data and never stall the 1 kHz disk logger.
                try:
                    self._queue.get_nowait()
                except Empty:
                    pass
                else:
                    self.dropped_batches += 1
                self._queue.put_nowait(tuple(self._pending))
            self._pending.clear()

    def _payload(self, batch: tuple[tuple[Reading, ...], ...]) -> dict[str, object]:
        samples = []
        for scan in batch:
            for channel, reading in zip(self.channels, scan):
                value = reading.raw_volts
                if channel.calibration is not None:
                    value = channel.calibration.convert(value)
                samples.append({
                    "t": reading.timestamp.isoformat(),
                    "channel": channel.name,
                    "value": value if value is not None and isfinite(value) else None,
                })
        return {
            "contract_version": 1,
            "source": "daq",
            "run_id": self.run_id,
            "channels": {
                c.name: {"unit": c.unit, "calibration_id": c.calibration_id}
                for c in self.channels
            },
            "samples": samples,
        }

    def _run(self) -> None:
        with httpx.Client(timeout=self.timeout_s, transport=self.transport, trust_env=False) as client:
            while not self._stop.is_set():
                try:
                    batch = self._queue.get(timeout=0.05)
                except Empty:
                    if self._closing.is_set():
                        return
                    continue
                try:
                    response = client.post(
                        self.ingest_url.rstrip("/") + "/ingest", json=self._payload(batch),
                    )
                    response.raise_for_status()
                except httpx.HTTPError:
                    self.failed_batches += 1
                else:
                    self.sent_batches += 1

    def close(self) -> None:
        if not self._closing.is_set():
            self._enqueue()
            self._closing.set()
            self._thread.join(timeout=self.timeout_s + 0.1)
            self._stop.set()
            while True:
                try:
                    self._queue.get_nowait()
                except Empty:
                    break
                self.dropped_batches += 1

    def __enter__(self) -> "LiveClient":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
