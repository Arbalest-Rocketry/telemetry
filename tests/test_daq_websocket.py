from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
import csv
from datetime import datetime, timedelta, timezone
from itertools import islice
from pathlib import Path
import socket
import subprocess
import sys
from threading import Event, Thread
import time
from typing import NoReturn

import pytest
import uvicorn
from PySide6.QtWidgets import QApplication
from starlette.types import Receive, Scope, Send

from daq.acquire.mock import MockSource
from daq.gui import DaqWindow
from daq.live.client import LiveClient
from daq.live.websocket import WebSocketClient
from daq.log.writer import DiskLogger
from ingest.app import create_app
from ingest.contract import LiveBatch
from ingest.db import InMemoryStore


START = datetime(2026, 11, 10, 20, tzinfo=timezone.utc)


def wait_for(condition: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline
        time.sleep(0.01)


@dataclass
class IngestServer:
    store: InMemoryStore = field(default_factory=InMemoryStore)
    paths: list[str] = field(default_factory=list)
    port: int = 0
    _server: uvicorn.Server = field(init=False)
    _thread: Thread = field(init=False)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", self.port))
        self.port = sock.getsockname()[1]
        app = create_app(self.store)

        async def track(scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] in ("http", "websocket"):
                self.paths.append(scope["path"])
            await app(scope, receive, send)

        self._server = uvicorn.Server(uvicorn.Config(
            track, log_level="error", lifespan="off",
            ws="websockets-sansio", timeout_graceful_shutdown=0.2,
        ))
        self._thread = Thread(target=self._server.run, kwargs={"sockets": [sock]}, daemon=True)
        self._thread.start()
        wait_for(lambda: self._server.started)

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=3.0)
        assert not self._thread.is_alive()


@pytest.fixture
def server() -> Iterator[IngestServer]:
    server = IngestServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


def test_record_cli_defaults_to_websocket(server: IngestServer, tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "daq.record", "--mock", "--seconds", "0.2",
         "--out", str(tmp_path), "--ingest-url", server.url, "--live-rate-hz", "50"],
        check=True, capture_output=True, text=True, timeout=10,
    )
    run_dir = Path(result.stdout.strip())
    with (run_dir / "raw.csv").open(newline="") as raw:
        assert len(list(csv.DictReader(raw))) == 200
    assert server.paths == ["/ingest/ws"]
    assert len(server.store.batches) == 2
    assert all(batch.run_id == run_dir.name for batch in server.store.batches)
    assert all(len(batch.samples) == 55 for batch in server.store.batches)


