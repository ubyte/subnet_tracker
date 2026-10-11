import sqlite3

import pytest

from addresses_world import answer, ok, resolver, tracked
from domains_world import NOW, count
from subnet_tracker.core.addresses import Resolution
from subnet_tracker.core.db import transaction
from subnet_tracker.core.domains import delete_domains
from subnet_tracker.core.events import EventFilter, EventKind, list_events
from subnet_tracker.core.resolve_runs import (
    CLEANUP_INTERVAL,
    NewResolveRun,
    ResolverPortion,
    ResolverRunStats,
    ResolveRun,
    SchedulerState,
    add_addresses_expired,
    advance_full_pass,
    cleanup_due,
    close_full_pass,
    finish_resolve_run,
    list_resolve_runs,
    load_scheduler_state,
    mark_cleanup,
    new_full_pass_due,
    open_full_pass,
    prune_resolve_runs,
    record_run_results,
    run_resolvers,
    save_scheduler_state,
    start_resolve_run,
)
from subnet_tracker.core.services.resolves import Recorded
from subnet_tracker.types import DomainId, NameId, ResolverId, ResolverKind, ResolveRunId, ResolveStatus, ResolveTrigger, Seconds, Timestamp

HOUR = Seconds(3600)
DAY = 86400


def runs(db: sqlite3.Connection) -> tuple[ResolveRun, ...]:
    return list_resolve_runs(db, 100)


def manual(domains: int = 0) -> NewResolveRun:
    return NewResolveRun(ResolveTrigger.MANUAL, NOW, domains)


def portion(
    resolver_id: ResolverId,
    queries: int = 0,
    ok_count: int = 0,
    notfound: int = 0,
    errors: int = 0,
    unreachable: int = 0,
    total_ms: int | None = None,
    healthy: bool | None = None,
) -> ResolverPortion:
    return ResolverPortion(resolver_id, queries, ok_count, notfound, errors, unreachable, total_ms, healthy)


def test_new_full_pass_waits_out_the_interval_unless_none_is_open() -> None:
    idle = SchedulerState()
    assert new_full_pass_due(idle, HOUR, NOW)
    recent = SchedulerState(last_full_started_at=NOW)
    assert not new_full_pass_due(recent, HOUR, Timestamp(NOW + HOUR - 1))
    assert new_full_pass_due(recent, HOUR, Timestamp(NOW + HOUR))
    # an open pass is continued by the caller; it is not a reason to open another
    running = SchedulerState(ResolveRunId(1), NameId(4), NOW)
    assert not new_full_pass_due(running, HOUR, Timestamp(NOW + 10 * HOUR))


def test_cleanup_is_due_once_a_day() -> None:
    assert Seconds(DAY) == CLEANUP_INTERVAL
    assert cleanup_due(None, NOW)
    assert not cleanup_due(NOW, Timestamp(NOW + DAY - 1))
    assert cleanup_due(NOW, Timestamp(NOW + DAY))


def test_scheduler_state_starts_empty_and_round_trips(db: sqlite3.Connection) -> None:
    assert load_scheduler_state(db) == SchedulerState()
    run = start_resolve_run(db, manual())
    state = SchedulerState(run, NameId(41), Timestamp(NOW - 5), Timestamp(NOW - DAY))
    save_scheduler_state(db, state)
    assert load_scheduler_state(db) == state
    save_scheduler_state(db, SchedulerState())
    assert load_scheduler_state(db) == SchedulerState()


