import sqlite3
from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from db_rows import NET_FIRST, NET_LAST, insert
from domains_world import NOW, count
from groups_world import group_ids, make_group
from subnet_tracker.core.db import transaction
from subnet_tracker.core.edits import KEEP
from subnet_tracker.core.global_routes import (
    GlobalRoute,
    GlobalRouteEdit,
    NewGlobalRoute,
    checked_route_edit,
    create_global_route,
    delete_global_route,
    edit_global_route,
    edit_route_groups,
    find_global_route,
    get_global_route,
    global_route_network,
    list_global_routes,
    server_address_error,
)
from subnet_tracker.core.groups import Group, GroupRow, delete_group, list_groups
from subnet_tracker.core.memberships import SetChange
from subnet_tracker.types import FieldError, GlobalRouteId, GroupId, IPAddress, Timestamp

LATER = Timestamp(NOW + 10)
SERVERS = (ip_address("203.0.113.10"), ip_address("2a01:4f8:5::10"))


def make_route(db: sqlite3.Connection, cidr: str, *groups: Group, comment: str = "", enabled: bool = True) -> GlobalRoute:
    route = create_global_route(db, NewGlobalRoute(cidr, comment, enabled, group_ids(*groups)), SERVERS, NOW)
    assert isinstance(route, GlobalRoute)
    return route


def stored(db: sqlite3.Connection, route_id: GlobalRouteId) -> GlobalRoute:
    route = get_global_route(db, route_id)
    assert route is not None
    return route


@pytest.mark.parametrize(
    ("cidr", "covered"),
    [
        ("203.0.113.10/32", "203.0.113.10"),
        ("203.0.113.0/24", "203.0.113.10"),
        ("203.0.113.8/29", "203.0.113.10"),
        ("0.0.0.0/0", "203.0.113.10"),
        ("2a01:4f8:5::10/128", "2a01:4f8:5::10"),
        ("2a01:4f8::/32", "2a01:4f8:5::10"),
        ("::/0", "2a01:4f8:5::10"),
        ("203.0.113.11/32", None),
        ("203.0.113.0/29", None),
        ("198.51.100.0/24", None),
        ("2a01:4f8:5::11/128", None),
        ("2a01:4f8:6::/48", None),
    ],
)
def test_route_covering_a_server_address_is_an_error(cidr: str, covered: str | None) -> None:
    assert server_address_error(ip_network(cidr), SERVERS) == (None if covered is None else f"covers the server address {covered}")


def test_server_addresses_of_the_other_family_do_not_matter() -> None:
    assert server_address_error(ip_network("0.0.0.0/0"), SERVERS[1:]) is None
    assert server_address_error(ip_network("::/0"), SERVERS[:1]) is None
    assert server_address_error(ip_network("0.0.0.0/0"), ()) is None


@given(st.ip_addresses(), st.data())
def test_every_network_around_a_server_address_is_rejected(server: IPAddress, data: st.DataObject) -> None:
    network = ip_network(f"{server}/{data.draw(st.integers(0, server.max_prefixlen))}", strict=False)
    assert server_address_error(network, [server]) == f"covers the server address {server}"


@given(st.tuples(st.ip_addresses(v=4), st.ip_addresses(v=4)) | st.tuples(st.ip_addresses(v=6), st.ip_addresses(v=6)))
def test_largest_network_that_splits_off_the_server_address_is_allowed(addresses: tuple[IPAddress, IPAddress]) -> None:
    server, other = addresses
    assume(server != other)
    prefix = server.max_prefixlen - (int(server) ^ int(other)).bit_length() + 1
    network = ip_network(f"{other}/{prefix}", strict=False)
    assert server_address_error(network, [server]) is None
    assert server_address_error(network.supernet(), [server]) is not None


@pytest.mark.parametrize(
    ("text", "result"),
    [
        (" 192.0.2.7/24 ", IPv4Network("192.0.2.0/24")),
        ("192.0.2.7", IPv4Network("192.0.2.7/32")),
        ("2001:DB8:0:0::1", IPv6Network("2001:db8::1/128")),
        ("2001:db8::1/32", IPv6Network("2001:db8::/32")),
        ("example.com", FieldError("cidr", "not an address or CIDR")),
        ("192.0.2.0/33", FieldError("cidr", "not an address or CIDR")),
        ("fe80::1%eth0", FieldError("cidr", "zone index is not routable")),
        ("203.0.113.10", FieldError("cidr", "covers the server address 203.0.113.10")),
        ("2a01:4f8:5::/64", FieldError("cidr", "covers the server address 2a01:4f8:5::10")),
    ],
)
def test_global_route_network_is_normalized_and_checked(text: str, result: IPv4Network | IPv6Network | FieldError) -> None:
    assert global_route_network(text, SERVERS) == result


