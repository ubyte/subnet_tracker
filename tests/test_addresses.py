import sqlite3
from ipaddress import IPv4Network, ip_address, ip_network

import pytest

from addresses_world import DAY, answer, ok, record, resolver, tracked
from db_rows import insert
from domains_world import NOW, count
from subnet_tracker.core.addresses import (
    AddressSighting,
    NameResolve,
    Resolution,
    SharedIp,
    SightingState,
    StoredAddress,
    addresses_in,
    answer_times,
    name_resolves,
    request_external,
    shared_ips,
    sighting_state,
)
from subnet_tracker.core.db import transaction
from subnet_tracker.core.events import Event, EventFilter, EventKind, list_events
from subnet_tracker.core.services.resolves import Recorded
from subnet_tracker.types import AddressId, DomainId, DomainName, NameId, ResolverId, ResolverKind, ResolveStatus, Seconds, Timestamp

RETENTION = Seconds(4 * DAY)


@pytest.mark.parametrize(
    ("status", "ips", "error"),
    [
        (ResolveStatus.OK, (), None),
        (ResolveStatus.NODATA, ("192.0.2.1",), None),
        (ResolveStatus.ERROR, ("192.0.2.1",), None),
        (ResolveStatus.OK, ("192.0.2.1",), "timeout"),
        (ResolveStatus.NXDOMAIN, (), "timeout"),
    ],
)
def test_addresses_come_only_with_ok_and_errors_only_without_an_answer(status: ResolveStatus, ips: tuple[str, ...], error: str | None) -> None:
    with pytest.raises(ValueError, match="status"):
        Resolution(status, frozenset(ip_address(ip) for ip in ips), error=error)


def test_failures_carry_an_error_text() -> None:
    assert Resolution(ResolveStatus.UNREACHABLE, error="network unreachable").error == "network unreachable"
    assert Resolution(ResolveStatus.ERROR).error is None


@pytest.mark.parametrize(
    ("status", "answered", "succeeded"),
    [
        (ResolveStatus.OK, True, True),
        (ResolveStatus.NODATA, True, False),
        (ResolveStatus.NXDOMAIN, True, False),
        (ResolveStatus.NOTFOUND, True, False),
        (ResolveStatus.ERROR, False, False),
        (ResolveStatus.UNREACHABLE, False, False),
    ],
)
def test_answer_times_follow_the_classification_table(status: ResolveStatus, answered: bool, succeeded: bool) -> None:
    assert answer_times(status, NOW) == (NOW if answered else None, NOW if succeeded else None)


@pytest.mark.parametrize(
    ("last_seen_at", "answered_at", "state"),
    [
        (NOW, NOW, SightingState.ACTIVE),
        (NOW, None, SightingState.ACTIVE),
        (NOW, NOW - DAY, SightingState.ACTIVE),
        (NOW - 1, NOW, SightingState.STALE),
        (NOW - RETENTION + 1, NOW, SightingState.STALE),
        (NOW - RETENTION, NOW, SightingState.EXPIRED),
    ],
)
def test_sighting_state_by_the_answer_of_its_pair(last_seen_at: int, answered_at: int | None, state: SightingState) -> None:
    sighting = AddressSighting(ResolverId(1), Timestamp(NOW - 10 * DAY), Timestamp(last_seen_at), None if answered_at is None else Timestamp(answered_at))
    assert sighting_state(sighting, RETENTION) is state


def test_name_resolves_read_back(db: sqlite3.Connection) -> None:
    a, www = tracked(db, "a.com www.a.com")
    dns, browser = resolver(db, "dns"), resolver(db, "browser", ResolverKind.BROWSER)
    ok_a = answer(a, dns, ok("192.0.2.1", cnames=("edge.a.net",)))
    failed = answer(a, browser, Resolution(ResolveStatus.ERROR, error="timeout"), "10.8.0.6 Firefox")
    record(db, ok_a, failed, answer(www, dns, Resolution(ResolveStatus.NXDOMAIN)))
    assert name_resolves(db, [a]) == (
        NameResolve(a, dns, NOW, NOW, NOW, ResolveStatus.OK, None, (DomainName("edge.a.net"),), None, None),
        NameResolve(a, browser, NOW, None, None, ResolveStatus.ERROR, "timeout", (), "10.8.0.6 Firefox", None),
    )
    assert [(r.name_id, r.status) for r in name_resolves(db, [a, www])] == [(a, ResolveStatus.OK), (a, ResolveStatus.ERROR), (www, ResolveStatus.NXDOMAIN)]


