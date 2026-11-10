import csv
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import subprocess
import sys
from threading import Event, Thread
import time

import httpx
import pytest

from daq.acquire.mock import MockSource
from daq.live.client import LiveClient
from daq.live.downsample import Downsampler
from daq.log.writer import DiskLogger
from daq.models import PRESSURE_CHANNELS, Channel, Reading


START = datetime(2026, 11, 10, 20, tzinfo=timezone.utc)
CHANNELS = (PRESSURE_CHANNELS[0], Channel("load_cell_1", "load_cell"))


def test_downsampling_averages_separate_windows() -> None:
    downsampler = Downsampler(1000, 250, CHANNELS)
    output = []
    for i in range(10):
        timestamp = START + timedelta(milliseconds=i)
        averaged = downsampler.add((
            Reading("pressure_1", timestamp, float(i)),
            Reading("load_cell_1", timestamp, float(i * 2)),
        ))
        if averaged is not None:
            output.append(averaged)
    assert [[r.raw_volts for r in scan] for scan in output] == [[1.5, 3.0], [5.5, 11.0]]
    assert [scan[0].timestamp for scan in output] == [
        START + timedelta(milliseconds=1.5), START + timedelta(milliseconds=5.5),
    ]


@pytest.mark.parametrize("rate", [20, 25, 30, 50, 1000])
def test_live_rate_never_exceeds_requested_rate(rate: float) -> None:
    downsampler = Downsampler(1000, rate)
    output = [
        averaged for scan in MockSource(start_utc=START).generate(1000)
        if (averaged := downsampler.add(scan)) is not None
    ]
    assert 0 < len(output) <= rate
    assert len(output) >= rate - 1
    for first, second in zip(output, output[1:]):
        assert (second[0].timestamp - first[0].timestamp).total_seconds() >= 1 / rate


@pytest.mark.parametrize("missing", [None, float("nan"), float("inf")])
def test_downsampling_preserves_missing_channels(missing: float | None) -> None:
    downsampler = Downsampler(1000, 500, CHANNELS)
    assert downsampler.add((
        Reading("pressure_1", START, missing), Reading("load_cell_1", START, 1.0),
    )) is None
    averaged = downsampler.add((
        Reading("pressure_1", START, 2.360), Reading("load_cell_1", START, 3.0),
    ))
    assert averaged is not None
    assert [r.raw_volts for r in averaged] == [None, 2.0]


@pytest.mark.parametrize("rate", [0, -1, 1001, float("inf"), float("nan")])
def test_invalid_live_rate(rate: float) -> None:
    with pytest.raises(ValueError, match="live_rate_hz"):
        Downsampler(1000, rate)


