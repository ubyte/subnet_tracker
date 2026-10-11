import sqlite3

import pytest

from addresses_world import DAY, answer, ok, record, resolver, rows, tracked
from domains_world import NOW, count
from subnet_tracker.core.addresses import Resolution, name_resolves
from subnet_tracker.core.db import transaction
from subnet_tracker.core.events import Event, EventFilter, EventKind, list_events
from subnet_tracker.core.resolve_runs import NewResolveRun, add_addresses_expired, list_resolve_runs, start_resolve_run
from subnet_tracker.core.services.resolves import Expired, expire_addresses
from subnet_tracker.core.settings import SettingValue
from subnet_tracker.core.settings_store import set_setting
from subnet_tracker.types import DomainId, DomainName, NameId, ResolverKind, ResolveStatus, ResolveTrigger, Seconds, Timestamp

RETENTION = Seconds(4 * DAY)
ACTOR = "tick"


def miss(status: ResolveStatus = ResolveStatus.NODATA) -> Resolution:
    return Resolution(status, error="timeout" if status in (ResolveStatus.ERROR, ResolveStatus.UNREACHABLE) else None)


def seen(db: sqlite3.Connection) -> list[tuple[object, ...]]:
    return rows(
        db,
        "SELECT n.name, a.ip, r.name FROM sightings s JOIN addresses a ON a.id = s.address_id JOIN names n ON n.id = a.name_id JOIN resolvers r ON r.id = s.resolver_id ORDER BY n.name, a.ip, r.name",
    )


def expired_events(db: sqlite3.Connection) -> tuple[Event, ...]:
    return list_events(db, EventFilter(10, EventKind.ADDRESS_EXPIRED))


def domain_of(db: sqlite3.Connection, name: NameId) -> DomainId:
    return DomainId(db.execute("SELECT domain_id FROM names WHERE id = ?", (name,)).fetchone()[0])


def test_empty_database_expires_nothing(db: sqlite3.Connection) -> None:
    assert expire_addresses(db, ACTOR, NOW) == Expired(0)
    assert expired_events(db) == ()
    assert not db.in_transaction


def test_an_address_still_in_the_last_answer_stays_however_late_the_call(db: sqlite3.Connection) -> None:
    (a,) = tracked(db, "a.com")
    dns = resolver(db, "dns")
    record(db, answer(a, dns, ok("192.0.2.1")))
    requests = count(db, "requests")
    assert expire_addresses(db, ACTOR, Timestamp(NOW + 100 * DAY)) == Expired(0)
    assert seen(db) == [("a.com", "192.0.2.1", "dns")]
    assert name_resolves(db, [a])[0].answered_at == NOW
    assert expired_events(db) == ()
    assert count(db, "requests") == requests


@pytest.mark.parametrize("status", [ResolveStatus.NODATA, ResolveStatus.NXDOMAIN, ResolveStatus.NOTFOUND])
def test_an_authoritative_miss_expires_on_the_retention_boundary(db: sqlite3.Connection, status: ResolveStatus) -> None:
    (a,) = tracked(db, "a.com")
    dns = resolver(db, "dns")
    domain = domain_of(db, a)
    record(db, answer(a, dns, ok("192.0.2.1")))
    record(db, answer(a, dns, miss(status)), at=NOW + RETENTION - 1)
    assert expire_addresses(db, ACTOR, Timestamp(NOW + 100 * DAY)) == Expired(0)
    assert seen(db) == [("a.com", "192.0.2.1", "dns")]
    record(db, answer(a, dns, miss(status)), at=NOW + RETENTION)
    requests = count(db, "requests")
    resolves = name_resolves(db, [a])
    assert expire_addresses(db, ACTOR, Timestamp(NOW)) == Expired(1)
    assert seen(db) == []
    assert count(db, "addresses") == 0
    assert name_resolves(db, [a]) == resolves
    assert count(db, "requests") == requests
    (event,) = expired_events(db)
    assert (event.ts, event.kind, event.actor, event.domain_id, event.name, event.details) == (
        Timestamp(NOW),
        EventKind.ADDRESS_EXPIRED,
        ACTOR,
        domain,
        DomainName("a.com"),
        {"ip": "192.0.2.1", "resolver_id": dns, "resolver": "dns"},
    )
    assert [e.id for e in list_events(db, EventFilter(10, resolver_id=dns)) if e.kind is EventKind.ADDRESS_EXPIRED] == [event.id]
    assert expire_addresses(db, ACTOR, Timestamp(NOW + 1)) == Expired(0)
    assert expired_events(db) == (event,)
    assert not db.in_transaction


