import fcntl
import logging
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from cli_world import Cli, Result
from db_rows import ROWS, insert
from domains_world import count
from subnet_tracker.cli.command import Runtime
from subnet_tracker.core.requests import Apply, Claim, ResolveAll, ResolveDomains, enqueue
from subnet_tracker.core.settings import ApplySettings
from subnet_tracker.core.settings_store import set_setting
from subnet_tracker.tick import POLL_SECONDS, RESOLVE_PORTION, TICK_LOCK, WAIT_LIMIT, ApplyKind, Portion, ReconnectState, ResolveRequest, TickBusy, TickReport, apply_mode, reconnect_due_at, run_tick
from subnet_tracker.types import DomainId, PendingReconnect, ReconnectMode, ResolveTrigger, Seconds, Timestamp

NOW = Timestamp(1_000_000)
AUTO, ON_LEAVE, NOW_MODE, NONE = ReconnectMode.AUTO, ReconnectMode.ON_LEAVE, ReconnectMode.NOW, ReconnectMode.NONE
NEW = ResolveTrigger.NEW_DOMAINS


def ids(*values: int) -> frozenset[DomainId]:
    return frozenset(DomainId(v) for v in values)


def at(now: int) -> Callable[[], Timestamp]:
    return lambda: Timestamp(now)


@dataclass
class Clock:
    now: int
    slept: list[int] = field(default_factory=list)

    def __call__(self) -> Timestamp:
        return Timestamp(self.now)

    def sleep(self, seconds: int) -> None:
        self.slept.append(seconds)
        self.now += seconds


@dataclass
class Steps:
    routes_added: list[bool] = field(default_factory=list)
    portion_results: list[Portion] = field(default_factory=list)
    requested: list[ResolveRequest] = field(default_factory=list)
    kinds: list[ApplyKind] = field(default_factory=list)
    commits: list[ReconnectMode | None] = field(default_factory=list)
    portions_polled: int = 0

    def resolve_requested(self, conn: sqlite3.Connection, request: ResolveRequest, claim: Claim | None, now: Timestamp) -> None:
        assert not conn.in_transaction
        self.requested.append(request)

    def next_portion(self, conn: sqlite3.Connection, now: Timestamp) -> Portion:
        assert not conn.in_transaction
        self.portions_polled += 1
        return self.portion_results.pop(0) if self.portion_results else Portion(False, False)

    def apply(self, conn: sqlite3.Connection, kind: ApplyKind, now: Timestamp) -> bool:
        assert not conn.in_transaction
        self.kinds.append(kind)
        return self.routes_added.pop(0) if self.routes_added else False

    def commit(self, conn: sqlite3.Connection, mode: ReconnectMode | None, now: Timestamp) -> None:
        assert conn.in_transaction
        self.commits.append(mode)


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(seconds: float) -> None:
        raise AssertionError(f"real sleep {seconds}")

    monkeypatch.setattr(time, "sleep", fail)


@pytest.fixture
def domain(db: sqlite3.Connection) -> DomainId:
    return DomainId(insert(db, "domains", {"source": "manual", "created_at": 0, "updated_at": 0}))


def lock_path(tmp_path: Path) -> Path:
    return tmp_path / "tick.lock"


def tick(db: sqlite3.Connection, tmp_path: Path, steps: Steps, clock: Callable[[], Timestamp] | Clock, sleep: Callable[[int], None] | None = None) -> TickReport:
    if isinstance(clock, Clock):
        sleep = clock.sleep
    elif sleep is None:

        def sleep(seconds: int) -> None:
            raise AssertionError(f"unexpected sleep {seconds}")

    assert sleep is not None
    return run_tick(db, resolve=steps, apply=steps, clock=clock, sleep=sleep, lock=lock_path(tmp_path))


@contextmanager
def held(path: Path) -> Iterator[None]:
    with path.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def pending(db: sqlite3.Connection, mode: str, since: int, presence: int | None = None, last: int | None = None) -> None:
    insert(db, "reconnect_state", {"id": 1, "pending_mode": mode, "pending_since": since, "presence_until": presence, "last_reconnect_at": last})


