import pytest

from daq.control.config import ControlConfig
from daq.control.state import ControlError, Controller, Mask, State, Valve

V = Valve


def open_valves(valves: Mask) -> set[Valve]:
    return {valve for valve in Valve if valves[valve]}


@pytest.fixture
def sent() -> list[Mask]:
    return []


@pytest.fixture
def controller(sent: list[Mask]) -> Controller:
    return Controller(send=sent.append)


def test_coil_offsets_match_the_stand() -> None:
    assert [(valve.name, valve.value) for valve in Valve] == [
        ("LOX_DUMP", 0), ("FUEL_DUMP", 1), ("LOX_FILL", 2), ("LOX_RUN", 3),
        ("FUEL_RUN", 4), ("LOX_VENT", 5), ("FUEL_VENT", 6),
    ]


def test_starts_idle_with_everything_closed(controller: Controller) -> None:
    assert controller.state is State.IDLE
    assert open_valves(controller.valves) == set()


@pytest.mark.parametrize(("state", "expected"), [
    (State.IDLE, set()),
    (State.FILLING, {V.LOX_FILL, V.LOX_VENT, V.FUEL_VENT}),
    (State.PRESSURIZATION, set()),
    (State.READY, set()),
    (State.TEST, {V.LOX_RUN, V.FUEL_RUN}),
    (State.SAFE, {V.LOX_DUMP, V.FUEL_DUMP, V.LOX_VENT, V.FUEL_VENT}),
    (State.ABORT, {V.LOX_DUMP, V.FUEL_DUMP, V.LOX_VENT, V.FUEL_VENT}),
])
def test_state_requests_its_valve_pattern(
    controller: Controller, sent: list[Mask], state: State, expected: set[Valve],
) -> None:
    # Start from a pattern that opens what the next state must close.
    controller.change_state(State.TEST if state is not State.TEST else State.FILLING)
    controller.change_state(state)
    assert controller.state is state
    assert open_valves(controller.valves) == expected
    assert sent[-1] == controller.valves


def test_abort_from_test_closes_run_and_fill_and_opens_dumps_and_vents(
    controller: Controller, sent: list[Mask],
) -> None:
    controller.change_state(State.TEST)
    controller.abort()
    assert controller.state is State.ABORT
    assert controller.abort_reason == "Emergency abort"
    assert open_valves(sent[-1]) == {V.LOX_DUMP, V.FUEL_DUMP, V.LOX_VENT, V.FUEL_VENT}


@pytest.mark.parametrize(
    "state", [s for s in State if s not in (State.SAFE, State.ABORT)],
)
def test_abort_only_leaves_to_safe(controller: Controller, sent: list[Mask], state: State) -> None:
    controller.pressure_psi = 0.0
    controller.abort()
    before = len(sent)
    with pytest.raises(ControlError, match="ABORT"):
        controller.change_state(state)
    assert controller.state is State.ABORT
    assert len(sent) == before
    controller.change_state(State.SAFE)
    assert controller.state is State.SAFE
    assert controller.abort_reason == ""


def test_reset_is_blocked_above_the_safe_pressure() -> None:
    controller = Controller(ControlConfig(reset_max_psi=50.0))
    controller.abort()
    controller.pressure_psi = 50.1
    with pytest.raises(ControlError, match="above 50 PSI"):
        controller.reset_abort()
    with pytest.raises(ControlError):
        controller.change_state(State.SAFE)
    assert controller.state is State.ABORT
    controller.pressure_psi = 50.0
    controller.reset_abort()
    assert controller.state is State.SAFE
    assert open_valves(controller.valves) == {V.LOX_DUMP, V.FUEL_DUMP, V.LOX_VENT, V.FUEL_VENT}


def test_reset_is_blocked_while_pressure_is_unknown(controller: Controller) -> None:
    controller.abort()
    assert controller.pressure_psi is None
    with pytest.raises(ControlError, match="unknown"):
        controller.reset_abort()
    assert controller.state is State.ABORT


def test_reset_threshold_comes_from_config() -> None:
    assert ControlConfig().reset_max_psi == 50.0
    controller = Controller(ControlConfig(reset_max_psi=200.0))
    controller.abort()
    controller.pressure_psi = 150.0
    controller.reset_abort()
    assert controller.state is State.SAFE


def test_reset_outside_abort_is_rejected(controller: Controller, sent: list[Mask]) -> None:
    with pytest.raises(ControlError):
        controller.reset_abort()
    assert controller.state is State.IDLE
    assert sent == []


def test_manual_toggle_keeps_the_state(controller: Controller, sent: list[Mask]) -> None:
    controller.toggle(V.FUEL_VENT)
    assert controller.state is State.IDLE
    assert open_valves(sent[-1]) == {V.FUEL_VENT}
    controller.toggle(V.FUEL_VENT)
    assert open_valves(sent[-1]) == set()


def test_opening_lox_run_closes_lox_fill(controller: Controller, sent: list[Mask]) -> None:
    controller.change_state(State.FILLING)
    controller.toggle(V.LOX_RUN)
    assert open_valves(sent[-1]) == {V.LOX_RUN, V.LOX_VENT, V.FUEL_VENT}


def test_lox_fill_cannot_open_while_lox_run_is_open(
    controller: Controller, sent: list[Mask],
) -> None:
    controller.change_state(State.TEST)
    controller.toggle(V.LOX_FILL)
    assert open_valves(sent[-1]) == {V.LOX_RUN, V.FUEL_RUN}


@pytest.mark.parametrize("valve", [V.LOX_RUN, V.FUEL_RUN, V.LOX_FILL])
def test_abort_blocks_run_and_fill_overrides(
    controller: Controller, sent: list[Mask], valve: Valve,
) -> None:
    controller.abort()
    before = len(sent)
    with pytest.raises(ControlError, match="locked"):
        controller.toggle(valve)
    assert not controller.valves[valve]
    assert len(sent) == before


@pytest.mark.parametrize("valve", [V.LOX_DUMP, V.FUEL_DUMP, V.LOX_VENT, V.FUEL_VENT])
def test_abort_still_allows_dump_and_vent_toggles(
    controller: Controller, sent: list[Mask], valve: Valve,
) -> None:
    controller.abort()
    controller.toggle(valve)
    assert not sent[-1][valve]
    assert controller.state is State.ABORT


def test_emergency_abort_reapplies_the_pattern(controller: Controller, sent: list[Mask]) -> None:
    controller.abort()
    controller.toggle(V.LOX_DUMP)
    controller.abort()
    assert sent[-1][V.LOX_DUMP]


def test_auto_abort_trips_once(controller: Controller, sent: list[Mask]) -> None:
    assert controller.auto_abort("pressure_1 4600 PSI")
    controller.toggle(V.LOX_DUMP)
    assert not controller.auto_abort("pressure_2 4700 PSI")
    assert controller.abort_reason == "pressure_1 4600 PSI"
    assert not sent[-1][V.LOX_DUMP]
