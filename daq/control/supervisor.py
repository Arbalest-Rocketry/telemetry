from collections import deque
from collections.abc import Callable
from datetime import datetime
from math import isfinite
from threading import Event, Thread
import time

from daq.control.state import Controller
from daq.models import PRESSURE_CHANNELS, Reading

Scan = tuple[Reading, ...]


class AbortSupervisor:
    """Watches the PTs on its own thread and trips the controller's abort."""

    def __init__(
        self, controller: Controller, *,
        period_s: float = 0.01, clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.controller = controller
        self.config = controller.config
        self.period_s = period_s
        self.clock = clock
        channels = {channel.name: channel for channel in PRESSURE_CHANNELS}
        self._calibrations = {name: channels[name].calibration for name in self.config.watch}
        # Two seconds of scans; the acquisition side only ever appends.
        self._scans: deque[Scan] = deque(maxlen=2000)
        self._bad_since: dict[str, datetime] = {}
        self._last_scan = clock()
        self._stop = Event()
        self._thread = Thread(target=self._run, name="daq-abort")

    def offer(self, scan: Scan) -> None:
        self._scans.append(scan)

    def start(self) -> None:
        self._last_scan = self.clock()
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join()

    def _run(self) -> None:
        while not self._stop.wait(self.period_s):
            self.step()

    def step(self) -> None:
        now = self.clock()
        while self._scans:
            self._last_scan = now
            self._check(self._scans.popleft())
        stale_s = now - self._last_scan
        if stale_s > self.config.fault_window_s:
            self.controller.pressure_psi = None
            self.controller.auto_abort(f"PT readings stale for {stale_s:.2f} s")

    def _check(self, scan: Scan) -> None:
        config = self.config
        volts = {reading.channel: reading.raw_volts for reading in scan}
        pressures = []
        for name, calibration in self._calibrations.items():
            value = volts.get(name)
            if value is None or not isfinite(value):
                problem = "read failed"
            elif value < config.fault_low_v or value > config.fault_high_v:
                problem = f"in voltage fault band ({value:.2f} V)"
            else:
                self._bad_since.pop(name, None)
                psi = calibration.convert(value)
                pressures.append(psi)
                if psi > config.abort_psi:
                    self.controller.auto_abort(
                        f"{name} {psi:.0f} PSI > {config.abort_psi:.0f} PSI"
                    )
                continue
            since = self._bad_since.setdefault(name, scan[0].timestamp)
            bad_s = (scan[0].timestamp - since).total_seconds()
            if bad_s > config.fault_window_s:
                self.controller.auto_abort(f"{name} {problem} for {bad_s:.2f} s")
        complete = len(pressures) == len(self._calibrations)
        self.controller.pressure_psi = max(pressures) if complete else None
