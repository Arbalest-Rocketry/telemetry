from collections.abc import Callable, Iterator
import time

import pytest

from daq.control.config import ControlConfig
from daq.control.fake_plc import FakePlc
from daq.control.plc import ValveClient
from daq.control.state import Controller, PATTERNS, State, Valve, mask

V = Valve
ABORT_MASK = mask(V.LOX_DUMP, V.FUEL_DUMP, V.LOX_VENT, V.FUEL_VENT)


def wait_for(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 5
    while not condition():
        assert time.monotonic() < deadline, "Timed out waiting for PLC"
        time.sleep(0.01)


@pytest.fixture
def plc() -> Iterator[FakePlc]:
    plc = FakePlc()
    plc.start()
    yield plc
    plc.stop()


@pytest.fixture
def client(plc: FakePlc) -> Iterator[ValveClient]:
    client = ValveClient(ControlConfig(plc_port=plc.port), poll_s=0.05)
    yield client
    client.close()


def test_default_plc_address_is_local() -> None:
    config = ControlConfig()
    assert (config.plc_host, config.plc_port, config.plc_unit) == ("127.0.0.1", 5020, 1)


def test_write_lands_on_coils_and_is_read_back(plc: FakePlc, client: ValveClient) -> None:
    wait_for(lambda: client.connected)
    assert client.confirmed == mask()
    wanted = mask(V.LOX_FILL, V.LOX_VENT, V.FUEL_VENT)
    client.set_mask(wanted)
    assert client.commanded == wanted
    wait_for(lambda: client.confirmed == wanted)
    assert plc.coils == (False, False, True, False, False, True, True)
    assert client.status == "PLC ok"


def test_wrong_unit_id_is_not_confirmed(plc: FakePlc) -> None:
    client = ValveClient(ControlConfig(plc_port=plc.port, plc_unit=2), poll_s=0.05)
    try:
        client.set_mask(ABORT_MASK)
        wait_for(lambda: "write failed" in client.status)
        assert client.confirmed is None
        assert plc.coils == mask()
    finally:
        client.close()


def test_stuck_valve_shows_commanded_differs_from_confirmed(
    plc: FakePlc, client: ValveClient,
) -> None:
    plc.stuck_closed = {V.LOX_DUMP}
    client.set_mask(ABORT_MASK)
    wait_for(lambda: client.status == "PLC readback mismatch")
    assert client.commanded == ABORT_MASK
    assert client.confirmed == mask(V.FUEL_DUMP, V.LOX_VENT, V.FUEL_VENT)


def test_mask_set_before_the_plc_is_up_is_sent_on_connect() -> None:
    plc = FakePlc()
    client = ValveClient(ControlConfig(plc_port=plc.port), poll_s=0.05)
    try:
        wanted = mask(V.LOX_RUN, V.FUEL_RUN)
        client.set_mask(wanted)
        wait_for(lambda: "write failed" in client.status)
        assert not client.connected
        assert client.confirmed is None
        plc.start()
        wait_for(lambda: client.confirmed == wanted)
        assert plc.coils == wanted
        assert client.status == "PLC ok"
    finally:
        client.close()
        plc.stop()


def test_reconnect_resends_the_current_mask(plc: FakePlc, client: ValveClient) -> None:
    client.set_mask(ABORT_MASK)
    wait_for(lambda: client.confirmed == ABORT_MASK)
    plc.stop()
    wait_for(lambda: not client.connected)
    assert client.confirmed is None
    assert client.status.startswith("PLC disconnected")
    plc.start()
    assert plc.coils == mask()
    wait_for(lambda: client.confirmed == ABORT_MASK)
    assert plc.coils == ABORT_MASK


def test_only_the_latest_mask_is_sent_after_an_outage(plc: FakePlc, client: ValveClient) -> None:
    wait_for(lambda: client.connected)
    plc.stop()
    wait_for(lambda: not client.connected)
    started = time.monotonic()
    client.set_mask(mask(V.LOX_RUN, V.FUEL_RUN))
    client.set_mask(ABORT_MASK)
    assert time.monotonic() - started < 0.05
    wait_for(lambda: "write failed" in client.status)
    plc.start()
    wait_for(lambda: client.confirmed == ABORT_MASK)
    assert plc.coils == ABORT_MASK
    assert client.status == "PLC ok"


def test_controller_states_reach_the_coils(plc: FakePlc, client: ValveClient) -> None:
    controller = Controller(send=client.set_mask)
    controller.change_state(State.TEST)
    wait_for(lambda: client.confirmed == PATTERNS[State.TEST])
    assert plc.coils == (False, False, False, True, True, False, False)
    controller.abort()
    wait_for(lambda: client.confirmed == ABORT_MASK)
    assert plc.coils == (True, True, False, False, False, True, True)