def test_resolve_now_marks_existing_results_of_external_resolvers(db: sqlite3.Connection) -> None:
    a, b = tracked(db, "a.com b.com")
    dns, doh, browser, agent = resolver(db, "dns"), resolver(db, "doh", ResolverKind.DOH), resolver(db, "browser", ResolverKind.BROWSER), resolver(db, "laptop", ResolverKind.AGENT)
    record(db, *(answer(n, r, Resolution(ResolveStatus.NXDOMAIN)) for n in (a, b) for r in (dns, doh, browser, agent)))
    assert request_external(db, Timestamp(NOW + 5), [a]) == 2
    assert {(r.resolver_id, r.requested_at) for r in name_resolves(db, [a])} == {(dns, None), (doh, None), (browser, NOW + 5), (agent, NOW + 5)}
    assert {r.requested_at for r in name_resolves(db, [b])} == {None}
    assert request_external(db, Timestamp(NOW + 9)) == 4
    external = {(n, r, NOW + 9) for n in (a, b) for r in (browser, agent)}
    assert {(r.name_id, r.resolver_id, r.requested_at) for r in name_resolves(db, [a, b]) if r.resolver_id in (browser, agent)} == external


def test_resolve_now_without_results_changes_nothing(db: sqlite3.Connection) -> None:
    (a,) = tracked(db, "a.com")
    resolver(db, "browser", ResolverKind.BROWSER)
    assert request_external(db, NOW, [a]) == 0
    assert name_resolves(db, [a]) == ()


@pytest.fixture
def edges(db: sqlite3.Connection) -> sqlite3.Connection:
    (a,) = tracked(db, "a.com")
    dns = resolver(db, "dns")
    inside = ("192.0.2.0", "192.0.2.255", "2001:db8::", "2001:db8::ffff:ffff:ffff:ffff")
    neighbours = ("192.0.1.255", "192.0.3.0", "2001:db7:ffff:ffff:ffff:ffff:ffff:ffff", "2001:db8:0:1::", "c000:205::1")
    record(db, answer(a, dns, ok(*inside, *neighbours)))
    return db


@pytest.mark.parametrize(
    ("network", "ips"),
    [
        ("192.0.2.0/24", ["192.0.2.0", "192.0.2.255"]),
        ("2001:db8::/64", ["2001:db8::", "2001:db8::ffff:ffff:ffff:ffff"]),
        ("192.0.2.255/32", ["192.0.2.255"]),
        ("198.51.100.0/24", []),
    ],
)
def test_range_query_takes_both_ends_of_the_network_and_no_neighbour(edges: sqlite3.Connection, network: str, ips: list[str]) -> None:
    assert [str(a.ip) for a in addresses_in(edges, ip_network(network))] == ips


def test_range_query_uses_the_index(db: sqlite3.Connection) -> None:
    plan = " ".join(row["detail"] for row in db.execute("EXPLAIN QUERY PLAN SELECT id FROM addresses WHERE family = ? AND ip_bin BETWEEN ? AND ?", (4, bytes(4), bytes(4))))
    assert "USING INDEX addresses_range" in plan or "USING COVERING INDEX addresses_range" in plan


