from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import pytest

from daq.calibrate import Calibration, load_cell_calibration, pt2400_psi
from daq.models import CHANNELS, LOAD_CELL_CHANNELS, PRESSURE_CHANNELS, Channel, Reading


@pytest.mark.parametrize(
    ("volts", "psi"),
    [(0.472, 0.0), (1.416, 2500.0), (2.360, 5000.0), (0.0, -1250.0), (4.248, 10000.0)],
)
def test_pt2400_conversion(volts: float, psi: float) -> None:
    assert pt2400_psi(volts) == pytest.approx(psi)


def test_calibration_preserves_missing_readings() -> None:
    assert pt2400_psi(None) is None
    assert Calibration(v_zero=1.0, v_span=2.0, full_scale=100.0).convert(None) is None


def test_configurable_linear_calibration() -> None:
    calibration = Calibration(v_zero=0.5, v_span=2.0, full_scale=1000.0)
    assert calibration.convert(0.5) == pytest.approx(0.0)
    assert calibration.convert(1.5) == pytest.approx(500.0)
    assert calibration.convert(2.5) == pytest.approx(1000.0)


@pytest.mark.parametrize("span", [0.0, -0.0])
def test_calibration_rejects_zero_span(span: float) -> None:
    with pytest.raises(ValueError, match="v_span"):
        Calibration(v_zero=0.0, v_span=span, full_scale=1.0)


@pytest.mark.parametrize("parameter", ["v_zero", "v_span", "full_scale"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_calibration_requires_finite_parameters(parameter: str, value: float) -> None:
    parameters = dict(v_zero=0.0, v_span=1.0, full_scale=1.0)
    parameters[parameter] = value
    with pytest.raises(ValueError, match="finite"):
        Calibration(**parameters)


def test_load_cell_inamp_conversion() -> None:
    # Example calibration: 2 mV/V * 5 V * gain 201 = 2.01 V span.
    calibration = load_cell_calibration(
        gain=201.0, excitation_volts=5.0, sensitivity_mv_per_v=2.0, full_scale=1000.0
    )
    assert calibration.convert(1.25) == pytest.approx(0.0)
    assert calibration.convert(3.26) == pytest.approx(1000.0)
    assert calibration.convert(0.245) == pytest.approx(-500.0)
    assert calibration.convert(None) is None


def test_load_cell_calibration_accepts_measured_zero() -> None:
    calibration = load_cell_calibration(
        gain=51.0,
        excitation_volts=10.0,
        sensitivity_mv_per_v=3.0,
        full_scale=2000.0,
        v_zero=1.3,
    )
    assert calibration.convert(1.3) == pytest.approx(0.0)
    assert calibration.convert(2.83) == pytest.approx(2000.0)


@pytest.mark.parametrize(
    "parameter", ["gain", "excitation_volts", "sensitivity_mv_per_v", "full_scale"]
)
@pytest.mark.parametrize("value", [0.0, -1.0, float("nan"), float("inf")])
def test_load_cell_requires_valid_parameters(parameter: str, value: float) -> None:
    parameters = dict(
        gain=201.0, excitation_volts=5.0, sensitivity_mv_per_v=2.0, full_scale=1000.0
    )
    parameters[parameter] = value
    with pytest.raises(ValueError, match="positive"):
        load_cell_calibration(**parameters)


def test_channel_inventory_and_contract_metadata() -> None:
    assert len(PRESSURE_CHANNELS) == 6
    assert len(LOAD_CELL_CHANNELS) == 5
    assert len({channel.name for channel in CHANNELS}) == 11
    for channel in PRESSURE_CHANNELS:
        assert channel.sensor == "PT2400"
        assert channel.raw_unit == "V"
        assert channel.unit == "PSI"
        assert channel.calibration_id == "pt2400-v1"
        assert channel.calibration is not None
        assert asdict(channel.calibration) == {
            "v_zero": 0.472, "v_span": 1.888, "full_scale": 5000.0
        }
        assert channel.calibration.convert(2.360) == pytest.approx(5000.0)
    for channel in LOAD_CELL_CHANNELS:
        assert channel.raw_unit == channel.unit == "V"
        assert channel.calibration_id is None
        assert channel.calibration is None


def test_load_cell_can_be_configured_with_calibration() -> None:
    calibration = load_cell_calibration(
        gain=201.0, excitation_volts=5.0, sensitivity_mv_per_v=2.0, full_scale=1000.0
    )
    channel = Channel(
        name="load_cell_1", sensor="RSB6-2HKM-S", unit="N",
        calibration_id="example-v1", calibration=calibration,
    )
    assert channel.unit == "N"
    assert channel.calibration is not None
    assert channel.calibration.convert(3.26) == pytest.approx(1000.0)


def test_channel_rejects_inconsistent_calibration_metadata() -> None:
    with pytest.raises(ValueError, match="raw units"):
        Channel(name="load_cell_1", sensor="load_cell", unit="N")
    with pytest.raises(ValueError, match="no ID"):
        Channel(name="load_cell_1", sensor="load_cell", calibration_id="unknown")
    with pytest.raises(ValueError, match="calibration_id"):
        Channel(
            name="load_cell_1", sensor="load_cell",
            calibration=Calibration(v_zero=0.0, v_span=1.0, full_scale=1.0),
        )


@pytest.mark.parametrize("raw_volts", [None, 0.0, 0.472, 5.0])
def test_reading_retains_raw_volts_and_utc_timestamp(raw_volts: float | None) -> None:
    timestamp = datetime(2026, 11, 10, 20, 15, 49, 123456, tzinfo=timezone.utc)
    reading = Reading(channel="pressure_1", timestamp=timestamp, raw_volts=raw_volts)
    assert reading.timestamp == timestamp
    assert reading.raw_volts == raw_volts


def test_reading_normalizes_aware_timestamp_to_utc() -> None:
    local = datetime(2026, 11, 10, 15, 15, tzinfo=timezone(timedelta(hours=-5)))
    reading = Reading(channel="pressure_1", timestamp=local, raw_volts=0.472)
    assert reading.timestamp == datetime(2026, 11, 10, 20, 15, tzinfo=timezone.utc)
    assert reading.timestamp.tzinfo is timezone.utc


def test_reading_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        Reading(channel="pressure_1", timestamp=datetime(2026, 11, 10), raw_volts=None)


@pytest.mark.parametrize("name", ["Pressure_1", "pressure 1", "", "1pressure"])
def test_channel_names_follow_contract(name: str) -> None:
    with pytest.raises(ValueError, match="lowercase"):
        Channel(name=name, sensor="PT2400")
    with pytest.raises(ValueError, match="lowercase"):
        Reading(channel=name, timestamp=datetime.now(timezone.utc), raw_volts=None)
