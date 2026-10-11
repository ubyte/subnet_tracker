from collections.abc import Sequence
from dataclasses import dataclass

from subnet_tracker.core.plan_store import ReconnectState
from subnet_tracker.core.requests import strongest_reconnect
from subnet_tracker.core.settings import ApplySettings, ApplyStrategy
from subnet_tracker.types import PendingReconnect, ReconnectMode, Timestamp


# presence_until 0 is absent: the design formula uses `or`, so 0 falls through like a missing value.
def reconnect_due_at(state: ReconnectState, settings: ApplySettings) -> Timestamp | None:
    match state.pending_mode:
        case None:
            return None
        case PendingReconnect.ON_LEAVE:
            return state.presence_until or state.pending_since
        case PendingReconnect.AUTO:
            window = int(state.last_reconnect_at or 0) + int(settings.min_interval)
            return Timestamp(max(window, int(state.presence_until or 0), int(state.pending_since or 0)))


# A claimed on_leave counts only after routes were added. A deletion does not add a mode; an already pending one stays.
def reconnect_mode(claimed: ReconnectMode | None, pending: ReconnectMode | None, routes_added: bool, *, scheduled: bool) -> ReconnectMode | None:
    modes: list[ReconnectMode] = []
    if pending is not None:
        modes.append(pending)
    if claimed == ReconnectMode.ON_LEAVE:
        if routes_added:
            modes.append(claimed)
    elif claimed not in (None, ReconnectMode.NONE):
        modes.append(claimed)
    if scheduled and routes_added:
        modes.append(ReconnectMode.AUTO)
    return strongest_reconnect(modes)


def presence_after_activity(now: Timestamp, settings: ApplySettings) -> Timestamp:
    return Timestamp(int(now) + int(settings.idle_delay))


def presence_after_leave(now: Timestamp, settings: ApplySettings) -> Timestamp:
    return Timestamp(int(now) + int(settings.leave_grace))


@dataclass(frozen=True, slots=True)
class ClientReconnect:
    cid: int
    at: Timestamp


@dataclass(frozen=True, slots=True)
class ReconnectPlan:
    state: ReconnectState
    due_at: Timestamp | None
    clients: tuple[ClientReconnect, ...]
    reconnect: bool


def reconnect_plan(
    clients: Sequence[int],
    presence_until: Timestamp | None,
    unreconnected: int,
    now: Timestamp,
    state: ReconnectState,
    settings: ApplySettings,
    mode: ReconnectMode | None,
) -> ReconnectPlan:
    if settings.strategy == ApplyStrategy.NONE or mode in (None, ReconnectMode.NONE):
        return ReconnectPlan(state, None, (), False)
    if unreconnected <= 0:
        return ReconnectPlan(ReconnectState(None, None, presence_until, state.last_reconnect_at), None, (), False)
    if mode == ReconnectMode.NOW:
        return _reconnect_now(clients, presence_until, now, settings)
    return _defer(presence_until, now, state, settings, mode)


def _reconnect_now(clients: Sequence[int], presence_until: Timestamp | None, now: Timestamp, settings: ApplySettings) -> ReconnectPlan:
    step = int(settings.reconnect_spread)
    kills = tuple(ClientReconnect(cid, Timestamp(int(now) + index * step)) for index, cid in enumerate(clients))
    return ReconnectPlan(ReconnectState(None, None, presence_until, now), now, kills, True)


# auto and on_leave only record the deadline. Listing clients here as well as on the later now would reconnect them twice.
def _defer(presence_until: Timestamp | None, now: Timestamp, state: ReconnectState, settings: ApplySettings, mode: ReconnectMode) -> ReconnectPlan:
    pending = PendingReconnect(mode)
    since = now if state.pending_mode != pending or state.pending_since is None else state.pending_since
    deferred = ReconnectState(pending, since, presence_until, state.last_reconnect_at)
    return ReconnectPlan(deferred, reconnect_due_at(deferred, settings), (), False)
