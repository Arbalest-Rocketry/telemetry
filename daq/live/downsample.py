from dataclasses import dataclass, field
from datetime import datetime
from math import ceil, isfinite

from daq.models import CHANNELS, Channel, Reading


@dataclass
class Downsampler:
    sample_rate_hz: float
    live_rate_hz: float = 25.0
    channels: tuple[Channel, ...] = CHANNELS
    window_size: int = field(init=False)
    _count: int = field(default=0, init=False, repr=False)
    _sums: list[float] = field(init=False, repr=False)
    _missing: list[bool] = field(init=False, repr=False)
    _first_timestamp: datetime | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if not isfinite(self.sample_rate_hz) or self.sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be finite and positive")
        if not isfinite(self.live_rate_hz) or not 0 < self.live_rate_hz <= self.sample_rate_hz:
            raise ValueError("live_rate_hz must be positive and no greater than sample_rate_hz")
        names = [channel.name for channel in self.channels]
        if not names or len(set(names)) != len(names):
            raise ValueError("channels must be nonempty and unique")
        # Whole scan windows keep the actual live rate at or below the requested rate.
        self.window_size = ceil(self.sample_rate_hz / self.live_rate_hz)
        self._sums = [0.0] * len(self.channels)
        self._missing = [False] * len(self.channels)

    def add(self, scan: tuple[Reading, ...]) -> tuple[Reading, ...] | None:
        if tuple(r.channel for r in scan) != tuple(c.name for c in self.channels):
            raise ValueError("Scan channels must match the configured order")
        if any(r.timestamp != scan[0].timestamp for r in scan):
            raise ValueError("Readings in a scan must share a timestamp")
        if self._count == 0:
            self._first_timestamp = scan[0].timestamp
        for i, reading in enumerate(scan):
            if reading.raw_volts is None or not isfinite(reading.raw_volts):
                self._missing[i] = True
            else:
                self._sums[i] += reading.raw_volts
        self._count += 1
        if self._count < self.window_size:
            return None

        assert self._first_timestamp is not None
        timestamp = self._first_timestamp + (scan[0].timestamp - self._first_timestamp) / 2
        averaged = tuple(
            Reading(channel.name, timestamp, None if missing else total / self._count)
            for channel, total, missing in zip(self.channels, self._sums, self._missing)
        )
        self._count = 0
        self._sums = [0.0] * len(self.channels)
        self._missing = [False] * len(self.channels)
        return averaged
