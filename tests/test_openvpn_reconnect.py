import pytest

from subnet_tracker.core.plan_store import ReconnectState
from subnet_tracker.core.settings import ApplySettings, ApplyStrategy
from subnet_tracker.openvpn.reconnect import ClientReconnect, ReconnectPlan, presence_after_activity, presence_after_leave, reconnect_due_at, reconnect_mode, reconnect_plan
from subnet_tracker.types import PendingReconnect, ReconnectMode, Seconds, Timestamp

NONE, AUTO, ON_LEAVE, NOW_MODE = ReconnectMode.NONE, ReconnectMode.AUTO, ReconnectMode.ON_LEAVE, ReconnectMode.NOW
SETTINGS = ApplySettings(min_interval=Seconds(60), idle_delay=Seconds(30), leave_grace=Seconds(5), reconnect_spread=Seconds(2))


def state(mode: PendingReconnect | None, since: int | None, presence: int | None, last: int | None) -> ReconnectState:
    return ReconnectState(mode, None if since is None else Timestamp(since), None if presence is None else Timestamp(presence), None if last is None else Timestamp(last))


@pytest.mark.parametrize(
    ("pending", "due"),
    [
        (ReconnectState(), None),
        (state(PendingReconnect.ON_LEAVE, 10, 50, None), Timestamp(50)),
        (state(PendingReconnect.ON_LEAVE, 10, None, None), Timestamp(10)),
        (state(PendingReconnect.ON_LEAVE, 10, 0, None), Timestamp(10)),
        (state(PendingReconnect.ON_LEAVE, 0, None, None), Timestamp(0)),
        (state(PendingReconnect.ON_LEAVE, 10, 50, 10_000), Timestamp(50)),
        (state(PendingReconnect.AUTO, 100, None, 100), Timestamp(160)),
        (state(PendingReconnect.AUTO, 100, 0, 100), Timestamp(160)),
        (state(PendingReconnect.AUTO, 100, 500, 100), Timestamp(500)),
        (state(PendingReconnect.AUTO, 1000, None, None), Timestamp(1000)),
        (state(PendingReconnect.AUTO, 1000, 100, None), Timestamp(1000)),
    ],
)
def test_reconnect_due_at(pending: ReconnectState, due: Timestamp | None) -> None:
    assert reconnect_due_at(pending, ApplySettings(min_interval=Seconds(60))) == due


def test_on_leave_due_does_not_move_when_min_interval_changes() -> None:
    pending = state(PendingReconnect.ON_LEAVE, 10, 50, 10_000)
    short = reconnect_due_at(pending, ApplySettings(min_interval=Seconds(1)))
    long = reconnect_due_at(pending, ApplySettings(min_interval=Seconds(100_000)))
    assert short == long == Timestamp(50)


@pytest.mark.parametrize(
    ("claimed", "pending_mode", "routes_added", "scheduled", "mode"),
    [
        (None, None, False, True, None),
        (None, None, True, True, AUTO),
        (None, None, True, False, None),
        (ON_LEAVE, None, False, False, None),
        (ON_LEAVE, None, True, False, ON_LEAVE),
        (ON_LEAVE, AUTO, False, False, AUTO),
        (ON_LEAVE, AUTO, True, False, ON_LEAVE),
        (AUTO, ON_LEAVE, False, False, ON_LEAVE),
        (NOW_MODE, ON_LEAVE, False, False, NOW_MODE),
        (NONE, None, True, False, None),
        (NONE, ON_LEAVE, True, False, ON_LEAVE),
        (NONE, None, True, True, AUTO),
        (AUTO, None, False, False, AUTO),
        (None, ON_LEAVE, False, True, ON_LEAVE),
        (None, ON_LEAVE, True, True, ON_LEAVE),
    ],
)
def test_reconnect_mode(claimed: ReconnectMode | None, pending_mode: ReconnectMode | None, routes_added: bool, scheduled: bool, mode: ReconnectMode | None) -> None:
    assert reconnect_mode(claimed, pending_mode, routes_added, scheduled=scheduled) == mode


def test_a_deletion_does_not_schedule_reconnect() -> None:
    assert reconnect_mode(None, None, False, scheduled=True) is None
    assert reconnect_mode(None, None, False, scheduled=False) is None
    held = state(PendingReconnect.AUTO, 40, 90, 10)
    plan = reconnect_plan((1,), Timestamp(1), 4, Timestamp(100), held, SETTINGS, None)
    assert plan == ReconnectPlan(held, None, (), False)


def test_after_add_does_not_choose_the_mode() -> None:
    now = Timestamp(10)
    left = reconnect_plan((), None, 1, now, ReconnectState(), ApplySettings(after_add=NOW_MODE), ON_LEAVE)
    right = reconnect_plan((), None, 1, now, ReconnectState(), ApplySettings(after_add=AUTO), ON_LEAVE)
    assert left == right
    assert left.state.pending_mode is PendingReconnect.ON_LEAVE


def test_activity_extends_presence_and_leave_shortens_it_to_grace() -> None:
    now = Timestamp(1_000)
    settings = ApplySettings(idle_delay=Seconds(30), leave_grace=Seconds(5))
    assert presence_after_activity(now, settings) == Timestamp(1_030)
    assert presence_after_leave(now, settings) == Timestamp(1_005)
    assert presence_after_activity(Timestamp(1_010), settings) == Timestamp(1_040)


