import fcntl
import logging
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum, auto
from pathlib import Path
from typing import Protocol

from subnet_tracker.core.db import transaction
from subnet_tracker.core.events import history_cutoff, prune_events
from subnet_tracker.core.requests import Claim, TickWork, claim_requests, finish_requests, strongest_reconnect
from subnet_tracker.core.settings import ApplySettings, ApplyStrategy
from subnet_tracker.core.settings_store import load_settings
from subnet_tracker.types import DomainId, PendingReconnect, ReconnectMode, Timestamp

# A full pass is many minutes, so tick resolves at most this many names and then serves whatever was queued.
RESOLVE_PORTION = 500
# The timer is minutely; a due time further out belongs to the next tick.
WAIT_LIMIT = 55
# Presence and the queue can move the due time, so the wait re-reads instead of sleeping until it.
POLL_SECONDS = 2
TICK_LOCK = Path("/run/subnet-tracker/tick.lock")

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReconnectState:
    pending_mode: PendingReconnect | None
    pending_since: Timestamp | None
    presence_until: Timestamp | None
    last_reconnect_at: Timestamp | None


@dataclass(frozen=True, slots=True)
class ResolveRequest:
    new_domains: frozenset[DomainId]
    manual_domains: frozenset[DomainId]
    resolve_all: bool


@dataclass(frozen=True, slots=True)
class Portion:
    taken: bool
    # False ends the portion loop: nothing was due, or the pass has no further portion
    more: bool


class ApplyKind(StrEnum):
    REQUEST = auto()
    PLAN = auto()


class ResolveStep(Protocol):
    def resolve_requested(self, conn: sqlite3.Connection, request: ResolveRequest, claim: Claim | None, now: Timestamp) -> None:
        """Resolve this request. Ids of deleted domains are already removed. Finish `claim` inside the transaction that stores the results when this step writes them."""

    def next_portion(self, conn: sqlite3.Connection, now: Timestamp) -> Portion:
        """Take the next scheduled portion, at most RESOLVE_PORTION names. Whether a full pass is due is the step's decision; tick does not read scheduler_state."""


class ApplyStep(Protocol):
    def apply(self, conn: sqlite3.Connection, kind: ApplyKind, now: Timestamp) -> bool:
        """Plan and write the routes file. Return whether routes were added. Do this outside a transaction: the file is not the database."""

    def commit(self, conn: sqlite3.Connection, mode: ReconnectMode | None, now: Timestamp) -> None:
        """Record the reconnect decision. None leaves reconnect_state unchanged; now reconnects and clears pending; auto and on_leave defer."""


class TickBusy(Exception):  # noqa: N818 — the lock is held, and the command exits successfully
    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(f"tick already running: {path}")


@dataclass(frozen=True, slots=True)
class TickReport:
    requests_finished: int
    portions: int
    reconnected: bool
    waited_seconds: int
    resolve_runs_pruned: int
    apply_runs_pruned: int
    events_pruned: int


class IdleResolve:
    def resolve_requested(self, conn: sqlite3.Connection, request: ResolveRequest, claim: Claim | None, now: Timestamp) -> None:
        return None

    def next_portion(self, conn: sqlite3.Connection, now: Timestamp) -> Portion:
        return Portion(False, False)


class IdleApply:
    def apply(self, conn: sqlite3.Connection, kind: ApplyKind, now: Timestamp) -> bool:
        return False

    def commit(self, conn: sqlite3.Connection, mode: ReconnectMode | None, now: Timestamp) -> None:
        return None


def reconnect_due_at(state: ReconnectState, settings: ApplySettings) -> Timestamp | None:
    match state.pending_mode:
        case None:
            return None
        case PendingReconnect.ON_LEAVE:
            return state.presence_until or state.pending_since
        case PendingReconnect.AUTO:
            window = int(state.last_reconnect_at or 0) + int(settings.min_interval)
            return Timestamp(max(window, int(state.presence_until or 0), int(state.pending_since or 0)))