def test_live_payload_matches_contract() -> None:
    requests = []

    def receive(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    channels = (*CHANNELS, Channel("missing", "load_cell"))
    with LiveClient(
        "http://ingest.test/", "test-run", 1000, 500, channels,
        transport=httpx.MockTransport(receive),
    ) as live:
        for i in range(4):
            timestamp = START + timedelta(milliseconds=i)
            live.write((
                Reading("pressure_1", timestamp, [0.472, 2.360, 2.360, 2.360][i]),
                Reading("load_cell_1", timestamp, float(i)),
                Reading("missing", timestamp, None),
            ))
    assert live.sent_batches == 1
    assert live.failed_batches == live.dropped_batches == 0
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == "http://ingest.test/ingest"
    assert request.headers["content-type"] == "application/json"
    message = json.loads(request.content)
    assert message == {
        "contract_version": 1,
        "source": "daq",
        "run_id": "test-run",
        "channels": {
            "pressure_1": {"unit": "PSI", "calibration_id": "pt2400-v1"},
            "load_cell_1": {"unit": "V", "calibration_id": None},
            "missing": {"unit": "V", "calibration_id": None},
        },
        "samples": [
            {"t": (START + timedelta(milliseconds=0.5)).isoformat(),
             "channel": "pressure_1", "value": pytest.approx(2500.0)},
            {"t": (START + timedelta(milliseconds=0.5)).isoformat(),
             "channel": "load_cell_1", "value": 0.5},
            {"t": (START + timedelta(milliseconds=0.5)).isoformat(),
             "channel": "missing", "value": None},
            {"t": (START + timedelta(milliseconds=2.5)).isoformat(),
             "channel": "pressure_1", "value": pytest.approx(5000.0)},
            {"t": (START + timedelta(milliseconds=2.5)).isoformat(),
             "channel": "load_cell_1", "value": 2.5},
            {"t": (START + timedelta(milliseconds=2.5)).isoformat(),
             "channel": "missing", "value": None},
        ],
    }


def test_full_queue_drops_without_delaying_disk_log(tmp_path: Path) -> None:
    entered = Event()
    release = Event()
    messages = []

    def stalled(request: httpx.Request) -> httpx.Response:
        messages.append(json.loads(request.content))
        entered.set()
        assert release.wait(5.0)
        return httpx.Response(204)

    scans = MockSource(start_utc=START).generate(2002)
    live = LiveClient(
        "http://ingest.test", "stalled", 1000, 1000, queue_size=1, batch_scans=1,
        transport=httpx.MockTransport(stalled),
    )
    try:
        with DiskLogger(tmp_path, "stalled", START, 1000) as logger:
            logger.write(scans[0])
            live.write(scans[0])
            assert entered.wait(2.0)
            started = time.monotonic()
            for scan in scans[1:]:
                logger.write(scan)
                live.write(scan)
            assert time.monotonic() - started < 1.0
        assert live.dropped_batches == 2000
        with (tmp_path / "raw.csv").open(newline="") as raw:
            rows = list(csv.DictReader(raw))
        assert len(rows) == 2002
        assert float(rows[-1]["t_s"]) == 2.001
        assert float(rows[-1]["pressure_1"]) == scans[-1][0].raw_volts
    finally:
        release.set()
        live.close()
    assert live.sent_batches == 2
    assert [message["samples"][0]["t"] for message in messages] == [
        scans[0][0].timestamp.isoformat(), scans[-1][0].timestamp.isoformat(),
    ]


@pytest.mark.parametrize("failure", ["timeout", "connection", "status"])
def test_failed_batches_are_counted_and_sender_continues(failure: str) -> None:
    calls = 0

    def receive(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout("slow ingest", request=request)
            if failure == "connection":
                raise httpx.ConnectError("offline", request=request)
            return httpx.Response(503)
        return httpx.Response(204)

    with LiveClient(
        "http://ingest.test", "errors", 1000, 1000, batch_scans=1,
        transport=httpx.MockTransport(receive),
    ) as live:
        for scan in MockSource(start_utc=START).generate(2):
            live.write(scan)
    assert live.failed_batches == 1
    assert live.sent_batches == 1
    assert live.dropped_batches == 0


def test_close_is_bounded_with_stalled_sender() -> None:
    entered = Event()
    release = Event()
    finished = Event()

    def stalled(request: httpx.Request) -> httpx.Response:
        entered.set()
        try:
            assert release.wait(5.0)
            return httpx.Response(204)
        finally:
            finished.set()

    scans = MockSource(start_utc=START).generate(2)
    live = LiveClient(
        "http://ingest.test", "shutdown", 1000, 1000, batch_scans=1, timeout_s=0.1,
        transport=httpx.MockTransport(stalled),
    )
    try:
        live.write(scans[0])
        assert entered.wait(2.0)
        live.write(scans[1])
        started = time.monotonic()
        live.close()
        assert time.monotonic() - started < 0.8
        assert live.dropped_batches == 1
        live.close()
        with pytest.raises(ValueError, match="closed"):
            live.write(scans[0])
    finally:
        release.set()
        live.close()
        assert finished.wait(2.0)


def test_record_cli_with_live_ingest(tmp_path: Path) -> None:
    messages = []
    paths = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            paths.append(self.path)
            messages.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(204)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            result = subprocess.run(
                [sys.executable, "-m", "daq.record", "--mock", "--seconds", "0.2",
                 "--out", str(tmp_path), "--ingest-url",
                 f"http://127.0.0.1:{server.server_port}", "--live-rate-hz", "50"],
                check=True, capture_output=True, text=True, timeout=10,
            )
        finally:
            server.shutdown()
            thread.join(timeout=2.0)
    run_dir = Path(result.stdout.strip())
    meta = json.loads((run_dir / "meta.json").read_text())
    with (run_dir / "raw.csv").open(newline="") as raw:
        assert len(list(csv.DictReader(raw))) == 200
    assert paths == ["/ingest", "/ingest"]
    assert all(message["run_id"] == meta["run_id"] for message in messages)
    assert all(len(message["samples"]) == 5 * 11 for message in messages)
    assert messages[0]["channels"] == {
        name: {"unit": channel["unit"], "calibration_id": channel["calibration_id"]}
        for name, channel in meta["channels"].items()
    }
