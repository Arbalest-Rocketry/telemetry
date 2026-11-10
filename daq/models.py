from dataclasses import dataclass
from datetime import datetime, timezone
import re

from daq.calibrate import Calibration, PT2400_CALIBRATION


def _validate_channel_name(name: str) -> None:
    if re.fullmatch(r"[a-z][a-z0-9_]*", name) is None:
        raise ValueError("Channel names must be lowercase identifiers")


@dataclass(frozen=True)
class Channel:
    name: str
    sensor: str
    raw_unit: str = "V"
    unit: str = "V"
    calibration_id: str | None = None
    calibration: Calibration | None = None

    def __post_init__(self) -> None:
        _validate_channel_name(self.name)
        if self.calibration is None:
            if self.calibration_id is not None or self.unit != self.raw_unit:
                raise ValueError("Uncalibrated channels must retain raw units and no ID")
        elif not self.calibration_id:
            raise ValueError("Calibrated channels require a calibration_id")


@dataclass(frozen=True)
class Reading:
    channel: str
    timestamp: datetime
    raw_volts: float | None

    def __post_init__(self) -> None:
        _validate_channel_name(self.channel)
        if self.timestamp.utcoffset() is None:
            raise ValueError("Acquisition timestamp must be timezone-aware")
        object.__setattr__(self, "timestamp", self.timestamp.astimezone(timezone.utc))


# T7/Mux80 wiring isn't assigned yet.
PRESSURE_CHANNELS = tuple(
    Channel(
        name=f"pressure_{i}",
        sensor="PT2400",
        unit="PSI",
        calibration_id="pt2400-v1",
        calibration=PT2400_CALIBRATION,
    )
    for i in range(1, 7)
)

# Leave load cells in volts until calibration is known.
LOAD_CELL_CHANNELS = (
    Channel(name="load_cell_1", sensor="RSB6-2HKM-S"),
    *(Channel(name=f"load_cell_{i}", sensor="DYX-301") for i in range(2, 6)),
)

CHANNELS = PRESSURE_CHANNELS + LOAD_CELL_CHANNELS