def test_checked_route_edit_normalizes_and_keeps_unmentioned_fields() -> None:
    assert checked_route_edit(GlobalRouteEdit(cidr="192.0.2.7/24", comment=" LAN "), SERVERS) == GlobalRouteEdit(cidr="192.0.2.0/24", comment="LAN", enabled=KEEP)
    assert checked_route_edit(GlobalRouteEdit(), SERVERS) == GlobalRouteEdit()


def test_create_route_stores_normalized_cidr_and_family(db: sqlite3.Connection) -> None:
    route = make_route(db, " 192.0.2.7/24", comment=" LAN behind the server ", enabled=False)
    assert route == GlobalRoute(route.id, IPv4Network("192.0.2.0/24"), "LAN behind the server", False, frozenset(), NOW, NOW)
    v6 = make_route(db, "2001:DB8:0::/32")
    assert [tuple(row) for row in db.execute("SELECT cidr, family FROM global_routes ORDER BY id")] == [("192.0.2.0/24", 4), ("2001:db8::/32", 6)]
    assert stored(db, route.id) == route
    assert find_global_route(db, IPv6Network("2001:db8::/32")) == v6
    assert not db.in_transaction


def test_create_route_joins_known_groups(db: sqlite3.Connection) -> None:
    video, music = make_group(db, "Video"), make_group(db, "Music")
    route = create_global_route(db, NewGlobalRoute("192.0.2.0/24", groups=group_ids(video, music) | {GroupId(99)}), SERVERS, NOW)
    assert isinstance(route, GlobalRoute)
    assert (route.groups, route.updated_at) == (group_ids(video, music), NOW)
    assert list_groups(db) == (GroupRow(None, 0, 0), GroupRow(music, 0, 1), GroupRow(video, 0, 1))


def test_unknown_route_is_not_found(db: sqlite3.Connection) -> None:
    make_route(db, "192.0.2.0/24")
    assert find_global_route(db, IPv4Network("192.0.2.0/25")) is None
    assert get_global_route(db, GlobalRouteId(99)) is None


@pytest.mark.parametrize(
    ("route", "error"),
    [
        (NewGlobalRoute("192.0.2.0/24/8"), FieldError("cidr", "not an address or CIDR")),
        (NewGlobalRoute("fe80::%eth0/64"), FieldError("cidr", "zone index is not routable")),
        (NewGlobalRoute("203.0.0.0/8"), FieldError("cidr", "covers the server address 203.0.113.10")),
        (NewGlobalRoute("2a01:4f8:5::10"), FieldError("cidr", "covers the server address 2a01:4f8:5::10")),
        (NewGlobalRoute("192.0.2.0/24", "two\nlines"), FieldError("comment", "line breaks are not allowed")),
    ],
)
def test_invalid_route_is_rejected(db: sqlite3.Connection, route: NewGlobalRoute, error: FieldError) -> None:
    assert create_global_route(db, route, SERVERS, NOW) == error
    assert count(db, "global_routes") == 0


def test_routes_bypass_builtin_exclusions_and_need_no_server_addresses(db: sqlite3.Connection) -> None:
    assert isinstance(create_global_route(db, NewGlobalRoute("10.8.0.0/24"), SERVERS, NOW), GlobalRoute)
    assert isinstance(create_global_route(db, NewGlobalRoute("0.0.0.0/0"), (), NOW), GlobalRoute)


def test_duplicate_cidr_is_rejected_after_normalization(db: sqlite3.Connection) -> None:
    make_route(db, "192.0.2.0/24")
    video = make_group(db, "Video")
    assert create_global_route(db, NewGlobalRoute("192.0.2.9/24", groups=group_ids(video)), SERVERS, NOW) == FieldError("cidr", "global route '192.0.2.0/24' already exists")
    assert (count(db, "global_routes"), count(db, "route_groups")) == (1, 0)


def test_list_routes_ipv4_first_by_address_then_prefix(db: sqlite3.Connection) -> None:
    for cidr in ("2001:db8::/32", "149.154.160.0/20", "9.0.0.0/16", "9.0.0.0/8"):
        make_route(db, cidr)
    assert [str(r.network) for r in list_global_routes(db)] == ["9.0.0.0/8", "9.0.0.0/16", "149.154.160.0/20", "2001:db8::/32"]


def test_edit_route_changes_given_fields_family_and_updated_at(db: sqlite3.Connection) -> None:
    video = make_group(db, "Video")
    route = make_route(db, "192.0.2.0/24", video)
    edited = edit_global_route(db, route.id, GlobalRouteEdit(cidr="2001:db8::1/48", comment=" moved ", enabled=False), SERVERS, LATER)
    assert edited == GlobalRoute(route.id, IPv6Network("2001:db8::/48"), "moved", False, group_ids(video), NOW, LATER)
    assert stored(db, route.id) == edited
    assert db.execute("SELECT family FROM global_routes").fetchone()[0] == 6


