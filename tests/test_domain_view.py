import sqlite3

import pytest

from db_rows import SHA256, insert
from domain_view_world import add_resolver, resolve_times, resolved, seen
from domains_world import NOW, paste
from groups_world import domain, join, make_group
from subnet_tracker.core.domain_query import DomainQuery, DomainSort, DomainState, query_domains
from subnet_tracker.core.domain_view import AddressView, DomainView, domain_views
from subnet_tracker.core.name_status import NameStatus, Sighting
from subnet_tracker.types import BatchId, FieldError, ResolverId, ResolveStatus, Seconds, Timestamp

DAY = Seconds(86400)


@pytest.fixture
def world(db: sqlite3.Connection) -> sqlite3.Connection:
    paste(db, "a.com www.b.com c.com d.com")
    join(db, domain(db, "a.com").id, make_group(db, "Work"))
    join(db, domain(db, "c.com").id, make_group(db, "Off", enabled=False))
    db.execute("UPDATE domains SET enabled = 0 WHERE id = ?", (domain(db, "d.com").id,))
    for tag in ("video", "art"):
        db.execute("INSERT INTO domain_tags (domain_id, tag_id) VALUES (?, ?)", (domain(db, "a.com").id, insert(db, "tags", {"name": tag})))
    system, spare = add_resolver(db, "system", 0), add_resolver(db, "spare", 1, use_for_routes=False)
    resolved(db, "a.com", system, ResolveStatus.OK, 100)
    seen(db, "a.com", "192.0.2.10", system, 50, 100)
    resolved(db, "www.b.com", system, ResolveStatus.NXDOMAIN, 100)
    resolved(db, "b.com", system, ResolveStatus.ERROR, 100, "timeout")
    resolved(db, "b.com", spare, ResolveStatus.OK, 100)
    resolved(db, "d.com", system, ResolveStatus.OK, 200)
    seen(db, "d.com", "192.0.2.20", system, 100, 100)
    return db


def views(db: sqlite3.Connection) -> dict[str, DomainView]:
    return {v.domain.canonical.name: v for v in domain_views(db)}


def test_views_are_newest_first_with_groups_tags_routing_and_both_names(world: sqlite3.Connection) -> None:
    found = views(world)
    assert list(found) == ["d.com", "c.com", "www.b.com", "a.com"]
    a = found["a.com"]
    assert (a.groups, a.tags, [n.name.name for n in a.names]) == (("Work",), ("art", "video"), ["a.com", "www.a.com"])
    assert {name: v.routable for name, v in found.items()} == {"d.com": False, "c.com": False, "www.b.com": True, "a.com": True}


def test_names_carry_resolver_results_in_resolver_order_and_their_status(world: sqlite3.Connection) -> None:
    b = views(world)["www.b.com"]
    canonical, pair = b.names
    assert [(r.resolver, r.routes, r.status, r.error) for r in pair.results] == [("system", True, ResolveStatus.ERROR, "timeout"), ("spare", False, ResolveStatus.OK, None)]
    assert (canonical.status, pair.status, b.status) == (NameStatus.NOTFOUND, NameStatus.ERROR, NameStatus.NOTFOUND)
    assert views(world)["c.com"].status is None


def test_revoked_agent_is_not_a_checked_resolver(world: sqlite3.Connection) -> None:
    agent = ResolverId(insert(world, "resolvers", {"name": "laptop", "kind": "agent", "position": 2, "created_at": 0, "updated_at": 0}))
    insert(world, "agents", {"resolver_id": agent, "cert_sha256": SHA256, "cert_not_after": 0, "revoked_at": 1, "created_at": 0})
    resolved(world, "c.com", agent, ResolveStatus.OK, 100)
    (result,) = views(world)["c.com"].names[0].results
    assert (result.resolver, result.routes) == ("laptop", False)


