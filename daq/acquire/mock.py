from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from math import pi, sin
from random import Random
from threading import Event
import time
from typing import ClassVar

from daq.calibrate import PT2400_CALIBRATION
from daq.models import CHANNELS, PRESSURE_CHANNELS, Reading


@dataclass
class MockSource:
    seed: int | None = None
    start_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    sample_rate_hz: ClassVar[int] = 1000
    _random: Random = field(init=False, repr=False)
    _scan_index: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.start_utc.utcoffset() is None:
            raise ValueError("start_utc must be timezone-aware")
        self.start_utc = self.start_utc.astimezone(timezone.utc)
        self._random = Random(self.seed)

    def _scan(self) -> tuple[Reading, ...]:
        timestamp = self.start_utc + timedelta(milliseconds=self._scan_index)
        t_s = self._scan_index / self.sample_rate_hz
        readings = []
        for i, channel in enumerate(CHANNELS):
            level = 0.5 + 0.4 * sin(2 * pi * 0.5 * t_s + i)
            level += self._random.uniform(-0.01, 0.01)
            if i < len(PRESSURE_CHANNELS):
                volts = PT2400_CALIBRATION.v_zero + PT2400_CALIBRATION.v_span * level
            else:
                # Example InAmp output: 1.25 V offset, 2 mV/V * 5 V * gain 201.
                volts = 1.25 + 2.01 * level
            readings.append(Reading(channel.name, timestamp, volts))
        self._scan_index += 1
        return tuple(readings)

    def generate(self, n_samples: int) -> list[tuple[Reading, ...]]:
        """Generate full scans without waiting, each with all 11 channels."""
        if n_samples < 0:
            raise ValueError("n_samples must be nonnegative")
        return [self._scan() for _ in range(n_samples)]

    def stream(self, stop: Event | None = None) -> Iterator[tuple[Reading, ...]]:
        """Pace scans at 1 kHz; pass an event to stop from another thread."""
        stop = stop if stop is not None else Event()
        start = time.monotonic()
        first_index = self._scan_index
        while not stop.is_set():
            deadline = start + (self._scan_index - first_index) / self.sample_rate_hz
            if stop.wait(max(0.0, deadline - time.monotonic())):
                break
            yield self._scan()
