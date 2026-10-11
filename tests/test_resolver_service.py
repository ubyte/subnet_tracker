import sqlite3

import pytest

from db_rows import insert
from domains_world import ACTOR, NOW, count, paste
from subnet_tracker.core.db import transaction
from subnet_tracker.core.events import EventFilter, EventKind, list_events
from subnet_tracker.core.requests import TickWork, claim_requests
from subnet_tracker.core.resolvers import NewResolver, ResolverEdit, get_resolver
from subnet_tracker.core.services.resolvers import add_resolver, change_resolver, remove_resolver
from subnet_tracker.types import FieldError, ReconnectMode, ResolverKind, Timestamp

LATER = Timestamp(NOW + 10)


def work(db: sqlite3.Connection) -> TickWork | None:
    claim = claim_requests(db)
    return None if claim is None else claim.work


def test_add_records_the_event_and_does_not_request_apply(db: sqlite3.Connection) -> None:
    added = add_resolver(db, NewResolver(" Quad9 ", ResolverKind.DNS, " UDP://9.9.9.9 ", False, 20), ACTOR, NOW)
    assert not isinstance(added, FieldError)
    resolver = added.value
    assert (resolver.name, resolver.endpoint, resolver.use_for_routes, resolver.max_qps, resolver.position) == ("Quad9", "udp://9.9.9.9", False, 20, 0)
    (event,) = list_events(db, EventFilter(1))
    assert (event.kind, dict(event.details)) == (
        EventKind.RESOLVER_ADDED,
        {"resolver_id": resolver.id, "resolver": "Quad9", "kind": "dns", "endpoint": "udp://9.9.9.9", "use_for_routes": False, "max_qps": 20},
    )
    assert work(db) is None


def test_agent_kind_is_refused(db: sqlite3.Connection) -> None:
    assert add_resolver(db, NewResolver("laptop", ResolverKind.AGENT), ACTOR, NOW) == FieldError("kind", "agent resolvers are created with their agent")
    assert count(db, "resolvers") == 0


def test_routes_flag_requests_apply_and_qps_does_not(db: sqlite3.Connection) -> None:
    added = add_resolver(db, NewResolver("Quad9", ResolverKind.DNS, "udp://9.9.9.9"), ACTOR, NOW)
    assert not isinstance(added, FieldError)
    resolver = added.value
    qps = change_resolver(db, resolver.id, ResolverEdit(max_qps=10), ACTOR, LATER)
    assert not isinstance(qps, FieldError)
    assert qps.changes == {"max_qps": (50, 10)}
    assert work(db) is None
    routes = change_resolver(db, resolver.id, ResolverEdit(use_for_routes=False), ACTOR, LATER)
    assert not isinstance(routes, FieldError)
    assert routes.changes == {"use_for_routes": (True, False)}
    (event,) = [e for e in list_events(db, EventFilter(5)) if e.kind is EventKind.RESOLVER_CHANGED and "use_for_routes" in e.details]
    assert event.details["resolver_id"] == resolver.id
    assert event.details["resolver"] == "Quad9"
    assert work(db) == TickWork(apply=ReconnectMode.AUTO)


def test_same_values_do_not_touch_the_row(db: sqlite3.Connection) -> None:
    added = add_resolver(db, NewResolver("Quad9", ResolverKind.DNS, "udp://9.9.9.9"), ACTOR, NOW)
    assert not isinstance(added, FieldError)
    resolver = added.value
    updated = change_resolver(db, resolver.id, ResolverEdit(name=" Quad9 ", endpoint="udp://9.9.9.9", max_qps=50), ACTOR, LATER)
    assert not isinstance(updated, FieldError)
    assert updated.changes == {}
    assert get_resolver(db, resolver.id) == resolver
    assert count(db, "events") == 1


def test_delete_of_a_routing_resolver_requests_apply_and_counts_addresses(db: sqlite3.Connection) -> None:
    paste(db, "a.com")
    name_id = db.execute("SELECT id FROM names WHERE name = 'a.com'").fetchone()[0]
    added = add_resolver(db, NewResolver("Quad9", ResolverKind.DNS, "udp://9.9.9.9"), ACTOR, NOW)
    assert not isinstance(added, FieldError)
    resolver = added.value
    address = insert(db, "addresses", {"name_id": name_id, "ip": "192.0.2.1", "family": 4, "ip_bin": bytes([192, 0, 2, 1])})
    insert(db, "sightings", {"address_id": address, "resolver_id": resolver.id, "first_seen_at": 0, "last_seen_at": 0})
    removed = remove_resolver(db, resolver.id, ACTOR, LATER)
    assert not isinstance(removed, FieldError)
    assert removed.addresses == 1
    assert count(db, "addresses") == 0
    (event,) = [e for e in list_events(db, EventFilter(5)) if e.kind is EventKind.RESOLVER_DELETED]
    assert dict(event.details) == {"resolver_id": resolver.id, "resolver": "Quad9", "addresses": 1}
    assert work(db) == TickWork(apply=ReconnectMode.AUTO)


def test_delete_of_a_resolver_kept_out_of_routes_does_not_request_apply(db: sqlite3.Connection) -> None:
    added = add_resolver(db, NewResolver("Quad9", ResolverKind.DNS, "udp://9.9.9.9", use_for_routes=False), ACTOR, NOW)
    assert not isinstance(added, FieldError)
    resolver = added.value
    removed = remove_resolver(db, resolver.id, ACTOR, LATER)
    assert not isinstance(removed, FieldError)
    assert removed.addresses == 0
    assert work(db) is None


def add_and_fail(db: sqlite3.Connection) -> None:
    with transaction(db):
        add_resolver(db, NewResolver("Quad9", ResolverKind.DNS, "udp://9.9.9.9"), ACTOR, NOW)
        raise RuntimeError


def test_add_rolls_back_with_the_outer_transaction(db: sqlite3.Connection) -> None:
    with pytest.raises(RuntimeError):
        add_and_fail(db)
    assert (count(db, "resolvers"), count(db, "events")) == (0, 0)