def test_full_run_must_exist(db: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        save_scheduler_state(db, SchedulerState(ResolveRunId(1), None, NOW, None))


@pytest.mark.parametrize("trigger", [ResolveTrigger.TIMER, ResolveTrigger.MANUAL, ResolveTrigger.NEW_DOMAINS])
def test_server_runs_have_no_resolver(trigger: ResolveTrigger) -> None:
    with pytest.raises(ValueError, match="only browser and agent"):
        NewResolveRun(trigger, NOW, resolver_id=ResolverId(1))


@pytest.mark.parametrize("trigger", [ResolveTrigger.BROWSER, ResolveTrigger.AGENT])
def test_external_runs_name_their_resolver(trigger: ResolveTrigger) -> None:
    with pytest.raises(ValueError, match="name their resolver"):
        NewResolveRun(trigger, NOW)


def test_domains_total_is_not_negative() -> None:
    with pytest.raises(ValueError, match="domains_total"):
        NewResolveRun(ResolveTrigger.MANUAL, NOW, -1)


def test_portion_counts_are_non_negative() -> None:
    with pytest.raises(ValueError, match="queries"):
        ResolverPortion(ResolverId(1), -1, 0, 0, 0, 0)
    with pytest.raises(ValueError, match="total_ms"):
        ResolverPortion(ResolverId(1), 1, 0, 0, 0, 0, total_ms=-1)
    with pytest.raises(ValueError, match="queries"):
        ResolverPortion(ResolverId(1), 0, 0, 0, 0, 0, total_ms=10)


def test_runs_come_back_newest_first(db: sqlite3.Connection) -> None:
    browser = resolver(db, "browser", ResolverKind.BROWSER)
    first = start_resolve_run(db, NewResolveRun(ResolveTrigger.NEW_DOMAINS, NOW, 2))
    second = start_resolve_run(db, NewResolveRun(ResolveTrigger.BROWSER, Timestamp(NOW + 1), 1, browser))
    third = start_resolve_run(db, manual(4))
    assert runs(db)[:2] == (
        ResolveRun(third, ResolveTrigger.MANUAL, None, NOW, None, 4, 0, 0, None),
        ResolveRun(second, ResolveTrigger.BROWSER, browser, Timestamp(NOW + 1), None, 1, 0, 0, None),
    )
    assert [run.id for run in runs(db)] == [third, second, first]
    assert list_resolve_runs(db, 1) == (runs(db)[0],)


def test_unknown_resolver_is_rejected(db: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        start_resolve_run(db, NewResolveRun(ResolveTrigger.AGENT, NOW, resolver_id=ResolverId(99)))
    assert runs(db) == ()


def test_finish_sets_the_time_and_replaces_the_error(db: sqlite3.Connection) -> None:
    run = start_resolve_run(db, manual(3))
    finish_resolve_run(db, run, Timestamp(NOW + 8), "timeout")
    assert runs(db) == (ResolveRun(run, ResolveTrigger.MANUAL, None, NOW, Timestamp(NOW + 8), 3, 0, 0, "timeout"),)
    finish_resolve_run(db, run, Timestamp(NOW + 9), None)
    assert runs(db) == (ResolveRun(run, ResolveTrigger.MANUAL, None, NOW, Timestamp(NOW + 9), 3, 0, 0, None),)


def test_finish_of_a_missing_run_raises(db: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        finish_resolve_run(db, ResolveRunId(1), NOW)


def test_expired_addresses_add_up(db: sqlite3.Connection) -> None:
    run = start_resolve_run(db, manual())
    add_addresses_expired(db, run, 2)
    add_addresses_expired(db, run, 0)
    add_addresses_expired(db, run, 5)
    assert runs(db)[0].addresses_expired == 7


def test_expired_count_is_not_negative(db: sqlite3.Connection) -> None:
    run = start_resolve_run(db, manual())
    with pytest.raises(ValueError, match="count"):
        add_addresses_expired(db, run, -1)
    assert runs(db)[0].addresses_expired == 0


def test_expired_of_a_missing_run_raises(db: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        add_addresses_expired(db, ResolveRunId(1), 1)


def test_full_pass_opens_once_and_closes_without_the_cursor(db: sqlite3.Connection) -> None:
    mark_cleanup(db, Timestamp(NOW - DAY))
    run = open_full_pass(db, NOW, 12)
    assert runs(db) == (ResolveRun(run, ResolveTrigger.TIMER, None, NOW, None, 12, 0, 0, None),)
    assert load_scheduler_state(db) == SchedulerState(run, None, NOW, Timestamp(NOW - DAY))
    with pytest.raises(ValueError, match="already open"):
        open_full_pass(db, Timestamp(NOW + 1), 1)
    assert [item.id for item in runs(db)] == [run]
    advance_full_pass(db, NameId(7))
    assert load_scheduler_state(db).full_cursor_name_id == NameId(7)
    close_full_pass(db, Timestamp(NOW + 3), "stopped")
    assert runs(db) == (ResolveRun(run, ResolveTrigger.TIMER, None, NOW, Timestamp(NOW + 3), 12, 0, 0, "stopped"),)
    assert load_scheduler_state(db) == SchedulerState(None, None, NOW, Timestamp(NOW - DAY))
    assert not new_full_pass_due(load_scheduler_state(db), HOUR, Timestamp(NOW + 3))


def test_cursor_and_close_need_an_open_pass(db: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="no full resolve pass"):
        advance_full_pass(db, NameId(1))
    with pytest.raises(ValueError, match="no full resolve pass"):
        close_full_pass(db, NOW)
    assert load_scheduler_state(db) == SchedulerState()


def test_finishing_another_run_leaves_the_open_pass(db: sqlite3.Connection) -> None:
    full = open_full_pass(db, NOW, 1)
    advance_full_pass(db, NameId(2))
    other = start_resolve_run(db, manual())
    finish_resolve_run(db, other, Timestamp(NOW + 1))
    assert load_scheduler_state(db) == SchedulerState(full, NameId(2), NOW, None)


def test_mark_cleanup_keeps_the_pass(db: sqlite3.Connection) -> None:
    run = open_full_pass(db, NOW, 1)
    advance_full_pass(db, NameId(3))
    mark_cleanup(db, Timestamp(NOW + DAY))
    assert load_scheduler_state(db) == SchedulerState(run, NameId(3), NOW, Timestamp(NOW + DAY))
    assert cleanup_due(load_scheduler_state(db).last_cleanup_at, Timestamp(NOW + DAY)) is False


def test_recording_writes_addresses_through_the_resolve_service(db: sqlite3.Connection) -> None:
    (name,) = tracked(db, "a.com")
    dns = resolver(db, "dns")
    run = start_resolve_run(db, manual(1))
    before = count(db, "requests")
    recorded = record_run_results(db, run, [answer(name, dns, ok("192.0.2.1"))], [portion(dns, 2, ok_count=1, total_ms=10)], "tick", NOW)
    assert recorded == Recorded(1)
    assert runs(db)[0].addresses_new == 1
    assert count(db, "requests") == before
    times = db.execute("SELECT last_attempt_at, last_success_at FROM names WHERE id = ?", (name,)).fetchone()
    assert (times["last_attempt_at"], times["last_success_at"]) == (NOW, NOW)
    events = [event for event in list_events(db, EventFilter(10)) if event.kind is EventKind.ADDRESS_NEW]
    assert [(event.actor, event.details["ip"], event.details["resolver"]) for event in events] == [("tick", "192.0.2.1", "dns")]
    assert run_resolvers(db, run) == (ResolverRunStats(dns, 2, 1, 0, 0, 0, 5, True),)


def test_a_repeat_address_does_not_increase_addresses_new(db: sqlite3.Connection) -> None:
    (name,) = tracked(db, "a.com")
    dns = resolver(db, "dns")
    run = start_resolve_run(db, manual())
    record_run_results(db, run, [answer(name, dns, ok("192.0.2.1"))], [portion(dns, 1, ok_count=1)], "tick", NOW)
    again = record_run_results(db, run, [answer(name, dns, ok("192.0.2.1"))], [portion(dns, 1, ok_count=1)], "tick", Timestamp(NOW + 1))
    assert again == Recorded(0)
    assert runs(db)[0].addresses_new == 1
    assert run_resolvers(db, run) == (ResolverRunStats(dns, 2, 2, 0, 0, 0, None, True),)


def test_an_error_updates_the_run_and_not_the_addresses(db: sqlite3.Connection) -> None:
    (name,) = tracked(db, "a.com")
    dns = resolver(db, "dns")
    run = start_resolve_run(db, manual())
    failed = answer(name, dns, Resolution(ResolveStatus.ERROR, error="timeout"))
    assert record_run_results(db, run, [failed], [portion(dns, 2, errors=1, total_ms=30)], "tick", NOW) == Recorded(0)
    assert runs(db)[0].addresses_new == 0
    assert count(db, "addresses") == 0
    assert run_resolvers(db, run) == (ResolverRunStats(dns, 2, 0, 0, 1, 0, 15, True),)


def test_caller_buckets_are_stored_and_added(db: sqlite3.Connection) -> None:
    (name,) = tracked(db, "a.com")
    dns, doh = resolver(db, "dns"), resolver(db, "doh", ResolverKind.DOH)
    run = start_resolve_run(db, manual())
    # notfound is the caller's bucket: the result status is not reclassified here
    record_run_results(db, run, [answer(name, dns, ok("192.0.2.1"))], [portion(dns, 1, notfound=4), portion(doh, 3, unreachable=3)], "tick", NOW)
    record_run_results(db, run, [], [portion(dns, 2, ok_count=1, notfound=1, errors=1, unreachable=1, healthy=False)], "tick", NOW)
    assert run_resolvers(db, run) == (
        ResolverRunStats(dns, 3, 1, 5, 1, 1, None, False),
        ResolverRunStats(doh, 3, 0, 0, 0, 3, None, True),
    )
    assert run_resolvers(db, ResolveRunId(run + 1)) == ()


def test_average_is_the_integer_mean_weighted_by_queries(db: sqlite3.Connection) -> None:
    dns = resolver(db, "dns")
    run = start_resolve_run(db, manual())
    record_run_results(db, run, [], [portion(dns, 4, total_ms=10)], "tick", NOW)
    assert run_resolvers(db, run)[0].avg_ms == 2
    record_run_results(db, run, [], [portion(dns, 4, total_ms=22)], "tick", NOW)
    assert run_resolvers(db, run) == (ResolverRunStats(dns, 8, 0, 0, 0, 0, 3, True),)


def test_timing_is_all_portions_or_none(db: sqlite3.Connection) -> None:
    (name,) = tracked(db, "a.com")
    dns = resolver(db, "dns")
    run = start_resolve_run(db, manual())
    mixed = [portion(dns, 1, ok_count=1), portion(dns, 1, total_ms=10)]
    with pytest.raises(ValueError, match="timing"):
        record_run_results(db, run, [answer(name, dns, ok("192.0.2.1"))], mixed, "tick", NOW)
    assert (count(db, "addresses"), runs(db)[0].addresses_new, run_resolvers(db, run)) == (0, 0, ())
    record_run_results(db, run, [], [portion(dns, 1)], "tick", NOW)
    with pytest.raises(ValueError, match="timing"):
        record_run_results(db, run, [], [portion(dns, 1, total_ms=10)], "tick", NOW)
    assert run_resolvers(db, run) == (ResolverRunStats(dns, 1, 0, 0, 0, 0, None, True),)
    timed = start_resolve_run(db, manual())
    record_run_results(db, timed, [], [portion(dns, 1, total_ms=10)], "tick", NOW)
    with pytest.raises(ValueError, match="timing"):
        record_run_results(db, timed, [], [portion(dns, 1)], "tick", NOW)
    assert run_resolvers(db, timed) == (ResolverRunStats(dns, 1, 0, 0, 0, 0, 10, True),)


def test_healthy_stays_until_a_portion_sets_it(db: sqlite3.Connection) -> None:
    dns = resolver(db, "dns")
    run = start_resolve_run(db, manual())
    record_run_results(db, run, [], [portion(dns, 1, healthy=None)], "tick", NOW)
    assert run_resolvers(db, run)[0].healthy is True
    record_run_results(db, run, [], [portion(dns, 1, healthy=False)], "tick", NOW)
    assert run_resolvers(db, run)[0].healthy is False
    record_run_results(db, run, [], [portion(dns, 1)], "tick", NOW)
    assert run_resolvers(db, run)[0].healthy is False
    record_run_results(db, run, [], [portion(dns, 1, healthy=True)], "tick", NOW)
    assert run_resolvers(db, run)[0].healthy is True


def test_a_deleted_name_adds_no_address(db: sqlite3.Connection) -> None:
    (name,) = tracked(db, "a.com")
    dns = resolver(db, "dns")
    domain = DomainId(db.execute("SELECT domain_id FROM names WHERE id = ?", (name,)).fetchone()[0])
    delete_domains(db, [domain])
    run = start_resolve_run(db, manual())
    recorded = record_run_results(db, run, [answer(name, dns, ok("192.0.2.1"))], [portion(dns, 1, ok_count=1)], "tick", NOW)
    assert recorded == Recorded(0)
    assert runs(db)[0].addresses_new == 0
    assert count(db, "addresses") == 0
    assert run_resolvers(db, run) == (ResolverRunStats(dns, 1, 1, 0, 0, 0, None, True),)


def test_a_missing_run_writes_nothing(db: sqlite3.Connection) -> None:
    (name,) = tracked(db, "a.com")
    dns = resolver(db, "dns")
    with pytest.raises(ValueError, match="does not exist"):
        record_run_results(db, ResolveRunId(1), [answer(name, dns, ok("192.0.2.1"))], [portion(dns, 1, ok_count=1)], "tick", NOW)
    assert count(db, "addresses") == 0


def test_unknown_resolver_stats_roll_back_the_addresses(db: sqlite3.Connection) -> None:
    (name,) = tracked(db, "a.com")
    dns = resolver(db, "dns")
    run = start_resolve_run(db, manual())
    missing = portion(ResolverId(99), 1, ok_count=1)
    with pytest.raises(sqlite3.IntegrityError):
        record_run_results(db, run, [answer(name, dns, ok("192.0.2.1"))], [missing], "tick", NOW)
    assert count(db, "addresses") == 0
    assert runs(db)[0].addresses_new == 0
    assert run_resolvers(db, run) == ()


def test_recording_is_visible_to_other_connections_only_after_commit(db: sqlite3.Connection, other: sqlite3.Connection) -> None:
    (name,) = tracked(db, "a.com")
    dns = resolver(db, "dns")
    run = start_resolve_run(db, manual())
    with transaction(db):
        record_run_results(db, run, [answer(name, dns, ok("192.0.2.1"))], [portion(dns, 1, ok_count=1)], "tick", NOW)
        assert count(other, "addresses") == 0
        assert list_resolve_runs(other, 10)[0].addresses_new == 0
        assert run_resolvers(other, run) == ()
    assert count(other, "addresses") == 1
    assert list_resolve_runs(other, 10)[0].addresses_new == 1
    assert run_resolvers(other, run) == (ResolverRunStats(dns, 1, 1, 0, 0, 0, None, True),)


def _start_and_fail(db: sqlite3.Connection) -> None:
    with transaction(db):
        start_resolve_run(db, manual())
        raise RuntimeError("fail")


def test_a_run_rolls_back_with_the_caller(db: sqlite3.Connection) -> None:
    with pytest.raises(RuntimeError, match="fail"):
        _start_and_fail(db)
    assert runs(db) == ()


def test_prune_keeps_the_cutoff_and_the_cursor(db: sqlite3.Connection) -> None:
    dns = resolver(db, "dns")
    old = open_full_pass(db, Timestamp(NOW - 2), 1)
    advance_full_pass(db, NameId(8))
    record_run_results(db, old, [], [portion(dns, 1, ok_count=1)], "tick", Timestamp(NOW - 2))
    kept = start_resolve_run(db, NewResolveRun(ResolveTrigger.MANUAL, NOW, 1))
    record_run_results(db, kept, [], [portion(dns, 2, errors=2)], "tick", NOW)
    boundary = start_resolve_run(db, NewResolveRun(ResolveTrigger.TIMER, Timestamp(NOW - 1), 1))
    assert prune_resolve_runs(db, Timestamp(NOW - 1)) == 1
    assert [run.id for run in runs(db)] == [boundary, kept]
    assert run_resolvers(db, old) == ()
    assert run_resolvers(db, kept) == (ResolverRunStats(dns, 2, 0, 0, 2, 0, None, True),)
    # the deleted pass cleared full_run_id by the foreign key; the cursor is not a column of the run
    assert load_scheduler_state(db) == SchedulerState(None, NameId(8), Timestamp(NOW - 2), None)
    assert prune_resolve_runs(db, Timestamp(NOW - 1)) == 0
    fresh = open_full_pass(db, NOW, 2)
    assert load_scheduler_state(db) == SchedulerState(fresh, None, NOW, None)
