from datetime import datetime, timedelta, timezone
from itertools import islice
from threading import Event, Thread
import time

import pytest

from daq.acquire.mock import MockSource
from daq.models import CHANNELS, PRESSURE_CHANNELS


START = datetime(2026, 11, 10, 20, tzinfo=timezone.utc)


def test_mock_scan_rate_and_channels() -> None:
    source = MockSource(seed=42, start_utc=START)
    scans = source.generate(1001)
    assert source.sample_rate_hz == 1000
    assert len(scans) == 1001
    for i, scan in enumerate(scans):
        assert len(scan) == 11
        assert tuple(reading.channel for reading in scan) == tuple(c.name for c in CHANNELS)
        assert {reading.timestamp for reading in scan} == {START + timedelta(milliseconds=i)}
    assert scans[-1][0].timestamp - scans[0][0].timestamp == timedelta(seconds=1)


def test_mock_voltages() -> None:
    scans = MockSource(seed=42, start_utc=START).generate(4000)
    pressure_names = {channel.name for channel in PRESSURE_CHANNELS}
    for scan in scans:
        for reading in scan:
            assert reading.raw_volts is not None
            if reading.channel in pressure_names:
                assert 0.472 <= reading.raw_volts <= 2.360
            else:
                assert 1.25 <= reading.raw_volts <= 3.26
    for i in range(11):
        assert scans[0][i].raw_volts != scans[-1][i].raw_volts


def test_mock_seed_and_continuous_generation() -> None:
    source = MockSource(seed=42, start_utc=START)
    scans = source.generate(10) + source.generate(10)
    assert scans == MockSource(seed=42, start_utc=START).generate(20)
    assert scans != MockSource(seed=43, start_utc=START).generate(20)


def test_mock_empty_and_negative_count() -> None:
    source = MockSource(seed=42, start_utc=START)
    assert source.generate(0) == []
    with pytest.raises(ValueError, match="nonnegative"):
        source.generate(-1)
    assert source.generate(1)[0][0].timestamp == START


def test_mock_requires_aware_start() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        MockSource(start_utc=datetime(2026, 11, 10))


def test_mock_normalizes_start_to_utc() -> None:
    local = START.astimezone(timezone(timedelta(hours=-5)))
    reading = MockSource(start_utc=local).generate(1)[0][0]
    assert reading.timestamp == START
    assert reading.timestamp.tzinfo is timezone.utc


def test_mock_stream_is_paced_and_matches_synchronous_samples() -> None:
    source = MockSource(seed=42, start_utc=START)
    first = source.generate(3)
    start = time.monotonic()
    scans = list(islice(source.stream(), 101))
    elapsed = time.monotonic() - start
    assert 0.09 <= elapsed < 2.0
    assert first + scans == MockSource(seed=42, start_utc=START).generate(104)


def test_mock_stream_stops_from_another_thread() -> None:
    stop = Event()
    started = Event()
    source = MockSource(seed=42, start_utc=START)

    def consume() -> None:
        for _ in source.stream(stop):
            started.set()

    thread = Thread(target=consume, daemon=True)
    thread.start()
    try:
        assert started.wait(2.0)
    finally:
        stop.set()
        thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert list(source.stream(stop)) == []
