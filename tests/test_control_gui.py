from collections.abc import Callable, Iterator
from pathlib import Path
import time

import pytest
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from daq.control.state import State, Valve, mask
from daq.gui import DaqWindow
from daq.models import PRESSURE_CHANNELS

V = Valve
ABORT_MASK = mask(V.LOX_DUMP, V.FUEL_DUMP, V.LOX_VENT, V.FUEL_VENT)


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
    deadline = time.monotonic() + 5
    while not condition():
        assert time.monotonic() < deadline, "Timed out waiting for GUI"
        QTest.qWait(10)


def volts(psi: float) -> float:
    return 0.472 + psi / 5000 * 1.888


def test_state_button_drives_coils_and_shows_commanded_and_confirmed(window: DaqWindow) -> None:
    wait_for(lambda: window.plc_status.text() == "PLC ok")
    assert window.state_label.text() == "State: IDLE"
    assert window.valve_buttons[V.LOX_RUN].text() == "LOX Run: CLOSED"
    assert not window.reset_button.isEnabled()
    window.state_buttons[State.TEST].click()
    wait_for(lambda: window.valve_confirmed[V.LOX_RUN].text() == "PLC OPEN")
    assert window.plc.coils == mask(V.LOX_RUN, V.FUEL_RUN)
    assert window.state_label.text() == "State: TEST"
    assert window.state_buttons[State.TEST].isChecked()
    assert not window.state_buttons[State.IDLE].isChecked()
    assert window.valve_buttons[V.LOX_RUN].text() == "LOX Run: OPEN"
    assert window.valve_buttons[V.FUEL_RUN].text() == "Fuel Run: OPEN"
    assert window.valve_buttons[V.LOX_FILL].text() == "LOX Fill: CLOSED"
    assert window.valve_confirmed[V.LOX_FILL].text() == "PLC CLOSED"


def test_valve_toggle_and_interlock(window: DaqWindow) -> None:
    window.state_buttons[State.FILLING].click()
    wait_for(lambda: window.plc.coils == mask(V.LOX_FILL, V.LOX_VENT, V.FUEL_VENT))
    window.valve_buttons[V.LOX_RUN].click()
    wait_for(lambda: window.plc.coils == mask(V.LOX_RUN, V.LOX_VENT, V.FUEL_VENT))
    wait_for(lambda: window.valve_buttons[V.LOX_FILL].text() == "LOX Fill: CLOSED")
    assert window.state_label.text() == "State: FILLING"


def test_emergency_abort_latches_until_reset_is_allowed(window: DaqWindow) -> None:
    window.state_buttons[State.TEST].click()
    window.abort_button.click()
    wait_for(lambda: window.plc.coils == ABORT_MASK)
    wait_for(lambda: window.state_label.text() == "State: ABORT")
    assert window.abort_alert.text() == "ABORT: Emergency abort"
    assert window.reset_button.isEnabled()

    window.state_buttons[State.IDLE].click()
    assert "Latched in ABORT" in window.control_message.text()
    window.valve_buttons[V.LOX_RUN].click()
    assert "locked" in window.control_message.text()
    # No acquisition yet, so there is no pressure to clear the reset with.
    window.reset_button.click()
    assert "pressure unknown" in window.control_message.text()
    QTest.qWait(100)
    assert window.state_label.text() == "State: ABORT"
    assert window.plc.coils == ABORT_MASK

    window.valve_buttons[V.LOX_VENT].click()
    wait_for(lambda: window.plc.coils == mask(V.LOX_DUMP, V.FUEL_DUMP, V.FUEL_VENT))
    assert window.control_message.text() == ""


def test_overpressure_auto_aborts_without_pausing_acquisition(
    window: DaqWindow, tmp_path: Path,
) -> None:
    window.connect_button.click()
    window.start_button.click()
    wait_for(window.stop_button.isEnabled)
    window.state_buttons[State.TEST].click()
    wait_for(lambda: window.plc.coils == mask(V.LOX_RUN, V.FUEL_RUN))
    source = window.acquisition.source
    source.overrides = {"pressure_2": volts(4600.0)}
    wait_for(lambda: window.state_label.text() == "State: ABORT")
    wait_for(lambda: window.plc.coils == ABORT_MASK)
    assert "pressure_2 4600 PSI > 4500 PSI" in window.abort_alert.text()
    assert not [w for w in QApplication.topLevelWidgets() if isinstance(w, QDialog)]
    previous_x = window.curves["pressure_1"].getData()[0][-1]
    wait_for(lambda: window.curves["pressure_1"].getData()[0][-1] > previous_x + 0.2)

    window.reset_button.click()
    assert "Reset blocked" in window.control_message.text()
    source.overrides = {channel.name: volts(20.0) for channel in PRESSURE_CHANNELS}
    wait_for(lambda: window.controller.pressure_psi == pytest.approx(20.0))
    window.reset_button.click()
    wait_for(lambda: window.state_label.text() == "State: SAFE")
    assert window.abort_alert.text() == ""
    assert not window.reset_button.isEnabled()

    window.stop_button.click()
    wait_for(window.start_button.isEnabled)
    run_dir, = tmp_path.iterdir()
    rows = (run_dir / "raw.csv").read_text().splitlines()
    t_s = [float(row.split(",")[0]) for row in rows[1:]]
    assert len(t_s) > 300
    assert max(b - a for a, b in zip(t_s, t_s[1:])) == pytest.approx(0.001)


def test_failed_pt_reads_auto_abort(window: DaqWindow) -> None:
    window.connect_button.click()
    wait_for(lambda: window.controller.pressure_psi is not None)
    window.acquisition.source.overrides = {"pressure_4": None}
    wait_for(lambda: window.state_label.text() == "State: ABORT")
    assert "pressure_4 read failed" in window.abort_alert.text()
    wait_for(lambda: window.plc.coils == ABORT_MASK)


def test_plc_outage_is_visible_and_the_mask_is_resent(window: DaqWindow) -> None:
    wait_for(lambda: window.plc_status.text() == "PLC ok")
    window.plc.stop()
    wait_for(lambda: window.plc_status.text().startswith("PLC disconnected"))
    assert window.valve_confirmed[V.LOX_DUMP].text() == "PLC ?"
    window.abort_button.click()
    wait_for(lambda: "write failed" in window.plc_status.text())
    assert window.valve_buttons[V.LOX_DUMP].text() == "LOX Dump: OPEN"
    assert window.valve_confirmed[V.LOX_DUMP].text() == "PLC ?"
    window.plc.start()
    wait_for(lambda: window.plc_status.text() == "PLC ok")
    assert window.plc.coils == ABORT_MASK
    assert window.valve_confirmed[V.LOX_DUMP].text() == "PLC OPEN"


def test_without_mock_the_plc_default_is_local_and_shown_as_down(app: QApplication) -> None:
    window = DaqWindow()
    window.show()
    try:
        assert window.plc is None
        assert window.valve_client.config.plc_host == "127.0.0.1"
        wait_for(lambda: window.plc_status.text().startswith("PLC disconnected"))
        assert window.valve_confirmed[V.LOX_DUMP].text() == "PLC ?"
    finally:
        window.close()