def test_design_intervals() -> None:
    assert (500, 55, 2, Path("/run/subnet-tracker/tick.lock")) == (RESOLVE_PORTION, WAIT_LIMIT, POLL_SECONDS, TICK_LOCK)


def test_default_lock_is_the_run_file() -> None:
    assert Runtime().lock_file == TICK_LOCK


@pytest.mark.parametrize(
    ("state", "due"),
    [
        (ReconnectState(None, None, None, None), None),
        (ReconnectState(PendingReconnect.ON_LEAVE, Timestamp(10), Timestamp(50), None), Timestamp(50)),
        (ReconnectState(PendingReconnect.ON_LEAVE, Timestamp(10), None, None), Timestamp(10)),
        # presence_until 0 is absent, same as the design expression `presence_until or pending_since`
        (ReconnectState(PendingReconnect.ON_LEAVE, Timestamp(10), Timestamp(0), None), Timestamp(10)),
        (ReconnectState(PendingReconnect.AUTO, Timestamp(100), None, Timestamp(100)), Timestamp(160)),
        (ReconnectState(PendingReconnect.AUTO, Timestamp(100), Timestamp(500), Timestamp(100)), Timestamp(500)),
        (ReconnectState(PendingReconnect.AUTO, Timestamp(1000), None, None), Timestamp(1000)),
    ],
)
def test_reconnect_due_at(state: ReconnectState, due: Timestamp | None) -> None:
    assert reconnect_due_at(state, ApplySettings(min_interval=Seconds(60))) == due


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
def test_apply_mode(claimed: ReconnectMode | None, pending_mode: ReconnectMode | None, routes_added: bool, scheduled: bool, mode: ReconnectMode | None) -> None:
    assert apply_mode(claimed, pending_mode, routes_added, scheduled=scheduled) == mode


def test_idle_tick_plans_once_keeps_the_lock_file_and_releases_it(db: sqlite3.Connection, tmp_path: Path) -> None:
    path = lock_path(tmp_path)
    path.write_text("keep\n")
    steps = Steps()
    report = tick(db, tmp_path, steps, at(NOW))
    assert report == TickReport(0, 0, False, 0, 0, 0, 0)
    assert (steps.kinds, steps.commits, steps.portions_polled) == ([ApplyKind.PLAN], [None], 1)
    assert path.read_text() == "keep\n"
    assert tick(db, tmp_path, Steps(), at(NOW)) == report


def test_a_held_lock_skips_the_tick_and_leaves_requests(db: sqlite3.Connection, tmp_path: Path) -> None:
    enqueue(db, [Apply(NOW_MODE)], NOW)
    path = lock_path(tmp_path)
    with held(path), pytest.raises(TickBusy):
        tick(db, tmp_path, Steps(), at(NOW))
    assert count(db, "requests") == 1
    with held(path):
        pass


def test_a_failed_step_leaves_the_request_and_the_lock(db: sqlite3.Connection, tmp_path: Path, domain: DomainId) -> None:
    enqueue(db, [ResolveDomains(ids(domain))], NOW)

    class Boom(Steps):
        def resolve_requested(self, conn: sqlite3.Connection, request: ResolveRequest, claim: Claim | None, now: Timestamp) -> None:
            raise RuntimeError("resolve failed")

    with pytest.raises(RuntimeError, match="resolve failed"):
        tick(db, tmp_path, Boom(), at(NOW))
    assert count(db, "requests") == 1
    with held(lock_path(tmp_path)):
        pass


def test_commit_and_finish_roll_back_together(db: sqlite3.Connection, tmp_path: Path) -> None:
    enqueue(db, [Apply(ON_LEAVE)], NOW)

    class Stop(Steps):
        def commit(self, conn: sqlite3.Connection, mode: ReconnectMode | None, now: Timestamp) -> None:
            assert conn.in_transaction
            if mode == ON_LEAVE:
                insert(conn, "reconnect_state", {"id": 1, "pending_mode": "on_leave", "pending_since": int(now)})
                raise RuntimeError("stop before finish")

    with pytest.raises(RuntimeError, match="stop before finish"):
        tick(db, tmp_path, Stop(routes_added=[True]), at(NOW))
    assert (count(db, "requests"), count(db, "reconnect_state")) == (1, 0)


