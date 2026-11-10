from collections.abc import Iterator
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from typing import Any

from fastapi.testclient import TestClient
import httpx
import pytest

from daq.live.client import LiveClient
from daq.models import PRESSURE_CHANNELS, Channel, Reading
from ingest.app import create_app
from ingest.db import InMemoryStore


@pytest.fixture
def batch() -> dict[str, Any]:
    return {
        "contract_version": 1,
        "source": "daq",
        "run_id": "test-run",
        "channels": {
            "pressure_1": {"unit": "PSI", "calibration_id": "pt2400-v1"},
            "load_cell_1": {"unit": "V", "calibration_id": None},
        },
        "samples": [
            {"t": "2026-11-10T20:00:00Z", "channel": "pressure_1", "value": 6000.0},
            {"t": "2026-11-10T20:00:00+00:00", "channel": "load_cell_1", "value": None},
            {"t": "2026-11-10T20:00:01Z", "channel": "pressure_1", "value": 0},
        ],
    }


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def client(store: InMemoryStore) -> Iterator[TestClient]:
    with TestClient(create_app(store)) as client:
        yield client


def test_post_and_websocket_store_the_same_batch(
    client: TestClient, store: InMemoryStore, batch: dict[str, Any],
) -> None:
    response = client.post("/ingest", json=batch)
    assert response.status_code == 200
    assert response.json() == {"accepted": 3}
    with client.websocket_connect("/ingest/ws") as websocket:
        websocket.send_json(batch)
        assert websocket.receive_json() == response.json()
    assert len(store.batches) == 2
    first, second = store.batches
    assert first == second
    assert first.source == "daq"
    assert first.run_id == "test-run"
    assert first.channels["pressure_1"].unit == "PSI"
    assert first.channels["pressure_1"].calibration_id == "pt2400-v1"
    assert first.channels["load_cell_1"].calibration_id is None
    assert [sample.value for sample in first.samples] == [6000.0, None, 0]
    assert [sample.channel for sample in first.samples] == [
        "pressure_1", "load_cell_1", "pressure_1",
    ]
    assert first.samples[0].t == datetime(2026, 11, 10, 20, tzinfo=timezone.utc)
    assert first.samples[-1].t == first.samples[0].t + timedelta(seconds=1)


@pytest.mark.parametrize(("path", "value", "error"), [
    (("contract_version",), 2, "contract_version"),
    (("contract_version",), True, "contract_version"),
    (("contract_version",), 1.0, "contract_version"),
    (("source",), "DAQ", "source"),
    (("source",), "bad source", "source"),
    (("run_id",), "", "run_id"),
    (("run_id",), None, "run_id"),
    (("channels",), [], "channels"),
    (("channels",), {}, "channels"),
    (("channels",), {"Pressure": {}}, "channel"),
    (("channels", "pressure_1"), None, "channels.pressure_1"),
    (("channels", "pressure_1", "unit"), "", "unit"),
    (("channels", "pressure_1", "calibration_id"), 123, "calibration_id"),
    (("samples",), [], "samples"),
    (("samples",), {}, "samples"),
    (("samples", 1), None, "sample"),
    (("samples", 1, "channel"), "unknown", "missing from channels"),
    (("samples", 1, "t"), "2026-11-10T20:00:00", "UTC"),
    (("samples", 1, "t"), "2026-11-10T15:00:00-05:00", "UTC"),
    (("samples", 1, "t"), "bad timestamp", "timestamp"),
    (("samples", 1, "t"), 123, "sample.t"),
    (("samples", 1, "value"), "1.0", "finite number or null"),
    (("samples", 1, "value"), True, "finite number or null"),
    (("samples", 1, "value"), float("nan"), "finite number or null"),
    (("samples", 1, "value"), float("inf"), "finite number or null"),
])
def test_invalid_batches_are_atomic_and_websocket_stays_usable(
    client: TestClient, store: InMemoryStore, batch: dict[str, Any],
    path: tuple[str | int, ...], value: object, error: str,
) -> None:
    invalid = deepcopy(batch)
    target = invalid
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    payload = json.dumps(invalid)
    response = client.post("/ingest", content=payload, headers={"content-type": "application/json"})
    assert response.status_code == 422
    assert error in response.json()["detail"]
    with client.websocket_connect("/ingest/ws") as websocket:
        websocket.send_text(payload)
        assert error in websocket.receive_json()["error"]
        assert store.batches == []
        websocket.send_json(batch)
        assert websocket.receive_json() == {"accepted": 3}
    assert len(store.batches) == 1