@pytest.mark.parametrize("transport", ["ws", "http"])
def test_gui_live_transport_survives_recording_start(
    server: IngestServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transport: str,
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    window = DaqWindow(mock=True, out=tmp_path, ingest_url=server.url)
    try:
        assert window.live_transport.currentText() == "ws"
        window.live_transport.setCurrentText(transport)
        window.connect_button.click()
        assert not window.live_transport.isEnabled()
        wait_for(lambda: bool(server.store.batches))
        monitoring_run = server.store.batches[0].run_id
        window.start_button.click()
        wait_for(lambda: any(batch.run_id != monitoring_run for batch in server.store.batches))
        app.processEvents()
    finally:
        window.close()
    assert set(server.paths) == {"/ingest/ws" if transport == "ws" else "/ingest"}
    run_dir, = tmp_path.iterdir()
    assert any(batch.run_id == run_dir.name for batch in server.store.batches)
    with (run_dir / "raw.csv").open(newline="") as raw:
        assert len(list(csv.DictReader(raw))) >= 200


def test_websocket_sends_same_downsampled_batches_as_http(server: IngestServer) -> None:
    scans = MockSource(seed=42, start_utc=START).generate(240)
    for client_type in (LiveClient, WebSocketClient):
        with client_type(server.url, "same-batches", 1000, 25) as live:
            for scan in scans:
                live.write(scan)
        assert live.sent_batches == 2
        assert live.failed_batches == live.dropped_batches == 0
    first, second, third, fourth = server.store.batches
    assert (first, second) == (third, fourth)
    assert [len(batch.samples) for batch in (third, fourth)] == [55, 11]
    pressure = third.samples[0]
    assert pressure.t == START + timedelta(milliseconds=19.5)
    volts = sum(scan[0].raw_volts for scan in scans[:40]) / 40
    assert pressure.value == pytest.approx((volts - 0.472) / 1.888 * 5000)


def test_disconnect_keeps_full_rate_log_and_reconnects(
    server: IngestServer, tmp_path: Path,
) -> None:
    source = MockSource(seed=42, start_utc=START)
    disconnected = Event()
    outage_logged = Event()
    finished = Event()
    scans = []
    errors = []
    live = WebSocketClient(
        server.url, "reconnect", 1000, 50, queue_size=2, batch_scans=1,
        timeout_s=0.1, reconnect_delay_s=0.02, max_reconnect_delay_s=0.08,
    )

    def record() -> None:
        try:
            with DiskLogger(tmp_path, "reconnect", START, 1000) as logger:
                for scan in islice(source.stream(), 2000):
                    logger.write(scan)
                    live.write(scan)
                    scans.append(scan)
                    if disconnected.is_set() and len(scans) >= 1000:
                        outage_logged.set()
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()

    thread = Thread(target=record, daemon=True)
    thread.start()
    try:
        wait_for(lambda: live.sent_batches >= 2)
        server.stop()
        disconnected.set()
        accepted_before = len(server.store.batches)
        assert outage_logged.wait(1.5)
        assert not finished.is_set()
        assert not errors
        assert len(server.store.batches) == accepted_before
        assert live.dropped_batches > 0
        sent_before = live.sent_batches
        server.start()
        wait_for(lambda: live.sent_batches > sent_before)
        assert finished.wait(2.0)
    finally:
        thread.join(timeout=3.0)
        live.close()
    assert not thread.is_alive()
    assert not live._thread.is_alive()
    assert not errors
    assert len(server.store.batches) > accepted_before
    assert server.store.batches[-1].samples[-1].t > START + timedelta(seconds=1.5)
    assert live.failed_batches >= 1
    with (tmp_path / "raw.csv").open(newline="") as raw:
        rows = list(csv.DictReader(raw))
    assert len(rows) == len(scans) == 2000
    for i, (row, scan) in enumerate(zip(rows, scans)):
        assert float(row["t_s"]) == i / 1000
        assert datetime.fromisoformat(row["timestamp_utc"]) == scan[0].timestamp
        assert [float(row[r.channel]) for r in scan] == [r.raw_volts for r in scan]


def test_close_is_bounded_while_offline() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        live = WebSocketClient(
            f"ws://127.0.0.1:{port}", "offline", 1000, 1000,
            queue_size=1, batch_scans=1, timeout_s=0.1,
        )
        scan = MockSource(start_utc=START).generate(1)[0]
        live.write(scan)
        started = time.monotonic()
        live.close()
        assert time.monotonic() - started < 0.8
    wait_for(lambda: not live._thread.is_alive())
    assert live.dropped_batches == 1
    assert live.sent_batches == live.failed_batches == 0
    live.close()
    with pytest.raises(ValueError, match="closed"):
        live.write(scan)


def test_reconnect_backoff_is_exponential_and_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = []
    retried = Event()

    def offline(uri: str, **kwargs: object) -> NoReturn:
        attempts.append(time.monotonic())
        if len(attempts) >= 6:
            retried.set()
        raise OSError("offline")

    monkeypatch.setattr("daq.live.websocket.connect", offline)
    with WebSocketClient(
        "ws://ingest.test", "backoff", 1000, timeout_s=0.1,
        reconnect_delay_s=0.02, max_reconnect_delay_s=0.08,
    ):
        assert retried.wait(2.0)
    intervals = [second - first for first, second in zip(attempts[:5], attempts[1:6])]
    assert intervals == pytest.approx([0.02, 0.04, 0.08, 0.08, 0.08], abs=0.02)


def test_rejected_batch_is_counted_and_connection_stays_usable(
    server: IngestServer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    insert = server.store.insert_batch
    rejected = False

    def reject_once(batch: LiveBatch) -> None:
        nonlocal rejected
        if not rejected:
            rejected = True
            raise ValueError("not ready")
        insert(batch)

    monkeypatch.setattr(server.store, "insert_batch", reject_once)
    with WebSocketClient(
        server.url, "rejected", 1000, 1000, batch_scans=1,
    ) as live:
        for scan in MockSource(start_utc=START).generate(2):
            live.write(scan)
    assert live.failed_batches == 1
    assert live.sent_batches == 1
    assert live.dropped_batches == 0
    assert len(server.store.batches) == 1
    assert server.store.batches[0].samples[0].t == START + timedelta(milliseconds=1)


@pytest.mark.parametrize(("delay", "maximum"), [
    (0, 1), (-1, 1), (2, 1), (float("nan"), 1), (1, float("inf")),
])
def test_invalid_reconnect_delays(delay: float, maximum: float) -> None:
    with pytest.raises(ValueError, match="Reconnect delays"):
        WebSocketClient(
            "ws://ingest.test", "invalid", 1000,
            reconnect_delay_s=delay, max_reconnect_delay_s=maximum,
        )
