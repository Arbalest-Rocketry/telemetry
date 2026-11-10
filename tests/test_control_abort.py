from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
import time

import pytest

from daq.control.config import ControlConfig
from daq.control.fake_plc import FakePlc
from daq.control.plc import ValveClient
from daq.control.state import ControlError, Controller, State, Valve, mask
from daq.control.supervisor import AbortSupervisor
from daq.models import CHANNELS, PRESSURE_CHANNELS, Reading

START = datetime(2026, 11, 10, tzinfo=timezone.utc)
WINDOW_S = 0.25
ABORT_MASK = mask(Valve.LOX_DUMP, Valve.FUEL_DUMP, Valve.LOX_VENT, Valve.FUEL_VENT)


def volts(psi: float) -> float:
    return 0.472 + psi / 5000 * 1.888


def scan(t_s: float = 0.0, **overrides: float | None) -> tuple[Reading, ...]:
    timestamp = START + timedelta(seconds=t_s)
    return tuple(
        Reading(channel.name, timestamp, overrides.get(channel.name, volts(20.0)))
        for channel in CHANNELS
    )


class Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def controller() -> Controller:
    return Controller(ControlConfig(fault_window_s=WINDOW_S))


@pytest.fixture
def supervisor(controller: Controller, clock: Clock) -> AbortSupervisor:
    return AbortSupervisor(controller, clock=clock)


def feed(supervisor: AbortSupervisor, *scans: tuple[Reading, ...]) -> None:
    for item in scans:
        supervisor.offer(item)
    supervisor.step()


def test_defaults_watch_every_pt_at_4500_psi() -> None:
    config = ControlConfig()
    assert config.watch == tuple(channel.name for channel in PRESSURE_CHANNELS)
    assert config.abort_psi == 4500.0
    assert (config.fault_low_v, config.fault_high_v) == (0.1, 4.9)


def test_exactly_4500_psi_does_not_trip(controller: Controller, supervisor: AbortSupervisor) -> None:
    feed(supervisor, scan(pressure_1=volts(4500.0)))
    assert controller.state is State.IDLE
    assert controller.pressure_psi == 4500.0


@pytest.mark.parametrize("channel", [channel.name for channel in PRESSURE_CHANNELS])
def test_any_watched_pt_above_4500_psi_trips(
    controller: Controller, supervisor: AbortSupervisor, channel: str,
) -> None:
    controller.change_state(State.TEST)
    feed(supervisor, scan(**{channel: volts(4500.5)}))
    assert controller.state is State.ABORT
    assert controller.valves == ABORT_MASK
    assert channel in controller.abort_reason
    assert "4500 PSI" in controller.abort_reason


def test_one_overpressure_scan_in_a_batch_is_not_missed(
    controller: Controller, supervisor: AbortSupervisor,
) -> None:
    feed(supervisor, scan(0.000), scan(0.001, pressure_4=volts(4800.0)), scan(0.002))
    assert controller.state is State.ABORT


def test_unwatched_pt_does_not_trip(clock: Clock) -> None:
    controller = Controller(ControlConfig(watch=("pressure_1",)))
    supervisor = AbortSupervisor(controller, clock=clock)
    feed(supervisor, scan(pressure_2=volts(4900.0)), scan(1.0, pressure_2=None))
    assert controller.state is State.IDLE
    assert controller.pressure_psi == pytest.approx(20.0)


@pytest.mark.parametrize(("bad", "reason"), [
    (None, "read failed"),
    (float("nan"), "read failed"),
    (0.09, "fault band"),
    (4.91, "fault band"),
])
def test_bad_pt_reads_trip_only_after_the_window(
    controller: Controller, supervisor: AbortSupervisor, bad: float | None, reason: str,
) -> None:
    feed(supervisor, scan(0.0, pressure_3=bad), scan(WINDOW_S, pressure_3=bad))
    assert controller.state is State.IDLE
    assert controller.pressure_psi is None
    feed(supervisor, scan(WINDOW_S + 0.001, pressure_3=bad))
    assert controller.state is State.ABORT
    assert "pressure_3" in controller.abort_reason
    assert reason in controller.abort_reason


def test_voltages_at_the_fault_band_edges_are_readings(
    controller: Controller, supervisor: AbortSupervisor,
) -> None:
    feed(supervisor, scan(0.0, pressure_1=0.1), scan(1.0, pressure_1=0.1))
    assert controller.state is State.IDLE
    # 4.9 V is far past 4500 PSI, so it trips as overpressure, not as a fault.
    feed(supervisor, scan(1.001, pressure_1=4.9))
    assert "PSI >" in controller.abort_reason