def apply_mode(claimed: ReconnectMode | None, pending: ReconnectMode | None, routes_added: bool, *, scheduled: bool) -> ReconnectMode | None:
    # on_leave from this claim counts only after routes were actually added; an already pending mode is kept either way
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


def _timestamp(value: int | None) -> Timestamp | None:
    return None if value is None else Timestamp(value)


def read_reconnect(conn: sqlite3.Connection) -> ReconnectState:
    row = conn.execute("SELECT pending_mode, pending_since, presence_until, last_reconnect_at FROM reconnect_state WHERE id = 1").fetchone()
    if row is None:
        return ReconnectState(None, None, None, None)
    mode = None if row["pending_mode"] is None else PendingReconnect(row["pending_mode"])
    return ReconnectState(mode, _timestamp(row["pending_since"]), _timestamp(row["presence_until"]), _timestamp(row["last_reconnect_at"]))


def live_domain_ids(conn: sqlite3.Connection, domain_ids: frozenset[DomainId]) -> frozenset[DomainId]:
    if not domain_ids:
        return frozenset()
    found: set[DomainId] = set()
    ordered = tuple(domain_ids)
    for start in range(0, len(ordered), RESOLVE_PORTION):
        chunk = ordered[start : start + RESOLVE_PORTION]
        rows = conn.execute(f"SELECT id FROM domains WHERE id IN ({', '.join('?' * len(chunk))})", chunk)
        found.update(DomainId(row["id"]) for row in rows)
    return frozenset(found)


def _request(conn: sqlite3.Connection, work: TickWork) -> ResolveRequest:
    return ResolveRequest(live_domain_ids(conn, work.new_domains), live_domain_ids(conn, work.manual_domains), work.resolve_all)


def _log_invalid(claim: Claim | None) -> None:
    if claim is None:
        return
    for _, error in claim.invalid:
        logger.warning("Claim.invalid: %s", error)


def _as_mode(state: ReconnectState) -> ReconnectMode | None:
    return None if state.pending_mode is None else ReconnectMode(state.pending_mode)


def _carry(carried: ReconnectMode | None, mode: ReconnectMode | None) -> ReconnectMode | None:
    if mode not in (ReconnectMode.AUTO, ReconnectMode.ON_LEAVE):
        return carried
    return strongest_reconnect(item for item in (carried, mode) if item is not None)


def _decision(conn: sqlite3.Connection, claimed: ReconnectMode | None, routes_added: bool, *, scheduled: bool, carried: ReconnectMode | None) -> ReconnectMode | None:
    if load_settings(conn).apply.strategy == ApplyStrategy.NONE:
        return None
    pending = strongest_reconnect(item for item in (_as_mode(read_reconnect(conn)), carried) if item is not None)
    return apply_mode(claimed, pending, routes_added, scheduled=scheduled)


def _commit(conn: sqlite3.Connection, apply: ApplyStep, mode: ReconnectMode | None, claim: Claim | None, now: Timestamp) -> None:
    # The decision and the claim commit together: a crash retries the request instead of arming a reconnect for work that was rolled back.
    with transaction(conn):
        apply.commit(conn, mode, now)
        if claim is not None:
            finish_requests(conn, claim)


def _serve(
    conn: sqlite3.Connection,
    resolve: ResolveStep,
    apply: ApplyStep,
    clock: Callable[[], Timestamp],
    carried: ReconnectMode | None,
    requested_apply: bool,
    *,
    always: bool,
) -> tuple[int, bool, ReconnectMode | None, bool]:
    claim = claim_requests(conn)
    if claim is None and not always:
        return 0, False, carried, requested_apply
    _log_invalid(claim)
    work = claim.work if claim is not None else TickWork()
    counted = 0 if claim is None else int(conn.execute("SELECT count(*) FROM requests WHERE id <= ?", (claim.through,)).fetchone()[0])
    # Resolve and the routes file stay outside the write transaction; only the reconnect decision shares it with finish.
    resolve.resolve_requested(conn, _request(conn, work), claim, clock())
    if claim is not None and work.apply is not None:
        routes_added = apply.apply(conn, ApplyKind.REQUEST, clock())
        mode = _decision(conn, work.apply, routes_added, scheduled=False, carried=carried)
        _commit(conn, apply, mode, claim, clock())
        return counted, mode == ReconnectMode.NOW, _carry(carried, mode), True
    if claim is not None:
        with transaction(conn):
            finish_requests(conn, claim)
    return counted, False, carried, requested_apply


