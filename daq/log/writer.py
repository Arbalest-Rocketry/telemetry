import csv
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from math import isfinite
from pathlib import Path
from queue import Full, Queue
from threading import Event, Thread
from types import TracebackType

from daq.models import CHANNELS, Channel, Reading


@dataclass
class DiskLogger:
    run_dir: Path
    run_id: str
    start_utc: datetime
    sample_rate_hz: float
    channels: tuple[Channel, ...] = CHANNELS
    # Two seconds of scans at 1 kHz; a full queue applies backpressure, never drops.
    _queue: Queue[tuple[Reading, ...] | None] = field(
        default_factory=lambda: Queue(maxsize=2000), init=False, repr=False
    )
    _ready: Event = field(default_factory=Event, init=False, repr=False)
    _thread: Thread = field(init=False, repr=False)
    _error: Exception | None = field(default=None, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.start_utc.utcoffset() is None:
            raise ValueError("start_utc must be timezone-aware")
        if not isfinite(self.sample_rate_hz) or self.sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be finite and positive")
        names = [channel.name for channel in self.channels]
        if not names or len(set(names)) != len(names):
            raise ValueError("channels must be nonempty and unique")
        if "t_s" in names:
            raise ValueError("Channel name conflicts with a timestamp column")
        self.start_utc = self.start_utc.astimezone(timezone.utc)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._thread = Thread(target=self._run, name="daq-disk-writer")
        self._thread.start()
        self._ready.wait()
        if self._error is not None:
            self._thread.join()
            raise self._error

    def _run(self) -> None:
        try:
            channels = {}
            for channel in self.channels:
                metadata = asdict(channel)
                metadata.pop("name")
                channels[channel.name] = metadata
            with (self.run_dir / "raw.csv").open(
                "x", newline="", encoding="utf-8", buffering=1024 * 1024
            ) as raw:
                with (self.run_dir / "meta.json").open("x", encoding="utf-8") as meta:
                    json.dump({
                        "contract_version": 1,
                        "source": "daq",
                        "run_id": self.run_id,
                        "start_utc": self.start_utc.isoformat(),
                        "sample_rate_hz": self.sample_rate_hz,
                        "channels": channels,
                    }, meta, indent=2)
                    meta.write("\n")
                writer = csv.writer(raw)
                writer.writerow(["t_s", *channels])
                self._ready.set()
                while (scan := self._queue.get()) is not None:
                    timestamp = scan[0].timestamp
                    writer.writerow([
                        (timestamp - self.start_utc).total_seconds(),
                        *(reading.raw_volts for reading in scan),
                    ])
        except Exception as error:
            self._error = error
        finally:
            self._ready.set()

    def _put(self, scan: tuple[Reading, ...] | None) -> None:
        while True:
            if self._error is not None:
                raise self._error
            try:
                self._queue.put(scan, timeout=0.1)
                return
            except Full:
                continue

    def write(self, scan: tuple[Reading, ...]) -> None:
        if self._closed:
            raise ValueError("Logger is closed")
        if tuple(reading.channel for reading in scan) != tuple(c.name for c in self.channels):
            raise ValueError("Scan channels must match the configured order")
        if any(reading.timestamp != scan[0].timestamp for reading in scan):
            raise ValueError("Readings in a scan must share a timestamp")
        self._put(scan)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._put(None)
            finally:
                self._thread.join()
        if self._error is not None:
            raise self._error

    def __enter__(self) -> "DiskLogger":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
