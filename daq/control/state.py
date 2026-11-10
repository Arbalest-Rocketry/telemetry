from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from threading import Lock

from daq.control.config import ControlConfig


class State(Enum):
    IDLE = "IDLE"
    FILLING = "FILLING"
    PRESSURIZATION = "PRESSURIZATION"
    READY = "READY"
    TEST = "TEST"
    SAFE = "SAFE"
    ABORT = "ABORT"


class Valve(IntEnum):
    # Values are the Modbus coil offsets.
    LOX_DUMP = 0
    FUEL_DUMP = 1
    LOX_FILL = 2
    LOX_RUN = 3
    FUEL_RUN = 4
    LOX_VENT = 5
    FUEL_VENT = 6


Mask = tuple[bool, ...]


def mask(*open_valves: Valve) -> Mask:
    return tuple(valve in open_valves for valve in Valve)


DUMPS_AND_VENTS = mask(Valve.LOX_DUMP, Valve.FUEL_DUMP, Valve.LOX_VENT, Valve.FUEL_VENT)
PATTERNS: dict[State, Mask] = {
    State.IDLE: mask(),
    State.FILLING: mask(Valve.LOX_FILL, Valve.LOX_VENT, Valve.FUEL_VENT),
    State.PRESSURIZATION: mask(),
    State.READY: mask(),
    State.TEST: mask(Valve.LOX_RUN, Valve.FUEL_RUN),
    State.SAFE: DUMPS_AND_VENTS,
    State.ABORT: DUMPS_AND_VENTS,
}
LOCKED_IN_ABORT = (Valve.LOX_RUN, Valve.FUEL_RUN, Valve.LOX_FILL)


class ControlError(Exception):
    pass


@dataclass
class Controller:
    config: ControlConfig = field(default_factory=ControlConfig)
    send: Callable[[Mask], None] = lambda mask: None
    state: State = field(default=State.IDLE, init=False)
    valves: Mask = field(default=PATTERNS[State.IDLE], init=False)
    abort_reason: str = field(default="", init=False)
    # Highest watched PT; None while any watched reading is unusable.
    pressure_psi: float | None = field(default=None, init=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def change_state(self, state: State) -> None:
        with self._lock:
            if state is State.ABORT:
                self._abort("ABORT selected")
                return
            if self.state is State.ABORT:
                if state is not State.SAFE:
                    raise ControlError("Latched in ABORT. Reset to SAFE first.")
                self._check_reset_pressure()
            self.state = state
            self.abort_reason = ""
            self._apply(PATTERNS[state])

    def toggle(self, valve: Valve) -> None:
        with self._lock:
            if self.state is State.ABORT and valve in LOCKED_IN_ABORT:
                raise ControlError(f"{valve.name} is locked closed in ABORT.")
            valves = list(self.valves)
            valves[valve] = not valves[valve]
            self._apply(tuple(valves))

    def abort(self, reason: str = "Emergency abort") -> None:
        with self._lock:
            self._abort(reason)

    def auto_abort(self, reason: str) -> bool:
        """Trip once; while latched, manual dump/vent toggles are left alone."""
        with self._lock:
            if self.state is State.ABORT:
                return False
            self._abort(reason)
            return True

    def reset_abort(self) -> None:
        if self.state is not State.ABORT:
            raise ControlError("Not in ABORT.")
        self.change_state(State.SAFE)

    def _abort(self, reason: str) -> None:
        self.state = State.ABORT
        self.abort_reason = reason
        self._apply(PATTERNS[State.ABORT])

    def _check_reset_pressure(self) -> None:
        limit = self.config.reset_max_psi
        if self.pressure_psi is None:
            raise ControlError("Reset blocked: pressure unknown.")
        if self.pressure_psi > limit:
            raise ControlError(
                f"Reset blocked: {self.pressure_psi:.0f} PSI is above {limit:.0f} PSI."
            )

    def _apply(self, valves: Mask) -> None:
        if valves[Valve.LOX_RUN] and valves[Valve.LOX_FILL]:
            valves = tuple(
                is_open and valve is not Valve.LOX_FILL for valve, is_open in zip(Valve, valves)
            )
        self.valves = valves
        self.send(valves)