def test_a_page_transition_does_not_reconnect() -> None:
    now = Timestamp(1_000)
    settings = ApplySettings(idle_delay=Seconds(30), leave_grace=Seconds(5), min_interval=Seconds(3_600))
    held = state(PendingReconnect.ON_LEAVE, 900, 2_000, None)
    left = presence_after_leave(now, settings)
    plan = reconnect_plan((1, 2), left, 3, now, held, settings, ON_LEAVE)
    assert (left, plan.clients, plan.reconnect, plan.due_at) == (Timestamp(1_005), (), False, left)
    arrived = Timestamp(1_001)
    extended = presence_after_activity(arrived, settings)
    stayed = reconnect_plan((1, 2), extended, 3, arrived, plan.state, settings, ON_LEAVE)
    assert (extended, stayed.due_at, stayed.clients, stayed.state.pending_since) == (Timestamp(1_031), extended, (), Timestamp(900))


def test_now_reconnects_every_client_immediately() -> None:
    now = Timestamp(9_100)
    held = state(PendingReconnect.ON_LEAVE, 1, 50_000, 9_000)
    settings = ApplySettings(min_interval=Seconds(100_000), reconnect_spread=Seconds(5))
    plan = reconnect_plan((7, 3, 7), Timestamp(50_000), 2, now, held, settings, NOW_MODE)
    assert plan.reconnect
    assert plan.due_at == now
    assert plan.clients == (ClientReconnect(7, now), ClientReconnect(3, Timestamp(9_105)), ClientReconnect(7, Timestamp(9_110)))
    assert plan.state == ReconnectState(None, None, Timestamp(50_000), now)


def test_now_with_no_connected_clients_still_finishes() -> None:
    now = Timestamp(100)
    plan = reconnect_plan((), None, 1, now, ReconnectState(), SETTINGS, NOW_MODE)
    assert plan == ReconnectPlan(ReconnectState(None, None, None, now), now, (), True)


def test_a_deferred_mode_does_not_kill_when_the_deadline_has_passed() -> None:
    now = Timestamp(100)
    plan = reconnect_plan((5,), Timestamp(20), 1, now, state(PendingReconnect.ON_LEAVE, 10, 20, None), SETTINGS, ON_LEAVE)
    assert plan.clients == ()
    assert plan.reconnect is False
    assert plan.due_at == Timestamp(20)
    assert plan.state == ReconnectState(PendingReconnect.ON_LEAVE, Timestamp(10), Timestamp(20), None)


def test_on_leave_without_presence_is_due_at_pending_since() -> None:
    now = Timestamp(100)
    plan = reconnect_plan((5,), Timestamp(0), 1, now, ReconnectState(), SETTINGS, ON_LEAVE)
    assert plan.clients == ()
    assert plan.due_at == now
    assert plan.state == ReconnectState(PendingReconnect.ON_LEAVE, now, Timestamp(0), None)
    assert plan.due_at == reconnect_due_at(plan.state, SETTINGS)


def test_auto_waits_for_the_window_and_for_presence() -> None:
    now = Timestamp(1_100)
    presence = Timestamp(1_500)
    settings = ApplySettings(min_interval=Seconds(3_600))
    window = reconnect_plan((9,), presence, 1, now, ReconnectState(last_reconnect_at=Timestamp(1_000)), settings, AUTO)
    assert window.clients == ()
    assert window.reconnect is False
    assert window.state == ReconnectState(PendingReconnect.AUTO, now, presence, Timestamp(1_000))
    assert window.due_at == Timestamp(4_600) == reconnect_due_at(window.state, settings)
    later_presence = Timestamp(9_000)
    waiting = reconnect_plan((9,), later_presence, 1, now, ReconnectState(last_reconnect_at=Timestamp(1_000)), ApplySettings(min_interval=Seconds(60)), AUTO)
    assert waiting.due_at == later_presence


def test_a_stronger_mode_starts_its_own_pending_since() -> None:
    now = Timestamp(500)
    plan = reconnect_plan((1,), Timestamp(800), 1, now, state(PendingReconnect.AUTO, 100, 200, 50), SETTINGS, ON_LEAVE)
    assert plan.state == ReconnectState(PendingReconnect.ON_LEAVE, now, Timestamp(800), Timestamp(50))
    assert plan.due_at == Timestamp(800)
    assert plan.clients == ()


def test_no_unreconnected_routes_drops_the_deferral_without_killing() -> None:
    plan = reconnect_plan((8, 9), Timestamp(90), 0, Timestamp(100), state(PendingReconnect.ON_LEAVE, 40, 90, 10), SETTINGS, ON_LEAVE)
    assert plan == ReconnectPlan(ReconnectState(None, None, Timestamp(90), Timestamp(10)), None, (), False)
    immediate = reconnect_plan((8,), Timestamp(90), 0, Timestamp(100), state(PendingReconnect.AUTO, 40, 90, 10), SETTINGS, NOW_MODE)
    assert immediate == ReconnectPlan(ReconnectState(None, None, Timestamp(90), Timestamp(10)), None, (), False)


def test_strategy_none_leaves_pending_in_place() -> None:
    held = state(PendingReconnect.AUTO, 40, None, 10)
    plan = reconnect_plan((1, 2), Timestamp(1), 0, Timestamp(100), held, ApplySettings(strategy=ApplyStrategy.NONE), NOW_MODE)
    assert plan == ReconnectPlan(held, None, (), False)


def test_none_does_not_change_pending() -> None:
    held = state(PendingReconnect.AUTO, 40, 90, 10)
    plan = reconnect_plan((1,), Timestamp(1), 3, Timestamp(100), held, SETTINGS, NONE)
    assert plan == ReconnectPlan(held, None, (), False)