def _pause(clock: Callable[[], Timestamp], sleep: Callable[[int], None], delay: int) -> None:
    before = int(clock())
    sleep(delay)
    if int(clock()) <= before:
        raise RuntimeError("the tick clock did not advance during the reconnect wait")


def _wait(
    conn: sqlite3.Connection,
    resolve: ResolveStep,
    apply: ApplyStep,
    clock: Callable[[], Timestamp],
    sleep: Callable[[int], None],
    started: int,
    carried: ReconnectMode | None,
) -> tuple[int, bool, int]:
    waited = 0
    finished = 0
    reconnected = False
    while True:
        finished_now, reconnected_now, carried, _ = _serve(conn, resolve, apply, clock, carried, False, always=False)
        finished += finished_now
        reconnected = reconnected or reconnected_now
        settings = load_settings(conn)
        if settings.apply.strategy == ApplyStrategy.NONE:
            break
        due = reconnect_due_at(read_reconnect(conn), settings.apply)
        if due is None or int(due) > started + WAIT_LIMIT:
            break
        now = int(clock())
        if now >= int(due):
            _commit(conn, apply, ReconnectMode.NOW, None, clock())
            return waited, True, finished
        delay = min(POLL_SECONDS, int(due) - now)
        _pause(clock, sleep, delay)
        waited += delay
    return waited, reconnected, finished


def _prune(conn: sqlite3.Connection, now: Timestamp) -> tuple[int, int, int]:
    # Once a day would need scheduler_state.last_cleanup_at, which the scheduler owns. The cutoff is the retention rule, so every tick applies it.
    cutoff = history_cutoff(now, load_settings(conn).history.retention)
    with transaction(conn):
        runs = conn.execute("DELETE FROM resolve_runs WHERE started_at < ?", (cutoff,)).rowcount
        applies = conn.execute("DELETE FROM apply_runs WHERE started_at < ?", (cutoff,)).rowcount
        events = prune_events(conn, cutoff)
    return runs, applies, events


@contextmanager
def _exclusive(path: Path) -> Iterator[None]:
    # Append, so taking the lock does not truncate the file. A second tick finds the lock busy and leaves the queue for the one that holds it.
    handle = path.open("a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise TickBusy(path) from None
    try:
        yield
    finally:
        handle.close()


def run_tick(conn: sqlite3.Connection, *, resolve: ResolveStep, apply: ApplyStep, clock: Callable[[], Timestamp], sleep: Callable[[int], None], lock: Path) -> TickReport:
    with _exclusive(lock):
        started = int(clock())
        finished = 0
        portions = 0
        reconnected = False
        carried: ReconnectMode | None = None
        requested_apply = False
        while True:
            finished_now, reconnected_now, carried, requested_apply = _serve(conn, resolve, apply, clock, carried, requested_apply, always=True)
            finished += finished_now
            reconnected = reconnected or reconnected_now
            portion = resolve.next_portion(conn, clock())
            if portion.taken:
                portions += 1
            if not portion.more:
                break
        routes_added = apply.apply(conn, ApplyKind.PLAN, clock())
        # A portion can add addresses the request apply has not seen. Without one, the request's mode is the decision, so none is not turned into auto.
        mode = _decision(conn, None, routes_added, scheduled=portions > 0 or not requested_apply, carried=carried)
        _commit(conn, apply, mode, None, clock())
        reconnected = reconnected or mode == ReconnectMode.NOW
        waited, wait_reconnected, wait_finished = _wait(conn, resolve, apply, clock, sleep, started, _carry(carried, mode))
        finished += wait_finished
        reconnected = reconnected or wait_reconnected
        runs, applies, events = _prune(conn, clock())
    return TickReport(finished, portions, reconnected, waited, runs, applies, events)