@pytest.mark.parametrize("status", [ResolveStatus.ERROR, ResolveStatus.UNREACHABLE])
def test_a_failure_does_not_expire_however_late(db: sqlite3.Connection, status: ResolveStatus) -> None:
    (a,) = tracked(db, "a.com")
    dns = resolver(db, "dns")
    record(db, answer(a, dns, ok("192.0.2.1")))
    record(db, answer(a, dns, miss(status)), at=NOW + 100 * DAY)
    assert expire_addresses(db, ACTOR, Timestamp(NOW + 100 * DAY)) == Expired(0)
    assert seen(db) == [("a.com", "192.0.2.1", "dns")]
    assert name_resolves(db, [a])[0].answered_at == NOW
    assert expired_events(db) == ()


def test_only_the_address_missing_past_retention_is_deleted(db: sqlite3.Connection) -> None:
    (a,) = tracked(db, "a.com")
    dns = resolver(db, "dns")
    record(db, answer(a, dns, ok("192.0.2.1", "2001:db8::5")))
    record(db, answer(a, dns, ok("192.0.2.1")), at=NOW + RETENTION - 1)
    assert expire_addresses(db, ACTOR, Timestamp(NOW + 50 * DAY)) == Expired(0)
    record(db, answer(a, dns, ok("192.0.2.1")), at=NOW + RETENTION)
    assert expire_addresses(db, ACTOR, Timestamp(NOW + 5)) == Expired(1)
    assert seen(db) == [("a.com", "192.0.2.1", "dns")]
    (event,) = expired_events(db)
    assert event.ts == Timestamp(NOW + 5)
    assert event.details == {"ip": "2001:db8::5", "resolver_id": dns, "resolver": "dns"}


def test_expiry_is_per_name_and_resolver(db: sqlite3.Connection) -> None:
    a, www = tracked(db, "a.com www.a.com")
    dns, browser = resolver(db, "dns"), resolver(db, "browser", ResolverKind.BROWSER)
    record(db, answer(a, dns, ok("192.0.2.1")), answer(a, browser, ok("192.0.2.1")), answer(www, dns, ok("192.0.2.1")), answer(www, browser, ok("192.0.2.1")))
    record(db, answer(a, dns, Resolution(ResolveStatus.NXDOMAIN)), at=NOW + RETENTION)
    assert expire_addresses(db, ACTOR, NOW) == Expired(0)
    assert seen(db) == [("a.com", "192.0.2.1", "browser"), ("www.a.com", "192.0.2.1", "browser"), ("www.a.com", "192.0.2.1", "dns")]
    assert expired_events(db) == ()
    record(db, answer(a, browser, Resolution(ResolveStatus.NXDOMAIN)), at=NOW + RETENTION)
    assert expire_addresses(db, ACTOR, NOW) == Expired(1)
    assert seen(db) == [("www.a.com", "192.0.2.1", "browser"), ("www.a.com", "192.0.2.1", "dns")]
    (event,) = expired_events(db)
    assert (event.name, event.details["resolver_id"]) == (DomainName("a.com"), browser)


def test_each_removed_address_is_one_event_in_address_order(db: sqlite3.Connection) -> None:
    a, www = tracked(db, "a.com www.a.com")
    dns = resolver(db, "dns")
    record(db, answer(a, dns, ok("192.0.2.1")), answer(www, dns, ok("192.0.2.1")))
    record(db, answer(a, dns, Resolution(ResolveStatus.NODATA)), answer(www, dns, Resolution(ResolveStatus.NODATA)), at=NOW + RETENTION)
    assert expire_addresses(db, ACTOR, Timestamp(NOW + 2)) == Expired(2)
    events = tuple(reversed(expired_events(db)))
    assert [(e.name, e.details["ip"], e.ts) for e in events] == [(DomainName("a.com"), "192.0.2.1", Timestamp(NOW + 2)), (DomainName("www.a.com"), "192.0.2.1", Timestamp(NOW + 2))]