def test_range_query_returns_every_name_of_an_ip_with_its_sightings(db: sqlite3.Connection) -> None:
    a, www, b = tracked(db, "a.com www.a.com b.com")
    dns, doh = resolver(db, "dns"), resolver(db, "doh", ResolverKind.DOH)
    record(db, answer(a, dns, ok("192.0.2.7")), answer(b, dns, ok("192.0.2.7")), answer(b, doh, ok("192.0.2.7")))
    record(db, answer(a, dns, ok("192.0.2.9")), answer(www, doh, ok("192.0.2.7")), at=NOW + DAY)
    domain_of = {n: DomainId(db.execute("SELECT domain_id FROM names WHERE id = ?", (n,)).fetchone()[0]) for n in (a, www, b)}
    first, second, third, fourth = (AddressId(i) for i in range(1, 5))
    later = Timestamp(NOW + DAY)
    assert addresses_in(db, IPv4Network("192.0.2.0/24")) == (
        StoredAddress(first, ip_address("192.0.2.7"), a, domain_of[a], DomainName("a.com"), (AddressSighting(dns, NOW, NOW, later),)),
        StoredAddress(second, ip_address("192.0.2.7"), b, domain_of[b], DomainName("b.com"), (AddressSighting(dns, NOW, NOW, NOW), AddressSighting(doh, NOW, NOW, NOW))),
        StoredAddress(fourth, ip_address("192.0.2.7"), www, domain_of[www], DomainName("www.a.com"), (AddressSighting(doh, later, later, later),)),
        StoredAddress(third, ip_address("192.0.2.9"), a, domain_of[a], DomainName("a.com"), (AddressSighting(dns, later, later, later),)),
    )


def test_sighting_without_a_result_reads_without_an_answer(db: sqlite3.Connection) -> None:
    (a,) = tracked(db, "a.com")
    dns = resolver(db, "dns")
    address = insert(db, "addresses", {"name_id": a, "ip": "192.0.2.1", "family": 4, "ip_bin": bytes([192, 0, 2, 1])})
    insert(db, "sightings", {"address_id": address, "resolver_id": dns, "first_seen_at": 5, "last_seen_at": 6})
    (stored,) = addresses_in(db, ip_network("192.0.2.1"))
    assert stored.sightings == (AddressSighting(dns, Timestamp(5), Timestamp(6), None),)


def test_shared_ips_group_rows_of_one_ip_in_order(db: sqlite3.Connection) -> None:
    a, b = tracked(db, "a.com b.com")
    dns = resolver(db, "dns")
    record(db, answer(a, dns, ok("192.0.2.7", "2001:db8::1")), answer(b, dns, ok("192.0.2.7")))
    grouped = shared_ips(addresses_in(db, ip_network("0.0.0.0/0")) + addresses_in(db, ip_network("::/0")))
    assert [(str(g.ip), g.names) for g in grouped] == [("192.0.2.7", (DomainName("a.com"), DomainName("b.com"))), ("2001:db8::1", (DomainName("a.com"),))]
    assert all(isinstance(g, SharedIp) for g in grouped)


def test_shared_ips_of_nothing() -> None:
    assert shared_ips(()) == ()


def test_unknown_names_have_no_results(db: sqlite3.Connection) -> None:
    assert name_resolves(db, [NameId(99)]) == ()


def test_resolve_now_of_an_empty_selection_changes_nothing(db: sqlite3.Connection) -> None:
    (a,) = tracked(db, "a.com")
    browser = resolver(db, "browser", ResolverKind.BROWSER)
    record(db, answer(a, browser, Resolution(ResolveStatus.NXDOMAIN)))
    assert request_external(db, Timestamp(NOW + 1), []) == 0
    assert name_resolves(db, [a])[0].requested_at is None


def resolution(status: ResolveStatus) -> Resolution:
    if status is ResolveStatus.OK:
        return ok("192.0.2.1")
    error = "down" if status in (ResolveStatus.ERROR, ResolveStatus.UNREACHABLE) else None
    return Resolution(status, error=error)


def address_events(db: sqlite3.Connection) -> tuple[Event, ...]:
    return list_events(db, EventFilter(100, EventKind.ADDRESS_NEW))


def times(db: sqlite3.Connection, name: NameId) -> tuple[int | None, int | None]:
    row = db.execute("SELECT last_attempt_at, last_success_at FROM names WHERE id = ?", (name,)).fetchone()
    return row["last_attempt_at"], row["last_success_at"]


