from dataclasses import dataclass

from daq.models import PRESSURE_CHANNELS


@dataclass(frozen=True)
class ControlConfig:
    abort_psi: float = 4500.0
    # Placeholder until the team picks a safe reset pressure.
    reset_max_psi: float = 50.0
    fault_low_v: float = 0.1
    fault_high_v: float = 4.9
    # How long a watched PT may be stale, missing, or in the fault band.
    fault_window_s: float = 0.25
    # All PTs until the team says which ones feed abort.
    watch: tuple[str, ...] = tuple(channel.name for channel in PRESSURE_CHANNELS)
    # Local fake PLC by default; the stand's address has to be set on purpose.
    plc_host: str = "127.0.0.1"
    plc_port: int = 5020
    plc_unit: int = 1