def test_missing_channel_counts_as_a_failed_read(
    controller: Controller, supervisor: AbortSupervisor,
) -> None:
    feed(supervisor, scan(0.0)[1:], scan(WINDOW_S + 0.001)[1:])
    assert controller.state is State.ABORT
    assert "pressure_1 read failed" in controller.abort_reason


def test_a_good_read_restarts_the_fault_window(
    controller: Controller, supervisor: AbortSupervisor,
) -> None:
    feed(
        supervisor,
        scan(0.0, pressure_2=None), scan(0.2, pressure_2=None), scan(0.21),
        scan(0.3, pressure_2=None), scan(0.5, pressure_2=None),
    )
    assert controller.state is State.IDLE


def test_stale_readings_trip_after_the_window(
    controller: Controller, supervisor: AbortSupervisor, clock: Clock,
) -> None:
    feed(supervisor, scan())
    clock.now = WINDOW_S
    supervisor.step()
    assert controller.state is State.IDLE
    clock.now = WINDOW_S + 0.01
    supervisor.step()
    assert controller.state is State.ABORT
    assert "stale" in controller.abort_reason
    assert controller.pressure_psi is None


def test_steady_scans_are_not_stale(
    controller: Controller, supervisor: AbortSupervisor, clock: Clock,
) -> None:
    for i in range(10):
        clock.now = i * 0.2
        feed(supervisor, scan(i * 0.2))
    assert controller.state is State.IDLE


def test_load_cell_faults_do_not_trip(controller: Controller, supervisor: AbortSupervisor) -> None:
    feed(supervisor, scan(0.0, load_cell_1=None), scan(1.0, load_cell_1=None))
    assert controller.state is State.IDLE


def test_reset_guard_follows_the_highest_watched_pt(
    controller: Controller, supervisor: AbortSupervisor,
) -> None:
    feed(supervisor, scan(0.0, pressure_5=volts(4600.0)))
    assert controller.state is State.ABORT
    feed(supervisor, scan(0.1, pressure_5=volts(51.0)))
    with pytest.raises(ControlError, match="above 50 PSI"):
        controller.reset_abort()
    feed(supervisor, scan(0.2, pressure_5=volts(49.0)))
    controller.reset_abort()
    assert controller.state is State.SAFE


def test_latched_abort_leaves_manual_dump_toggles_alone(
    controller: Controller, supervisor: AbortSupervisor,
) -> None:
    feed(supervisor, scan(0.0, pressure_1=volts(4600.0)))
    controller.toggle(Valve.LOX_DUMP)
    feed(supervisor, scan(0.1, pressure_1=volts(4600.0)))
    assert not controller.valves[Valve.LOX_DUMP]


def wait_for(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 5
    while not condition():
        assert time.monotonic() < deadline, "Timed out waiting for abort"
        time.sleep(0.01)


@pytest.fixture
def stand() -> Iterator[tuple[FakePlc, ValveClient, Controller]]:
    plc = FakePlc()
    plc.start()
    client = ValveClient(ControlConfig(plc_port=plc.port), poll_s=0.05)
    controller = Controller(ControlConfig(fault_window_s=0.1), send=client.set_mask)
    yield plc, client, controller
    client.close()
    plc.stop()


def test_overpressure_drives_the_abort_pattern_onto_the_coils(
    stand: tuple[FakePlc, ValveClient, Controller],
) -> None:
    plc, client, controller = stand
    controller.change_state(State.TEST)
    wait_for(lambda: plc.coils == mask(Valve.LOX_RUN, Valve.FUEL_RUN))
    supervisor = AbortSupervisor(controller)
    supervisor.start()
    try:
        for i in range(50):
            supervisor.offer(scan(i * 0.001))
        supervisor.offer(scan(0.05, pressure_6=volts(4501.0)))
        wait_for(lambda: client.confirmed == ABORT_MASK)
    finally:
        supervisor.close()
    assert controller.state is State.ABORT
    assert plc.coils == (True, True, False, False, False, True, True)


def test_supervisor_thread_trips_when_scans_stop(
    stand: tuple[FakePlc, ValveClient, Controller],
) -> None:
    plc, client, controller = stand
    supervisor = AbortSupervisor(controller)
    supervisor.start()
    try:
        supervisor.offer(scan())
        wait_for(lambda: controller.state is State.ABORT)
        wait_for(lambda: plc.coils == ABORT_MASK)
    finally:
        supervisor.close()
    assert "stale" in controller.abort_reason