def test_empty_edit_returns_route_unchanged(db: sqlite3.Connection) -> None:
    route = make_route(db, "192.0.2.0/24")
    assert edit_global_route(db, route.id, GlobalRouteEdit(), SERVERS, LATER) == route


def test_edit_of_unknown_route_returns_none(db: sqlite3.Connection) -> None:
    assert edit_global_route(db, GlobalRouteId(99), GlobalRouteEdit(enabled=False), SERVERS, LATER) is None


@pytest.mark.parametrize(
    ("edit", "error"),
    [
        (GlobalRouteEdit(cidr="203.0.113.0/24", enabled=False), FieldError("cidr", "covers the server address 203.0.113.10")),
        (GlobalRouteEdit(cidr="::/0"), FieldError("cidr", "covers the server address 2a01:4f8:5::10")),
        (GlobalRouteEdit(cidr=""), FieldError("cidr", "not an address or CIDR")),
        (GlobalRouteEdit(comment="a\u2028b"), FieldError("comment", "line breaks are not allowed")),
        (GlobalRouteEdit(cidr="198.51.100.7/24"), FieldError("cidr", "global route '198.51.100.0/24' already exists")),
    ],
)
def test_invalid_edit_is_rejected_and_changes_nothing(db: sqlite3.Connection, edit: GlobalRouteEdit, error: FieldError) -> None:
    make_route(db, "198.51.100.0/24")
    route = make_route(db, "192.0.2.0/24")
    assert edit_global_route(db, route.id, edit, SERVERS, LATER) == error
    assert stored(db, route.id) == route


def test_edit_to_own_cidr_is_allowed(db: sqlite3.Connection) -> None:
    route = make_route(db, "192.0.2.0/24")
    edited = edit_global_route(db, route.id, GlobalRouteEdit(cidr="192.0.2.99/24"), SERVERS, LATER)
    assert isinstance(edited, GlobalRoute)
    assert edited.network == route.network


def test_edit_without_cidr_does_not_recheck_server_addresses(db: sqlite3.Connection) -> None:
    route = make_route(db, "192.0.2.0/24")
    edited = edit_global_route(db, route.id, GlobalRouteEdit(comment="kept", enabled=True), (ip_address("192.0.2.1"),), LATER)
    assert isinstance(edited, GlobalRoute)
    assert edited.comment == "kept"


def test_edit_route_groups_touches_only_changed_routes(db: sqlite3.Connection) -> None:
    video, music = make_group(db, "Video"), make_group(db, "Music")
    a, b, c = make_route(db, "192.0.2.0/24", video), make_route(db, "198.51.100.0/24"), make_route(db, "2001:db8::/32", video, music)
    assert edit_route_groups(db, [a.id, b.id, c.id, GlobalRouteId(99)], SetChange(remove=group_ids(music), add=group_ids(video) | {GroupId(99)}), LATER) == 2
    assert [(r.groups, r.updated_at) for r in (stored(db, r.id) for r in (a, b, c))] == [(group_ids(video), NOW), (group_ids(video), LATER), (group_ids(video), LATER)]
    assert edit_route_groups(db, [a.id, c.id], SetChange(replace=frozenset()), LATER) == 2
    assert list_groups(db) == (GroupRow(None, 0, 2), GroupRow(music, 0, 0), GroupRow(video, 0, 1))
    assert not db.in_transaction


def test_delete_route_drops_its_groups_and_detaches_the_plan_route(db: sqlite3.Connection) -> None:
    video = make_group(db, "Video")
    route = make_route(db, "198.51.100.0/24", video)
    insert(db, "plan_routes", {"cidr": "198.51.100.0/24", "family": 4, "first_bin": NET_FIRST, "last_bin": NET_LAST, "kind": "global", "global_route_id": route.id, "added_at": 0, "reconnected": 0})
    assert delete_global_route(db, route.id)
    assert (count(db, "global_routes"), count(db, "route_groups"), count(db, "groups")) == (0, 0, 1)
    assert db.execute("SELECT global_route_id FROM plan_routes").fetchone()[0] is None
    assert not delete_global_route(db, route.id)


def test_deleting_a_group_leaves_its_routes_without_it(db: sqlite3.Connection) -> None:
    video, music = make_group(db, "Video"), make_group(db, "Music")
    route = make_route(db, "192.0.2.0/24", video, music)
    delete_group(db, video.id)
    assert stored(db, route.id).groups == group_ids(music)


def create_route_and_fail(db: sqlite3.Connection) -> None:
    with transaction(db):
        route = make_route(db, "192.0.2.0/24")
        edit_route_groups(db, [route.id], SetChange(add=group_ids(make_group(db, "New"))), LATER)
        raise RuntimeError


def test_route_writes_compose_in_one_transaction(db: sqlite3.Connection) -> None:
    with pytest.raises(RuntimeError):
        create_route_and_fail(db)
    assert (count(db, "global_routes"), count(db, "groups"), count(db, "route_groups")) == (0, 0, 0)
