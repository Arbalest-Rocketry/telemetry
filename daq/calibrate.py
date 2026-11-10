from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class Calibration:
    v_zero: float
    v_span: float
    full_scale: float

    def __post_init__(self) -> None:
        if not all(isfinite(v) for v in (self.v_zero, self.v_span, self.full_scale)):
            raise ValueError("Calibration parameters must be finite")
        if self.v_span == 0:
            raise ValueError("v_span must be nonzero")

    def convert(self, raw_volts: float | None) -> float | None:
        if raw_volts is None:
            return None
        return (raw_volts - self.v_zero) / self.v_span * self.full_scale


# CurrentShunt: 4 mA = 0.472 V; 20 mA = 2.360 V.
PT2400_CALIBRATION = Calibration(v_zero=0.472, v_span=1.888, full_scale=5000.0)


def pt2400_psi(raw_volts: float | None) -> float | None:
    return PT2400_CALIBRATION.convert(raw_volts)


def load_cell_calibration(
    *,
    gain: float,
    excitation_volts: float,
    sensitivity_mv_per_v: float,
    full_scale: float,
    v_zero: float = 1.25,  # InAmp 3 output offset.
) -> Calibration:
    if not all(
        isfinite(v) and v > 0
        for v in (gain, excitation_volts, sensitivity_mv_per_v, full_scale)
    ):
        raise ValueError("Load-cell parameters must be finite and positive")
    return Calibration(
        v_zero=v_zero,
        v_span=gain * excitation_volts * sensitivity_mv_per_v / 1000.0,  # mV/V to V/V.
        full_scale=full_scale,
    )