def test_addresses_carry_sightings_with_the_answer_time_of_their_resolver(world: sqlite3.Connection) -> None:
    found = views(world)
    assert found["d.com"].names[0].addresses == (AddressView("192.0.2.20", (Sighting("system", Timestamp(100), Timestamp(100), Timestamp(200)),)),)
    assert found["d.com"].stale(DAY)
    assert not found["a.com"].stale(DAY)
    assert not found["d.com"].stale(Seconds(100))


def test_views_of_given_domains(world: sqlite3.Connection) -> None:
    ids = [domain(world, "a.com").id, domain(world, "c.com").id]
    assert [v.domain.canonical.name for v in domain_views(world, ids)] == ["c.com", "a.com"]
    assert domain_views(world, []) == ()


def listed(db: sqlite3.Connection, query: DomainQuery) -> list[str]:
    found = query_domains(db, query)
    assert not isinstance(found, FieldError)
    return [v.domain.canonical.name for v in found]


@pytest.mark.parametrize(
    ("query", "names"),
    [
        (DomainQuery(), ["d.com", "c.com", "www.b.com", "a.com"]),
        (DomainQuery(group="Work"), ["a.com"]),
        (DomainQuery(no_group=True), ["d.com", "www.b.com"]),
        (DomainQuery(tag="video"), ["a.com"]),
        (DomainQuery(search="WOR"), ["a.com"]),
        (DomainQuery(search="ART"), ["a.com"]),
        (DomainQuery(search="b.c"), ["www.b.com"]),
        (DomainQuery(search="www.d"), ["d.com"]),
        (DomainQuery(state=DomainState.OK), ["d.com", "a.com"]),
        (DomainQuery(state=DomainState.ERROR), ["www.b.com"]),
        (DomainQuery(state=DomainState.STALE), ["d.com"]),
        (DomainQuery(state=DomainState.DISABLED), ["d.com", "c.com"]),
        (DomainQuery(no_group=True, state=DomainState.ERROR), ["www.b.com"]),
        (DomainQuery(sort=DomainSort.NAME), ["a.com", "c.com", "d.com", "www.b.com"]),
    ],
)
def test_query_filters_and_sorts(world: sqlite3.Connection, query: DomainQuery, names: list[str]) -> None:
    assert listed(world, query) == names


@pytest.mark.parametrize(("search", "found"), [("ПРИМ", True), ("пример.рф", True), ("xn--e1afmkfd", True), ("p1ai", True), ("кот", False)])
def test_search_matches_punycode_and_unicode_names(db: sqlite3.Connection, search: str, found: bool) -> None:
    paste(db, "a.com пример.рф")
    assert listed(db, DomainQuery(search=search)) == (["xn--e1afmkfd.xn--p1ai"] if found else [])


def test_resolve_time_sorts_put_the_oldest_and_unresolved_first(world: sqlite3.Connection) -> None:
    for name, attempt, success in (("a.com", 300, 300), ("www.a.com", 100, None), ("www.b.com", 200, None), ("d.com", 100, 50)):
        resolve_times(world, name, attempt, success)
    assert listed(world, DomainQuery(sort=DomainSort.LAST_ATTEMPT)) == ["c.com", "d.com", "www.b.com", "a.com"]
    assert listed(world, DomainQuery(sort=DomainSort.LAST_SUCCESS)) == ["c.com", "www.b.com", "d.com", "a.com"]


def test_query_by_batch(world: sqlite3.Connection) -> None:
    batch = paste(world, "e.com f.com", Timestamp(NOW + 1)).batch_id
    assert listed(world, DomainQuery(batch_id=batch)) == ["f.com", "e.com"]


@pytest.mark.parametrize(
    ("query", "error"),
    [
        (DomainQuery(group="Play"), FieldError("group", "'Play' does not exist")),
        (DomainQuery(tag="audio"), FieldError("tag", "'audio' does not exist")),
        (DomainQuery(batch_id=BatchId(99)), FieldError("batch", "99 does not exist")),
    ],
)
def test_unknown_group_tag_or_batch_is_an_error(world: sqlite3.Connection, query: DomainQuery, error: FieldError) -> None:
    assert query_domains(world, query) == error
