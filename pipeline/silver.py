"""Step 2: bronze -> silver.

Reads raw.csv, applies the calibrations in meta.json, writes Parquet in
engineering units. Re-running on the same run must give the same output.
"""


def calibrate(raw: float, calibration: dict | None) -> float:
    """value = (raw - v_zero) / v_span * full_scale; unchanged if no calibration."""
    raise NotImplementedError


def build_silver(run_id: str, source: str) -> None:
    raise NotImplementedError
