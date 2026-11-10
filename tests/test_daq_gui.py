from collections.abc import Callable, Iterator
import csv
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import numpy as np
import pytest
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton

from daq.gui import DaqWindow
from daq.models import CHANNELS


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> QApplication:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app: QApplication, tmp_path: Path) -> Iterator[DaqWindow]:
    window = DaqWindow(mock=True, out=tmp_path)
    window.show()
    yield window
    window.close()


def wait_for(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 3
    while not condition():
        assert time.monotonic() < deadline, "Timed out waiting for GUI"
        QTest.qWait(10)


def test_mock_plots_and_values(window: DaqWindow) -> None:
    assert not window.start_button.isEnabled()
    window.connect_button.click()
    wait_for(lambda: all(curve.getData()[0] is not None for curve in window.curves.values()))
    wait_for(lambda: len(window.curves["pressure_1"].getData()[0]) >= 3)
    for channel in CHANNELS:
        x, y = window.curves[channel.name].getData()
        assert len(x) == len(y) >= 3
        assert np.diff(x) == pytest.approx(0.04)
        assert np.all(np.isfinite(y))
        if channel.unit == "PSI":
            assert np.all((y > 400) & (y < 4600))
        else:
            assert np.all((y > 1) & (y < 4))
        assert channel.unit in window.values[channel.name].text()
        assert "--" not in window.values[channel.name].text()
    assert {button.text().split(":")[0] for button in window.findChildren(QPushButton)} == {
        "Connect", "Start Recording", "Stop Recording", "EMERGENCY ABORT", "Reset Abort",
        "IDLE", "FILLING", "PRESSURIZATION", "READY", "TEST", "SAFE", "ABORT",
        "LOX Dump", "Fuel Dump", "LOX Fill", "LOX Run", "Fuel Run", "LOX Vent", "Fuel Vent",
    }


def read_run(run_dir: Path) -> list[dict[str, str]]:
    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["contract_version"] == 1
    assert meta["source"] == "daq"
    assert meta["run_id"] == run_dir.name
    assert meta["sample_rate_hz"] == 1000
    assert meta["channels"]["pressure_1"]["unit"] == "PSI"
    with (run_dir / "raw.csv").open(newline="") as raw:
        reader = csv.DictReader(raw)
        assert reader.fieldnames == ["t_s", *(c.name for c in CHANNELS)]
        rows = list(reader)
    assert len(rows) >= 100
    assert float(rows[0]["t_s"]) == 0
    assert [float(row["t_s"]) for row in rows] == pytest.approx(np.arange(len(rows)) / 1000)
    assert all(0.472 < float(row["pressure_1"]) < 2.36 for row in rows)
    return rows


def test_record_stop_and_close_flush_full_rate_runs(window: DaqWindow, tmp_path: Path) -> None:
    window.connect_button.click()
    window.start_button.click()
    wait_for(window.stop_button.isEnabled)
    # Acquisition and logging keep going while the GUI doesn't process events.
    time.sleep(0.15)
    window.stop_button.click()
    wait_for(window.start_button.isEnabled)
    first_run, = tmp_path.iterdir()
    first_rows = read_run(first_run)
    previous_x = window.curves["pressure_1"].getData()[0][-1]
    wait_for(lambda: window.curves["pressure_1"].getData()[0][-1] > previous_x)
    assert read_run(first_run) == first_rows

    window.start_button.click()
    wait_for(window.stop_button.isEnabled)
    time.sleep(0.15)
    window.close()
    runs = list(tmp_path.iterdir())
    assert len(runs) == 2
    second_run = next(run for run in runs if run != first_run)
    read_run(second_run)
    assert read_run(first_run) == first_rows


def test_recording_error_keeps_monitoring(window: DaqWindow, tmp_path: Path) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")
    window.out = blocked
    window.connect_button.click()
    window.start_button.click()
    wait_for(window.start_button.isEnabled)
    assert not window.stop_button.isEnabled()
    assert window.recording_status.text() != "Starting recording..."
    wait_for(lambda: window.curves["pressure_1"].getData()[0] is not None)


def test_screenshot_cli(tmp_path: Path) -> None:
    screenshot = tmp_path / "daq.png"
    subprocess.run(
        [sys.executable, "-m", "daq.gui", "--mock", "--screenshot", str(screenshot),
         "--after", "0.2", "--out", str(tmp_path / "runs")],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        check=True, capture_output=True, text=True, timeout=15,
    )
    assert screenshot.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert not (tmp_path / "runs").exists()