def test_requests_are_served_between_portions(db: sqlite3.Connection, tmp_path: Path, domain: DomainId) -> None:
    class Between(Steps):
        def next_portion(self, conn: sqlite3.Connection, now: Timestamp) -> Portion:
            portion = super().next_portion(conn, now)
            if self.portions_polled == 1:
                enqueue(conn, [ResolveDomains(ids(domain), NEW)], now)
            return portion

    steps = Between(portion_results=[Portion(True, True), Portion(True, False)])
    report = tick(db, tmp_path, steps, at(NOW))
    assert [request.new_domains for request in steps.requested] == [ids(), ids(domain)]
    assert (report.portions, report.requests_finished, count(db, "requests")) == (2, 1, 0)
    assert steps.portions_polled == 2


def test_missing_domain_ids_are_dropped(db: sqlite3.Connection, tmp_path: Path, domain: DomainId) -> None:
    missing = DomainId(int(domain) + 1)
    enqueue(db, [ResolveDomains(ids(domain, missing), NEW), ResolveDomains(ids(missing)), ResolveAll()], NOW)
    steps = Steps()
    tick(db, tmp_path, steps, at(NOW))
    assert steps.requested[0] == ResolveRequest(ids(domain), ids(), True)
    assert count(db, "requests") == 0


