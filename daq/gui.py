import argparse
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from math import isfinite
from pathlib import Path
from queue import Empty, Full, Queue
import sys
from threading import Event, Thread
from uuid import uuid4

import pyqtgraph as pg
from PySide6.QtCore import QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication, QComboBox, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QPushButton, QVBoxLayout, QWidget,
)

from daq.acquire.mock import MockSource
from daq.control.config import ControlConfig
from daq.control.fake_plc import FakePlc
from daq.control.plc import ValveClient
from daq.control.state import ControlError, Controller, State, Valve
from daq.control.supervisor import AbortSupervisor
from daq.live.client import LiveClient
from daq.live.downsample import Downsampler
from daq.live.websocket import WebSocketClient
from daq.log.writer import DiskLogger
from daq.models import CHANNELS, Reading


@dataclass
class Acquisition:
    source: MockSource
    out: Path
    ingest_url: str
    live_transport: str = "ws"
    supervisor: AbortSupervisor | None = None
    samples: Queue[tuple[Reading, ...]] = field(default_factory=lambda: Queue(maxsize=8))
    messages: Queue[tuple[str, str]] = field(default_factory=Queue)
    commands: Queue[str] = field(default_factory=Queue)
    stop: Event = field(default_factory=Event)
    thread: Thread = field(init=False)

    def __post_init__(self) -> None:
        self.thread = Thread(target=self._run, name="daq-acquire")

    def _run(self) -> None:
        logger = None
        live = None
        downsampler = Downsampler(self.source.sample_rate_hz)
        run_id = uuid4().hex
        client_type = WebSocketClient if self.live_transport == "ws" else LiveClient
        try:
            if self.ingest_url:
                live = client_type(self.ingest_url, run_id, self.source.sample_rate_hz)
            for scan in self.source.stream(self.stop):
                try:
                    command = self.commands.get_nowait()
                except Empty:
                    command = ""
                # Before the logger, so a slow disk can't hold up the abort check.
                if self.supervisor is not None:
                    self.supervisor.offer(scan)
                if command == "start" and logger is None:
                    run_id = uuid4().hex
                    if live is not None:
                        live.close()
                        live = client_type(self.ingest_url, run_id, self.source.sample_rate_hz)
                    try:
                        logger = DiskLogger(
                            self.out / run_id, run_id, scan[0].timestamp,
                            self.source.sample_rate_hz,
                        )
                    except Exception as error:
                        self.messages.put(("recording_error", str(error)))
                    else:
                        self.messages.put(("recording", str(logger.run_dir)))
                elif command == "stop" and logger is not None:
                    logger.close()
                    logger = None
                    self.messages.put(("stopped", "Recording stopped"))
                if logger is not None:
                    logger.write(scan)
                if live is not None:
                    live.write(scan)
                averaged = downsampler.add(scan)
                if averaged is not None:
                    try:
                        self.samples.put_nowait(averaged)
                    except Full:
                        pass
        except Exception as error:
            self.messages.put(("error", str(error)))
        finally:
            try:
                if logger is not None:
                    logger.close()
            except Exception as error:
                self.messages.put(("error", str(error)))
            finally:
                if live is not None:
                    live.close()