def test_one_event_names_the_resolver_seen_latest(db: sqlite3.Connection) -> None:
    (a,) = tracked(db, "a.com")
    dns, doh = resolver(db, "dns"), resolver(db, "doh", ResolverKind.DOH)
    assert dns < doh
    record(db, answer(a, dns, ok("192.0.2.9")))
    record(db, answer(a, doh, ok("192.0.2.9")), at=NOW + 10)
    record(db, answer(a, dns, Resolution(ResolveStatus.NODATA)), at=NOW + RETENTION)
    record(db, answer(a, doh, Resolution(ResolveStatus.NODATA)), at=NOW + 10 + RETENTION)
    assert expire_addresses(db, ACTOR, Timestamp(NOW + 3)) == Expired(1)
    (event,) = expired_events(db)
    assert event.details == {"ip": "192.0.2.9", "resolver_id": doh, "resolver": "doh"}


def test_equal_last_seen_picks_the_smaller_resolver_id(db: sqlite3.Connection) -> None:
    (a,) = tracked(db, "a.com")
    dns, doh = resolver(db, "dns"), resolver(db, "doh", ResolverKind.DOH)
    record(db, answer(a, dns, ok("192.0.2.9")), answer(a, doh, ok("192.0.2.9")))
    record(db, answer(a, dns, Resolution(ResolveStatus.NODATA)), answer(a, doh, Resolution(ResolveStatus.NODATA)), at=NOW + RETENTION)
    assert expire_addresses(db, ACTOR, NOW) == Expired(1)
    assert expired_events(db)[0].details["resolver_id"] == dns


def test_the_threshold_is_routes_retention(db: sqlite3.Connection) -> None:
    (a,) = tracked(db, "a.com")
    dns = resolver(db, "dns")
    record(db, answer(a, dns, ok("192.0.2.1")))
    record(db, answer(a, dns, Resolution(ResolveStatus.NODATA)), at=NOW + DAY)
    assert isinstance(set_setting(db, "history.retention", "0s", NOW), SettingValue)
    assert expire_addresses(db, ACTOR, Timestamp(NOW + 100 * DAY)) == Expired(0)
    assert isinstance(set_setting(db, "routes.retention", "1d", NOW), SettingValue)
    assert expire_addresses(db, ACTOR, NOW) == Expired(1)
    assert seen(db) == []


def test_expiry_does_not_change_the_run_counter(db: sqlite3.Connection) -> None:
    (a,) = tracked(db, "a.com")
    dns = resolver(db, "dns")
    record(db, answer(a, dns, ok("192.0.2.1")))
    record(db, answer(a, dns, Resolution(ResolveStatus.NODATA)), at=NOW + RETENTION)
    run = start_resolve_run(db, NewResolveRun(ResolveTrigger.TIMER, NOW))
    assert expire_addresses(db, ACTOR, NOW) == Expired(1)
    assert list_resolve_runs(db, 10)[0].addresses_expired == 0
    add_addresses_expired(db, run, 1)
    assert list_resolve_runs(db, 10)[0].addresses_expired == 1


def expire_and_fail(db: sqlite3.Connection) -> None:
    with transaction(db):
        (a,) = tracked(db, "a.com")
        dns = resolver(db, "dns")
        record(db, answer(a, dns, ok("192.0.2.1")))
        record(db, answer(a, dns, Resolution(ResolveStatus.NODATA)), at=NOW + RETENTION)
        expire_addresses(db, ACTOR, NOW)
        raise RuntimeError("fail the caller")


def test_expiry_rolls_back_with_the_outer_transaction(db: sqlite3.Connection) -> None:
    with pytest.raises(RuntimeError, match="fail the caller"):
        expire_and_fail(db)
    assert (count(db, "domains"), count(db, "addresses"), count(db, "sightings"), count(db, "events")) == (0, 0, 0, 0)
    assert not db.in_transaction


def test_expiry_is_visible_to_other_connections_only_after_the_caller_commits(db: sqlite3.Connection, other: sqlite3.Connection) -> None:
    (a,) = tracked(db, "a.com")
    dns = resolver(db, "dns")
    record(db, answer(a, dns, ok("192.0.2.1")))
    record(db, answer(a, dns, Resolution(ResolveStatus.NODATA)), at=NOW + RETENTION)
    with transaction(db):
        assert expire_addresses(db, ACTOR, NOW) == Expired(1)
        assert count(other, "addresses") == 1
        assert expired_events(other) == ()
    assert count(other, "addresses") == 0
    assert len(expired_events(other)) == 1