@pytest.mark.parametrize("payload", ["{", "null", "[]", "{}"])
def test_malformed_messages_allow_websocket_recovery(
    client: TestClient, store: InMemoryStore, batch: dict[str, Any], payload: str,
) -> None:
    assert client.post("/ingest", content=payload).status_code == 422
    with client.websocket_connect("/ingest/ws") as websocket:
        websocket.send_text(payload)
        assert websocket.receive_json()["error"]
        assert store.batches == []
        websocket.send_json(batch)
        assert websocket.receive_json() == {"accepted": 3}


@pytest.mark.parametrize("path", [
    ("contract_version",), ("source",), ("run_id",), ("channels",), ("samples",),
    ("channels", "pressure_1", "unit"), ("channels", "pressure_1", "calibration_id"),
    ("samples", 1, "t"), ("samples", 1, "channel"), ("samples", 1, "value"),
])
def test_required_fields(
    client: TestClient, store: InMemoryStore, batch: dict[str, Any],
    path: tuple[str | int, ...],
) -> None:
    target = batch
    for key in path[:-1]:
        target = target[key]
    del target[path[-1]]
    assert client.post("/ingest", json=batch).status_code == 422
    assert store.batches == []


@pytest.mark.parametrize(("field", "value"), [("unit", "bar"), ("calibration_id", None)])
def test_metadata_stays_fixed_across_transports(
    client: TestClient, store: InMemoryStore, batch: dict[str, Any], field: str, value: str | None,
) -> None:
    assert client.post("/ingest", json=batch).status_code == 200
    changed = deepcopy(batch)
    changed["channels"]["pressure_1"][field] = value
    with client.websocket_connect("/ingest/ws") as websocket:
        websocket.send_json(changed)
        assert "changed" in websocket.receive_json()["error"]
        assert len(store.batches) == 1
        websocket.send_json(batch)
        assert websocket.receive_json() == {"accepted": 3}
    assert client.post("/ingest", json=changed).status_code == 422
    changed["run_id"] = "next-run"
    assert client.post("/ingest", json=changed).status_code == 200
    changed["run_id"] = batch["run_id"]
    changed["source"] = "other"
    assert client.post("/ingest", json=changed).status_code == 200
    assert len(store.batches) == 4


def test_daq_client_posts_to_app(client: TestClient, store: InMemoryStore) -> None:
    def receive(request: httpx.Request) -> httpx.Response:
        return client.post(
            request.url.path, content=request.content, headers=request.headers,
        )

    start = datetime(2026, 11, 10, 20, tzinfo=timezone.utc)
    channels = (PRESSURE_CHANNELS[0], Channel("load_cell_1", "load_cell"))
    with LiveClient(
        "http://ingest.test", "daq-run", 1000, 500, channels,
        transport=httpx.MockTransport(receive),
    ) as live:
        for i, volts in enumerate([0.472, 2.360, 2.360, 2.360]):
            timestamp = start + timedelta(milliseconds=i)
            live.write((
                Reading("pressure_1", timestamp, volts),
                Reading("load_cell_1", timestamp, None),
            ))
    assert live.sent_batches == 1
    assert live.failed_batches == live.dropped_batches == 0
    assert len(store.batches) == 1
    batch = store.batches[0]
    assert batch.source == "daq"
    assert batch.run_id == "daq-run"
    assert [sample.value for sample in batch.samples] == [
        pytest.approx(2500.0), None, pytest.approx(5000.0), None,
    ]
    assert batch.samples[0].t == start + timedelta(milliseconds=0.5)
    assert batch.samples[2].t == start + timedelta(milliseconds=2.5)
    assert batch.channels["pressure_1"].unit == "PSI"
    assert batch.channels["load_cell_1"].unit == "V"