class DaqWindow(QMainWindow):
    def __init__(
        self, *, mock: bool = False, out: Path = Path("data/runs"), ingest_url: str = "",
        config: ControlConfig = ControlConfig(),
    ) -> None:
        super().__init__()
        self.mock = mock
        self.out = out
        self.acquisition: Acquisition | None = None
        self.supervisor: AbortSupervisor | None = None
        self.plc: FakePlc | None = None
        if mock:
            self.plc = FakePlc()
            self.plc.start()
            config = replace(config, plc_host=self.plc.host, plc_port=self.plc.port)
        self.valve_client = ValveClient(config)
        self.controller = Controller(config, send=self.valve_client.set_mask)
        self.setWindowTitle("Arbalest DAQ")
        self.resize(1500, 950)
        self.setStyleSheet("""
            QWidget { background: #05070a; color: #e0e6ed; font-size: 13px; }
            QLabel#title { font-size: 26px; font-weight: bold; }
            QLabel#card { background: #111625; border: 1px solid #1d263b;
                          border-radius: 6px; padding: 8px; }
            QPushButton, QLineEdit { background: #111726; border: 1px solid #2a3652;
                                    border-radius: 5px; padding: 9px; }
            QPushButton:hover { background: #26314a; }
            QPushButton:disabled { color: #52607a; }
            QPushButton:checked { background: #14532d; border-color: #00e676; }
            QPushButton#abort { background: #b71c1c; font-weight: bold; padding: 14px; }
            QLabel#alert { color: #ff5252; font-weight: bold; }
        """)
        central = QWidget()
        layout = QHBoxLayout(central)
        main = QVBoxLayout()
        header = QHBoxLayout()
        title = QLabel("ARBALEST DAQ")
        title.setObjectName("title")
        header.addWidget(title)
        header.addStretch()
        self.status = QLabel("Offline")
        header.addWidget(self.status)
        main.addLayout(header)

        cards = QGridLayout()
        plots = QGridLayout()
        self.values: dict[str, QLabel] = {}
        self.plots: dict[str, pg.PlotWidget] = {}
        self.curves: dict[str, pg.PlotDataItem] = {}
        # Ten seconds at the downsampler's 25 Hz output rate.
        self.times: deque[float] = deque(maxlen=250)
        self.history: dict[str, deque[float]] = {}
        for i, channel in enumerate(CHANNELS):
            label = QLabel(f"{channel.name}\n-- {channel.unit}")
            label.setObjectName("card")
            cards.addWidget(label, i // 6, i % 6)
            self.values[channel.name] = label
            plot = pg.PlotWidget(background="#0a0d16")
            plot.setTitle(channel.name, color="#e0e6ed", size="10pt")
            plot.setLabel("left", channel.unit)
            plot.setLabel("bottom", "Elapsed time", units="s")
            plot.showGrid(x=True, y=True, alpha=0.15)
            plot.setMouseEnabled(x=False, y=False)
            plot.setMenuEnabled(False)
            plots.addWidget(plot, i // 3, i % 3)
            self.plots[channel.name] = plot
            self.curves[channel.name] = plot.plot(
                pen=pg.mkPen("#00e676" if channel.calibration else "#b37cff", width=2),
                connect="finite",
            )
            self.history[channel.name] = deque(maxlen=250)
        main.addLayout(cards)
        main.addLayout(plots, 1)
        layout.addLayout(main, 5)

        controls = QVBoxLayout()
        controls.addWidget(QLabel("CONNECTION & RECORDING"))
        controls.addWidget(QLabel("Source: mock / 1 kHz" if mock else "Source: unavailable"))
        controls.addWidget(QLabel("Ingest URL (optional)"))
        self.ingest_url = QLineEdit(ingest_url)
        self.ingest_url.setPlaceholderText("http://localhost:8000")
        controls.addWidget(self.ingest_url)
        controls.addWidget(QLabel("Live transport"))
        self.live_transport = QComboBox()
        self.live_transport.addItems(("ws", "http"))
        controls.addWidget(self.live_transport)
        self.connect_button = QPushButton("Connect")
        self.connect_button.clicked.connect(self.connect_source)
        controls.addWidget(self.connect_button)
        self.start_button = QPushButton("Start Recording")
        self.start_button.setEnabled(False)
        self.start_button.clicked.connect(self.start_recording)
        controls.addWidget(self.start_button)
        self.stop_button = QPushButton("Stop Recording")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_recording)
        controls.addWidget(self.stop_button)
        self.recording_status = QLabel("Recording off")
        self.recording_status.setWordWrap(True)
        controls.addWidget(self.recording_status)
        controls.addWidget(QLabel("Pressure: PSI\nLoad cells: V (uncalibrated)\nPlots: 25 Hz / last 10 s"))
        controls.addStretch()
        layout.addLayout(controls, 1)
        layout.addLayout(self._control_column(), 1)
        self.setCentralWidget(central)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh)
        self.timer.start(40)

    def _control_column(self) -> QVBoxLayout:
        column = QVBoxLayout()
        column.addWidget(QLabel("STAND CONTROL"))
        self.state_label = QLabel()
        column.addWidget(self.state_label)
        self.abort_alert = QLabel()
        self.abort_alert.setObjectName("alert")
        self.abort_alert.setWordWrap(True)
        column.addWidget(self.abort_alert)
        self.abort_button = QPushButton("EMERGENCY ABORT")
        self.abort_button.setObjectName("abort")
        self.abort_button.clicked.connect(lambda: self.controller.abort())
        column.addWidget(self.abort_button)
        self.reset_button = QPushButton("Reset Abort")
        self.reset_button.clicked.connect(lambda: self._command(self.controller.reset_abort))
        column.addWidget(self.reset_button)
        self.control_message = QLabel()
        self.control_message.setWordWrap(True)
        column.addWidget(self.control_message)

        column.addWidget(QLabel("TEST STATE"))
        self.state_buttons: dict[State, QPushButton] = {}
        for state in State:
            button = QPushButton(state.value)
            button.setCheckable(True)
            button.clicked.connect(
                lambda _, state=state: self._command(self.controller.change_state, state)
            )
            column.addWidget(button)
            self.state_buttons[state] = button

        column.addWidget(QLabel("VALVES (commanded / PLC readback)"))
        self.valve_buttons: dict[Valve, QPushButton] = {}
        self.valve_confirmed: dict[Valve, QLabel] = {}
        for valve in Valve:
            row = QHBoxLayout()
            button = QPushButton()
            button.setCheckable(True)
            button.clicked.connect(
                lambda _, valve=valve: self._command(self.controller.toggle, valve)
            )
            row.addWidget(button, 1)
            confirmed = QLabel()
            row.addWidget(confirmed)
            column.addLayout(row)
            self.valve_buttons[valve] = button
            self.valve_confirmed[valve] = confirmed
        self.plc_status = QLabel()
        self.plc_status.setWordWrap(True)
        column.addWidget(self.plc_status)
        column.addStretch()
        self._refresh_control()
        return column

    def _command(self, action: Callable[..., None], *args: object) -> None:
        try:
            action(*args)
        except ControlError as error:
            self.control_message.setText(str(error))
        else:
            self.control_message.setText("")

    def _refresh_control(self) -> None:
        controller = self.controller
        state = controller.state
        commanded = controller.valves
        confirmed = self.valve_client.confirmed
        self.state_label.setText(f"State: {state.value}")
        aborted = state is State.ABORT
        self.abort_alert.setText(f"ABORT: {controller.abort_reason}" if aborted else "")
        self.reset_button.setEnabled(aborted)
        for button_state, button in self.state_buttons.items():
            button.setChecked(button_state is state)
        for valve in Valve:
            name = " ".join(w if w == "LOX" else w.title() for w in valve.name.split("_"))
            is_open = commanded[valve]
            self.valve_buttons[valve].setChecked(is_open)
            self.valve_buttons[valve].setText(f"{name}: {'OPEN' if is_open else 'CLOSED'}")
            label = self.valve_confirmed[valve]
            if confirmed is None:
                text = "PLC ?"
            else:
                text = f"PLC {'OPEN' if confirmed[valve] else 'CLOSED'}"
            matches = confirmed is not None and confirmed[valve] == is_open
            style = f"color: {'#00e676' if matches else '#ffab00'}"
            label.setText(text)
            if label.styleSheet() != style:
                label.setStyleSheet(style)
        self.plc_status.setText(self.valve_client.status)

    def connect_source(self) -> None:
        if self.acquisition is not None:
            return
        if not self.mock:
            self.status.setText("No hardware adapter yet. Launch with --mock.")
            return
        self.supervisor = AbortSupervisor(self.controller)
        self.acquisition = Acquisition(
            MockSource(), self.out, self.ingest_url.text().strip(),
            self.live_transport.currentText(), self.supervisor,
        )
        self.supervisor.start()
        self.acquisition.thread.start()
        self.connect_button.setEnabled(False)
        self.ingest_url.setEnabled(False)
        self.live_transport.setEnabled(False)
        self.start_button.setEnabled(True)
        self.status.setText("Connected / MOCK / 1 kHz")

    def start_recording(self) -> None:
        if self.acquisition is not None and self.start_button.isEnabled():
            self.start_button.setEnabled(False)
            self.recording_status.setText("Starting recording...")
            self.acquisition.commands.put("start")

    def stop_recording(self) -> None:
        if self.acquisition is not None and self.stop_button.isEnabled():
            self.stop_button.setEnabled(False)
            self.recording_status.setText("Saving recording...")
            self.acquisition.commands.put("stop")

    def _refresh(self) -> None:
        self._refresh_control()
        acquisition = self.acquisition
        if acquisition is None:
            return
        while True:
            try:
                kind, message = acquisition.messages.get_nowait()
            except Empty:
                break
            self.recording_status.setText(message)
            self.start_button.setEnabled(kind in ("stopped", "recording_error"))
            self.stop_button.setEnabled(kind == "recording")
            if kind == "error":
                self.status.setText("Acquisition stopped")

        updated = False
        while True:
            try:
                scan = acquisition.samples.get_nowait()
            except Empty:
                break
            self.times.append((scan[0].timestamp - acquisition.source.start_utc).total_seconds())
            for channel, reading in zip(CHANNELS, scan):
                value = reading.raw_volts
                if channel.calibration is not None:
                    value = channel.calibration.convert(value)
                valid = value is not None and isfinite(value)
                self.history[channel.name].append(value if valid else float("nan"))
                text = f"{value:.1f}" if valid else "--"
                self.values[channel.name].setText(f"{channel.name}\n{text} {channel.unit}")
            updated = True
        if updated:
            for name, curve in self.curves.items():
                curve.setData(list(self.times), list(self.history[name]))
                self.plots[name].setXRange(max(0, self.times[-1] - 10), max(10, self.times[-1]), padding=0)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.timer.stop()
        # Supervisor first, or the acquisition winding down reads as stale PTs.
        if self.supervisor is not None:
            self.supervisor.close()
        if self.acquisition is not None:
            self.acquisition.stop.set()
            self.acquisition.thread.join()
        self.valve_client.close()
        if self.plc is not None:
            self.plc.stop()
        event.accept()


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor DAQ channels and record raw volts.")
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--out", type=Path, default=Path("data/runs"))
    parser.add_argument("--ingest-url", default="")
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument("--after", type=float, default=2.0)
    args = parser.parse_args()
    if not isfinite(args.after) or not 0 < args.after <= 2_147_483:
        parser.error("--after must be finite, positive and at most 2147483 seconds")
    if args.screenshot and not args.mock:
        parser.error("--screenshot requires --mock")
    app = QApplication(sys.argv[:1])
    window = DaqWindow(mock=args.mock, out=args.out, ingest_url=args.ingest_url)
    window.show()
    if args.screenshot:
        window.connect_source()

        def save_screenshot() -> None:
            saved = window.grab().save(str(args.screenshot))
            if not saved:
                print(f"Could not save screenshot: {args.screenshot}", file=sys.stderr)
            window.close()
            app.exit(0 if saved else 1)

        QTimer.singleShot(max(1, int(args.after * 1000)), save_screenshot)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