@pytest.mark.parametrize("status", list(ResolveStatus))
def test_a_recorded_result_follows_the_classification_and_only_ok_creates_an_address(db: sqlite3.Connection, status: ResolveStatus) -> None:
    (a,) = tracked(db, "a.com")
    dns = resolver(db, "dns")
    requests = count(db, "requests")
    assert record(db, answer(a, dns, resolution(status))) == Recorded(1 if status is ResolveStatus.OK else 0)
    assert count(db, "requests") == requests
    answered, succeeded = answer_times(status, NOW)
    error = "down" if status in (ResolveStatus.ERROR, ResolveStatus.UNREACHABLE) else None
    assert name_resolves(db, [a]) == (NameResolve(a, dns, NOW, answered, succeeded, status, error, (), None, None),)
    assert [str(item.ip) for item in addresses_in(db, ip_network("192.0.2.0/24"))] == (["192.0.2.1"] if status is ResolveStatus.OK else [])
    assert len(address_events(db)) == (1 if status is ResolveStatus.OK else 0)
    assert db.execute("SELECT cnames FROM name_resolves").fetchone()[0] is None


def test_name_times_follow_every_result_and_stay_on_that_name(db: sqlite3.Connection) -> None:
    a, www = tracked(db, "a.com www.a.com")
    dns = resolver(db, "dns")
    record(db, answer(a, dns, ok("192.0.2.1")))
    assert times(db, a) == (NOW, NOW)
    assert times(db, www) == (None, None)
    record(db, answer(a, dns, Resolution(ResolveStatus.ERROR, error="timeout")), answer(www, dns, Resolution(ResolveStatus.NODATA)), at=NOW + 5)
    assert times(db, a) == (NOW + 5, NOW)
    assert times(db, www) == (NOW + 5, None)


def test_a_later_result_replaces_the_row_and_an_error_does_not_touch_sightings(db: sqlite3.Connection) -> None:
    (a,) = tracked(db, "a.com")
    browser = resolver(db, "browser", ResolverKind.BROWSER)
    record(db, answer(a, browser, ok("192.0.2.1", cnames=("edge.a.net", "cdn.a.net")), "10.8.0.6 Firefox"))
    assert request_external(db, Timestamp(NOW + 4), [a]) == 1
    assert db.execute("SELECT cnames FROM name_resolves").fetchone()[0] == '["edge.a.net", "cdn.a.net"]'
    answered, failed, requested = Timestamp(NOW + 8), Timestamp(NOW + 12), Timestamp(NOW + 4)
    record(db, answer(a, browser, Resolution(ResolveStatus.NXDOMAIN)), at=answered)
    record(db, answer(a, browser, Resolution(ResolveStatus.ERROR, error="timeout")), at=failed)
    assert name_resolves(db, [a]) == (NameResolve(a, browser, failed, answered, NOW, ResolveStatus.ERROR, "timeout", (), None, requested),)
    (stored,) = addresses_in(db, ip_network("192.0.2.1"))
    assert stored.sightings == (AddressSighting(browser, NOW, NOW, Timestamp(NOW + 8)),)
    assert db.execute("SELECT cnames FROM name_resolves").fetchone()[0] is None
    assert len(address_events(db)) == 1


def test_an_empty_authoritative_answer_leaves_the_sighting_and_moves_the_answer_forward(db: sqlite3.Connection) -> None:
    (a,) = tracked(db, "a.com")
    dns = resolver(db, "dns")
    record(db, answer(a, dns, ok("192.0.2.1")))
    moment = Timestamp(NOW + 10)
    assert record(db, answer(a, dns, Resolution(ResolveStatus.NODATA)), at=moment) == Recorded(0)
    assert name_resolves(db, [a]) == (NameResolve(a, dns, moment, moment, NOW, ResolveStatus.NODATA, None, (), None, None),)
    (stored,) = addresses_in(db, ip_network("192.0.2.1"))
    assert stored.sightings == (AddressSighting(dns, NOW, NOW, Timestamp(NOW + 10)),)
    assert sighting_state(stored.sightings[0], RETENTION) is SightingState.STALE