def test_invalid_claim_is_logged_and_the_rest_runs(db: sqlite3.Connection, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    insert(db, "requests", {"kind": "apply", "payload": "{}", "created_at": int(NOW)})
    enqueue(db, [Apply(AUTO)], NOW)
    caplog.set_level(logging.WARNING, logger="subnet_tracker.tick")
    steps = Steps()
    report = tick(db, tmp_path, steps, at(NOW))
    assert any(record.message == "Claim.invalid: apply {}: KeyError: 'reconnect'" for record in caplog.records)
    assert steps.commits[0] == AUTO
    assert (report.requests_finished, count(db, "requests")) == (2, 0)


def test_on_leave_is_committed_only_when_routes_were_added(db: sqlite3.Connection, tmp_path: Path) -> None:
    enqueue(db, [Apply(ON_LEAVE)], NOW)
    withheld = Steps()
    tick(db, tmp_path, withheld, at(NOW))
    assert withheld.commits == [None, None]
    enqueue(db, [Apply(ON_LEAVE)], NOW)
    added = Steps(routes_added=[True])
    report = tick(db, tmp_path, added, at(NOW))
    assert added.commits == [ON_LEAVE, ON_LEAVE]
    assert not report.reconnected


def test_pending_mode_is_merged_with_the_claim(db: sqlite3.Connection, tmp_path: Path) -> None:
    pending(db, "on_leave", int(NOW) + WAIT_LIMIT + 100)
    enqueue(db, [Apply(AUTO)], NOW)
    steps = Steps()
    tick(db, tmp_path, steps, at(NOW))
    assert steps.commits == [ON_LEAVE, ON_LEAVE]


def test_now_reconnects_without_waiting(db: sqlite3.Connection, tmp_path: Path) -> None:
    enqueue(db, [Apply(NOW_MODE)], NOW)
    steps = Steps()
    report = tick(db, tmp_path, steps, at(NOW))
    assert steps.commits[0] == NOW_MODE
    assert report == TickReport(1, 0, True, 0, 0, 0, 0)
    assert steps.kinds == [ApplyKind.REQUEST, ApplyKind.PLAN]


def test_none_without_a_portion_does_not_schedule_auto(db: sqlite3.Connection, tmp_path: Path) -> None:
    enqueue(db, [Apply(NONE)], NOW)
    steps = Steps(routes_added=[True, True])
    tick(db, tmp_path, steps, at(NOW))
    assert steps.commits == [None, None]


def test_a_scheduled_plan_arms_auto_when_it_adds_routes(db: sqlite3.Connection, tmp_path: Path) -> None:
    steps = Steps(routes_added=[True])
    report = tick(db, tmp_path, steps, at(NOW))
    assert steps.kinds == [ApplyKind.PLAN]
    assert steps.commits == [AUTO]
    assert not report.reconnected


def test_a_portion_keeps_auto_for_routes_added_by_the_plan(db: sqlite3.Connection, tmp_path: Path) -> None:
    enqueue(db, [Apply(NONE)], NOW)
    steps = Steps(routes_added=[True, True], portion_results=[Portion(True, False)])
    tick(db, tmp_path, steps, at(NOW))
    assert steps.commits == [None, AUTO]


def test_strategy_none_writes_the_plan_and_does_not_reconnect(db: sqlite3.Connection, tmp_path: Path) -> None:
    set_setting(db, "apply.strategy", "none", NOW)
    pending(db, "auto", int(NOW))
    set_setting(db, "apply.min_interval", "0s", NOW)
    enqueue(db, [Apply(NOW_MODE)], NOW)
    clock = Clock(int(NOW))
    steps = Steps(routes_added=[True, True])
    report = tick(db, tmp_path, steps, clock)
    assert steps.kinds == [ApplyKind.REQUEST, ApplyKind.PLAN]
    assert steps.commits == [None, None]
    assert (clock.slept, report.reconnected) == ([], False)


def test_wait_polls_every_two_seconds_until_due(db: sqlite3.Connection, tmp_path: Path) -> None:
    set_setting(db, "apply.min_interval", "0s", NOW)
    pending(db, "auto", int(NOW) + 5)
    clock = Clock(int(NOW))
    steps = Steps()
    report = tick(db, tmp_path, steps, clock)
    assert clock.slept == [POLL_SECONDS, POLL_SECONDS, 1]
    assert steps.commits[-1] == NOW_MODE
    assert report.reconnected
    assert report.waited_seconds == 5
    assert max(step - POLL_SECONDS for step in clock.slept) <= 0


def test_wait_horizon_is_measured_from_tick_start(db: sqlite3.Connection, tmp_path: Path) -> None:
    set_setting(db, "apply.min_interval", "0s", NOW)
    pending(db, "auto", int(NOW) + WAIT_LIMIT + 30)
    clock = Clock(int(NOW))

    class Advance(Steps):
        def next_portion(self, conn: sqlite3.Connection, now: Timestamp) -> Portion:
            clock.now += 30
            return super().next_portion(conn, now)

    steps = Advance(portion_results=[Portion(True, False)])
    report = tick(db, tmp_path, steps, clock)
    # due is 30s after the portion, still inside a horizon measured from the moved clock, and outside one measured from the start
    assert (clock.slept, report.reconnected, clock.now) == ([], False, int(NOW) + 30)


def test_due_on_the_horizon_waits_and_one_second_later_does_not(db: sqlite3.Connection, tmp_path: Path) -> None:
    set_setting(db, "apply.min_interval", "0s", NOW)
    pending(db, "on_leave", int(NOW) + WAIT_LIMIT)
    clock = Clock(int(NOW))
    tick(db, tmp_path, Steps(), clock)
    remainder = WAIT_LIMIT % POLL_SECONDS
    assert clock.slept == [POLL_SECONDS] * (WAIT_LIMIT // POLL_SECONDS) + ([remainder] if remainder else [])
    db.execute("DELETE FROM reconnect_state")
    pending(db, "on_leave", int(NOW) + WAIT_LIMIT + 1)
    later = Clock(int(NOW))
    report = tick(db, tmp_path, Steps(), later)
    assert (later.slept, report.reconnected) == ([], False)


def test_wait_rereads_presence_and_requests(db: sqlite3.Connection, tmp_path: Path, domain: DomainId) -> None:
    set_setting(db, "apply.min_interval", "0s", NOW)
    pending(db, "on_leave", int(NOW) + 4)
    clock = Clock(int(NOW))
    steps = Steps()

    def sleep(seconds: int) -> None:
        assert not db.in_transaction
        clock.sleep(seconds)
        if len(clock.slept) == 1:
            db.execute("UPDATE reconnect_state SET presence_until = ? WHERE id = 1", (int(NOW) + WAIT_LIMIT + 10,))
            enqueue(db, [ResolveDomains(ids(domain), NEW)], NOW)

    report = run_tick(db, resolve=steps, apply=steps, clock=clock, sleep=sleep, lock=lock_path(tmp_path))
    assert clock.slept == [POLL_SECONDS]
    assert not report.reconnected
    assert steps.requested[-1].new_domains == ids(domain)
    assert steps.portions_polled == 1
    assert count(db, "requests") == 0


def test_a_frozen_clock_does_not_spin(db: sqlite3.Connection, tmp_path: Path) -> None:
    set_setting(db, "apply.min_interval", "0s", NOW)
    pending(db, "auto", int(NOW) + 4)
    with pytest.raises(RuntimeError, match="clock"):
        tick(db, tmp_path, Steps(), at(NOW), sleep=lambda seconds: None)


def test_history_is_pruned_on_the_retention_boundary(db: sqlite3.Connection, tmp_path: Path) -> None:
    set_setting(db, "history.retention", "10s", NOW)
    cutoff = int(NOW) - 10
    insert(db, "resolve_runs", {"trigger": "timer", "started_at": cutoff - 1})
    kept_run = insert(db, "resolve_runs", {"trigger": "manual", "started_at": cutoff})
    old_apply = dict(ROWS["apply_runs"])
    old_apply["started_at"] = cutoff - 1
    new_apply = dict(ROWS["apply_runs"])
    new_apply["started_at"] = cutoff
    insert(db, "apply_runs", old_apply)
    kept_apply = insert(db, "apply_runs", new_apply)
    insert(db, "events", {"ts": cutoff - 1, "kind": "domain.added", "actor": "tick"})
    insert(db, "events", {"ts": cutoff, "kind": "domain.added", "actor": "tick"})
    report = tick(db, tmp_path, Steps(), at(NOW))
    assert (report.resolve_runs_pruned, report.apply_runs_pruned, report.events_pruned) == (1, 1, 1)
    assert [tuple(row) for row in db.execute("SELECT id FROM resolve_runs")] == [(kept_run,)]
    assert [tuple(row) for row in db.execute("SELECT id FROM apply_runs")] == [(kept_apply,)]
    assert [tuple(row) for row in db.execute("SELECT ts FROM events")] == [(cutoff,)]


def test_tick_command_does_not_write_the_trigger(cli: Cli, db: sqlite3.Connection, tmp_path: Path) -> None:
    assert cli("tick") == Result(0, "finished 0 requests, 0 portions\n", "")
    assert not (tmp_path / "trigger").exists()
    trigger = tmp_path / "trigger"
    trigger.write_text("100 cli:alice\n")
    enqueue(db, [Apply(AUTO)], NOW)
    assert cli("tick") == Result(0, "finished 1 requests, 0 portions\n", "")
    assert trigger.read_text() == "100 cli:alice\n"
    assert count(db, "requests") == 0


def test_tick_json(cli: Cli, db: sqlite3.Connection) -> None:
    assert cli.json("tick") == {"requests_finished": 0, "portions": 0, "reconnected": False, "waited_seconds": 0, "resolve_runs_pruned": 0, "apply_runs_pruned": 0, "events_pruned": 0}


def test_tick_already_running_exits_clean(cli: Cli, db: sqlite3.Connection, tmp_path: Path) -> None:
    enqueue(db, [Apply(AUTO)], NOW)
    with held(tmp_path / "tick.lock"):
        assert cli.json("tick") == {"busy": True}
    assert count(db, "requests") == 1


def test_tick_needs_the_database(cli: Cli, tmp_path: Path) -> None:
    result = cli("tick")
    assert result.code == 1
    assert f"database {tmp_path / 'state.db'} does not exist" in result.err
