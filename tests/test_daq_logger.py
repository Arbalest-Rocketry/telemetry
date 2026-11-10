import csv
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

from daq.acquire.mock import MockSource
from daq.log.writer import DiskLogger
from daq.models import CHANNELS, Channel, Reading


START = datetime(2026, 11, 10, 15, tzinfo=timezone(timedelta(hours=-5)))


def test_full_rate_mock_log(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    source = MockSource(seed=42, start_utc=START)
    started = time.monotonic()
    with DiskLogger(run_dir, "test-run", START, source.sample_rate_hz) as logger:
        scans = source.generate(10_000)
        for scan in scans:
            logger.write(scan)
    assert time.monotonic() - started < 10.0

    with (run_dir / "raw.csv").open(newline="") as raw:
        reader = csv.DictReader(raw)
        assert reader.fieldnames == ["t_s", "timestamp_utc", *(c.name for c in CHANNELS)]
        rows = list(reader)
    assert len(rows) == 10_000
    for i, row in enumerate(rows):
        timestamp = datetime.fromisoformat(row["timestamp_utc"])
        assert timestamp.utcoffset() == timedelta(0)
        assert timestamp == START + timedelta(milliseconds=i)
        assert float(row["t_s"]) == pytest.approx(i / 1000)
        assert [float(row[c.name]) for c in CHANNELS] == [r.raw_volts for r in scans[i]]

    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["contract_version"] == 1
    assert meta["source"] == "daq"
    assert meta["run_id"] == "test-run"
    assert meta["sample_rate_hz"] == 1000
    start = datetime.fromisoformat(meta["start_utc"])
    assert start == START
    assert start.utcoffset() == timedelta(0)
    assert list(meta["channels"]) == [c.name for c in CHANNELS]
    assert meta["channels"]["pressure_1"] == {
        "sensor": "PT2400", "raw_unit": "V", "unit": "PSI",
        "calibration_id": "pt2400-v1",
        "calibration": {"v_zero": 0.472, "v_span": 1.888, "full_scale": 5000.0},
    }
    assert meta["channels"]["load_cell_1"] == {
        "sensor": "RSB6-2HKM-S", "raw_unit": "V", "unit": "V",
        "calibration_id": None, "calibration": None,
    }


def test_missing_and_fault_voltages_are_preserved(tmp_path: Path) -> None:
    channels = (Channel(name="pressure", sensor="PT2400"),)
    with DiskLogger(tmp_path, "raw-values", START, 1000, channels) as logger:
        for i, value in enumerate([None, 0.0, -0.1, 5.0]):
            logger.write((Reading("pressure", START + timedelta(milliseconds=i), value),))
    with (tmp_path / "raw.csv").open(newline="") as raw:
        rows = list(csv.DictReader(raw))
    assert [row["pressure"] for row in rows] == ["", "0.0", "-0.1", "5.0"]


def test_mid_run_stop_flushes_pending_scans(tmp_path: Path) -> None:
    source = MockSource(seed=42, start_utc=START)
    with pytest.raises(KeyboardInterrupt):
        with DiskLogger(tmp_path, "interrupted", START, 1000) as logger:
            for scan in source.generate(137):
                logger.write(scan)
            raise KeyboardInterrupt
    with (tmp_path / "raw.csv").open(newline="") as raw:
        assert len(list(csv.DictReader(raw))) == 137
    assert json.loads((tmp_path / "meta.json").read_text())["run_id"] == "interrupted"
    logger.close()
    with pytest.raises(ValueError, match="closed"):
        logger.write(source.generate(1)[0])


def test_empty_run_has_header_and_metadata(tmp_path: Path) -> None:
    with DiskLogger(tmp_path, "empty", START, 1000):
        pass
    with (tmp_path / "raw.csv").open(newline="") as raw:
        assert len(list(csv.reader(raw))) == 1
    assert json.loads((tmp_path / "meta.json").read_text())["run_id"] == "empty"


def test_existing_recording_is_not_overwritten(tmp_path: Path) -> None:
    raw = tmp_path / "raw.csv"
    raw.write_text("previous recording\n")
    with pytest.raises(FileExistsError):
        DiskLogger(tmp_path, "duplicate", START, 1000)
    assert raw.read_text() == "previous recording\n"


def test_scan_must_match_configured_channels_and_timestamp(tmp_path: Path) -> None:
    scan = MockSource(seed=42, start_utc=START).generate(1)[0]
    with DiskLogger(tmp_path, "invalid-scan", START, 1000) as logger:
        for invalid in [(), scan[:-1], tuple(reversed(scan))]:
            with pytest.raises(ValueError, match="channels"):
                logger.write(invalid)
        different_time = Reading(scan[0].channel, START + timedelta(seconds=1), 0.5)
        with pytest.raises(ValueError, match="timestamp"):
            logger.write((different_time, *scan[1:]))
        logger.write(scan)
    with (tmp_path / "raw.csv").open(newline="") as raw:
        assert len(list(csv.DictReader(raw))) == 1


def test_record_cli(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "daq.record", "--mock", "--seconds", "0.02",
         "--out", str(tmp_path)],
        check=True, capture_output=True, text=True, timeout=10,
    )
    run_dir = Path(result.stdout.strip())
    assert run_dir.parent == tmp_path
    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["run_id"] == run_dir.name
    assert meta["sample_rate_hz"] == 1000
    with (run_dir / "raw.csv").open(newline="") as raw:
        assert len(list(csv.DictReader(raw))) == 20