def test_address_new_is_the_new_row_not_the_new_sighting(db: sqlite3.Connection) -> None:
    a, www = tracked(db, "a.com www.a.com")
    dns, doh = resolver(db, "dns"), resolver(db, "doh", ResolverKind.DOH)
    domain = DomainId(db.execute("SELECT domain_id FROM names WHERE id = ?", (www,)).fetchone()[0])
    record(db, answer(www, dns, ok("2001:0DB8:0000::5")), actor="browser:10.8.0.6")
    (event,) = address_events(db)
    assert (event.actor, event.domain_id, event.name, event.details) == ("browser:10.8.0.6", domain, DomainName("www.a.com"), {"ip": "2001:db8::5", "resolver_id": dns, "resolver": "dns"})
    assert [e.id for e in list_events(db, EventFilter(10, resolver_id=dns)) if e.kind is EventKind.ADDRESS_NEW] == [event.id]
    assert record(db, answer(www, doh, ok("2001:db8::5")), at=NOW + 1) == Recorded(0)
    later = Timestamp(NOW + 2)
    assert record(db, answer(www, dns, ok("2001:db8::5", "192.0.2.8")), answer(a, dns, ok("192.0.2.8")), at=later) == Recorded(2)
    (v6,) = addresses_in(db, ip_network("2001:db8::/64"))
    assert v6.ip == ip_address("2001:db8::5")
    assert v6.sightings == (AddressSighting(dns, NOW, later, later), AddressSighting(doh, Timestamp(NOW + 1), Timestamp(NOW + 1), Timestamp(NOW + 1)))
    fresh = [e for e in address_events(db) if e.details["ip"] == "192.0.2.8"]
    assert [(e.name, e.details["resolver"]) for e in fresh] == [(DomainName("a.com"), "dns"), (DomainName("www.a.com"), "dns")]
    stored_ip = db.execute("SELECT family, ip_bin FROM addresses WHERE ip = '2001:db8::5'").fetchone()
    assert (stored_ip["family"], stored_ip["ip_bin"]) == (6, ip_address("2001:db8::5").packed)


def test_results_of_a_deleted_name_or_resolver_are_skipped(db: sqlite3.Connection) -> None:
    a, b = tracked(db, "a.com b.com")
    dns, gone = resolver(db, "dns"), resolver(db, "gone")
    db.execute("DELETE FROM names WHERE id = ?", (b,))
    db.execute("DELETE FROM resolvers WHERE id = ?", (gone,))
    recorded = record(db, answer(b, dns, ok("192.0.2.1")), answer(NameId(99), dns, ok("192.0.2.2")), answer(a, gone, ok("192.0.2.4")), answer(a, dns, ok("192.0.2.3")))
    assert recorded == Recorded(1)
    assert [str(item.ip) for item in addresses_in(db, ip_network("192.0.2.0/24"))] == ["192.0.2.3"]
    assert [(r.name_id, r.resolver_id) for r in name_resolves(db, [a, b])] == [(a, dns)]
    assert times(db, a) == (NOW, NOW)


def test_nothing_to_record_writes_nothing(db: sqlite3.Connection) -> None:
    assert record(db) == Recorded(0)
    assert count(db, "name_resolves") == 0


def record_and_fail(db: sqlite3.Connection) -> None:
    with transaction(db):
        (a,) = tracked(db, "a.com")
        dns = resolver(db, "dns")
        record(db, answer(a, dns, ok("192.0.2.1")))
        raise RuntimeError("fail the caller")


def test_recording_rolls_back_with_the_outer_transaction(db: sqlite3.Connection) -> None:
    with pytest.raises(RuntimeError, match="fail the caller"):
        record_and_fail(db)
    assert (count(db, "domains"), count(db, "addresses"), count(db, "sightings"), count(db, "name_resolves"), count(db, "events"), count(db, "requests")) == (0, 0, 0, 0, 0, 0)


def test_recording_is_visible_to_other_connections_only_after_the_caller_commits(db: sqlite3.Connection, other: sqlite3.Connection) -> None:
    (a,) = tracked(db, "a.com")
    dns = resolver(db, "dns")
    with transaction(db):
        record(db, answer(a, dns, ok("192.0.2.1")))
        assert count(other, "addresses") == 0
        assert name_resolves(other, [a]) == ()
    assert count(other, "addresses") == 1
    assert name_resolves(other, [a])[0].status is ResolveStatus.OK
