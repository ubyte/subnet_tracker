import sqlite3
from ipaddress import ip_address

import pytest

from domains_world import ACTOR, NOW, count
from groups_world import make_group
from subnet_tracker.core.db import transaction
from subnet_tracker.core.events import EventFilter, EventKind, list_events
from subnet_tracker.core.global_routes import GlobalRouteEdit, find_global_route, get_global_route
from subnet_tracker.core.requests import TickWork, claim_requests
from subnet_tracker.core.services.routes import RouteChange, add_route, change_route, lookup_route, remove_route
from subnet_tracker.types import FieldError, IPAddress, ReconnectMode, Timestamp

LATER = Timestamp(NOW + 10)
SERVERS = (ip_address("203.0.113.10"),)
NONE: tuple[IPAddress, ...] = ()


def work(db: sqlite3.Connection) -> TickWork | None:
    claim = claim_requests(db)
    return None if claim is None else claim.work


def test_add_normalizes_the_cidr_and_requests_apply(db: sqlite3.Connection) -> None:
    make_group(db, "Video")
    route = add_route(db, " 192.0.2.7/24 ", " LAN ", ["Video"], NONE, ACTOR, NOW)
    assert not isinstance(route, FieldError)
    assert (str(route.network), route.comment, route.enabled) == ("192.0.2.0/24", "LAN", True)
    (event,) = list_events(db, EventFilter(1))
    assert event.kind is EventKind.ROUTE_ADDED
    assert dict(event.details) == {"route_id": route.id, "cidr": "192.0.2.0/24", "comment": "LAN", "enabled": True, "groups": ["Video"]}
    assert work(db) == TickWork(apply=ReconnectMode.AUTO)


@pytest.mark.parametrize(
    ("cidr", "error"),
    [
        ("::ffff:192.0.2.1", "IPv4-mapped addresses are not routable, use the IPv4 form"),
        ("203.0.113.10", "covers the server address 203.0.113.10"),
        ("example.com", "not an address or CIDR"),
    ],
)
def test_rejected_cidr_writes_nothing(db: sqlite3.Connection, cidr: str, error: str) -> None:
    assert add_route(db, cidr, "", [], SERVERS, ACTOR, NOW) == FieldError("cidr", error)
    assert (count(db, "global_routes"), count(db, "events")) == (0, 0)


def test_unknown_group_writes_nothing(db: sqlite3.Connection) -> None:
    assert add_route(db, "192.0.2.0/24", "", ["Missing"], NONE, ACTOR, NOW) == FieldError("group", "'Missing' does not exist")
    assert count(db, "global_routes") == 0


def test_duplicate_cidr_is_rejected_after_normalization(db: sqlite3.Connection) -> None:
    assert not isinstance(add_route(db, "192.0.2.0/24", "", [], NONE, ACTOR, NOW), FieldError)
    assert add_route(db, "192.0.2.9/24", "", [], NONE, ACTOR, LATER) == FieldError("cidr", "global route '192.0.2.0/24' already exists")
    assert count(db, "global_routes") == 1


def test_comment_edit_does_not_request_apply(db: sqlite3.Connection) -> None:
    created = add_route(db, "192.0.2.0/24", "", [], NONE, ACTOR, NOW)
    assert not isinstance(created, FieldError)
    assert count(db, "requests") == 1
    updated = change_route(db, created.id, RouteChange(GlobalRouteEdit(comment=" LAN ")), NONE, ACTOR, LATER)
    assert not isinstance(updated, FieldError)
    assert updated.changes == {"comment": ("", "LAN")}
    assert count(db, "requests") == 1
    assert count(db, "events") == 2


def test_repeating_the_same_cidr_is_not_a_change_even_when_it_covers_a_server(db: sqlite3.Connection) -> None:
    created = add_route(db, "192.0.2.0/24", "", [], NONE, ACTOR, NOW)
    assert not isinstance(created, FieldError)
    servers = (ip_address("192.0.2.1"),)
    updated = change_route(db, created.id, RouteChange(GlobalRouteEdit(cidr="192.0.2.9/24")), servers, ACTOR, LATER)
    assert not isinstance(updated, FieldError)
    assert updated.changes == {}
    assert get_global_route(db, created.id) == created
    widened = change_route(db, created.id, RouteChange(GlobalRouteEdit(cidr="192.0.2.0/16")), servers, ACTOR, LATER)
    assert widened == FieldError("cidr", "covers the server address 192.0.2.1")
    assert get_global_route(db, created.id) == created


def test_group_edit_requests_apply(db: sqlite3.Connection) -> None:
    make_group(db, "Video")
    created = add_route(db, "192.0.2.0/24", "", [], NONE, ACTOR, NOW)
    assert not isinstance(created, FieldError)
    updated = change_route(db, created.id, RouteChange(add_groups=("Video",)), NONE, ACTOR, LATER)
    assert not isinstance(updated, FieldError)
    assert updated.changes == {"groups": ([], ["Video"])}
    stored = get_global_route(db, created.id)
    assert stored is not None
    assert stored.updated_at == LATER
    assert work(db) == TickWork(apply=ReconnectMode.AUTO)


def test_delete_requests_apply(db: sqlite3.Connection) -> None:
    created = add_route(db, "192.0.2.0/24", "", [], NONE, ACTOR, NOW)
    assert not isinstance(created, FieldError)
    removed = remove_route(db, created.id, ACTOR, LATER)
    assert not isinstance(removed, FieldError)
    assert find_global_route(db, created.network) is None
    assert lookup_route(db, "192.0.2.0/24") == FieldError("cidr", "192.0.2.0/24 does not exist")
    assert next(e.kind for e in list_events(db, EventFilter(2))) is EventKind.ROUTE_DELETED


def add_and_fail(db: sqlite3.Connection) -> None:
    with transaction(db):
        add_route(db, "192.0.2.0/24", "", [], NONE, ACTOR, NOW)
        raise RuntimeError


def test_add_rolls_back_with_the_outer_transaction(db: sqlite3.Connection) -> None:
    with pytest.raises(RuntimeError):
        add_and_fail(db)
    assert (count(db, "global_routes"), count(db, "events"), count(db, "requests")) == (0, 0, 0)
